import json
from typing import Literal, Optional

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate, nowdate
from shopify.collection import PaginatedIterator
from shopify.resources import Order

from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import (
	CUSTOMER_ID_FIELD,
	EVENT_MAPPER,
	ORDER_DISCOUNT_CODES_FIELD,
	ORDER_FINANCIAL_STATUS_FIELD,
	ORDER_FULFILLMENT_STATUS_FIELD,
	ORDER_ID_FIELD,
	ORDER_ITEM_DISCOUNT_FIELD,
	ORDER_ITEM_PROPERTIES_FIELD,
	ORDER_ITEM_SHIPPING_METHOD_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	ORDER_TIP_AMOUNT_FIELD,
	SETTING_DOCTYPE,
	UNMATCHED_ITEM_CODE,
)
from ecommerce_integrations.shopify.customer import ShopifyCustomer
from ecommerce_integrations.shopify.product import create_items_if_not_exist, get_item_code
from ecommerce_integrations.shopify.utils import create_shopify_log
from ecommerce_integrations.utils.price_list import get_dummy_price_list
from ecommerce_integrations.utils.taxation import get_dummy_tax_category

DEFAULT_TAX_FIELDS = {
	"sales_tax": "default_sales_tax_account",
	"shipping": "default_shipping_charges_account",
}


def sync_sales_order(payload, request_id=None):
	order = payload
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	if frappe.db.get_value("Sales Order", filters={ORDER_ID_FIELD: cstr(order["id"])}):
		create_shopify_log(status="Invalid", message="Sales order already exists, not synced")
		return
	try:
		shopify_customer = order.get("customer") if order.get("customer") is not None else {}
		shopify_customer["billing_address"] = order.get("billing_address", "")
		shopify_customer["shipping_address"] = order.get("shipping_address", "")
		customer_id = shopify_customer.get("id")
		if customer_id:
			customer = ShopifyCustomer(customer_id=customer_id)
			if not customer.is_synced():
				customer.sync_customer(customer=shopify_customer)
			else:
				customer.update_existing_addresses(shopify_customer)

		create_items_if_not_exist(order)

		setting = frappe.get_doc(SETTING_DOCTYPE)
		create_order(order, setting)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)
	else:
		create_shopify_log(status="Success")


def create_order(order, setting, company=None):
	# local import to avoid circular dependencies
	from ecommerce_integrations.shopify.fulfillment import create_delivery_note
	from ecommerce_integrations.shopify.invoice import create_sales_invoice

	so = create_sales_order(order, setting, company)
	if so:
		if order.get("financial_status") == "paid":
			create_sales_invoice(order, setting, so)

		if order.get("fulfillments"):
			create_delivery_note(order, setting, so)


def create_sales_order(shopify_order, setting, company=None):
	customer = setting.default_customer
	if shopify_order.get("customer", {}):
		if customer_id := shopify_order.get("customer", {}).get("id"):
			customer = frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: customer_id}, "name")

	so = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: shopify_order.get("id")}, "name")

	if not so:
		# B9: Separate tips from regular line items
		line_items, tip_total = _separate_tips(shopify_order.get("line_items", []))

		items = get_order_items(
			line_items,
			setting,
			getdate(shopify_order.get("created_at")),
			taxes_inclusive=shopify_order.get("taxes_included"),
		)

		if not items:
			message = (
				"Following items exists in the shopify order but relevant records were"
				" not found in the shopify Product master"
			)
			product_not_exists = []  # TODO: fix missing items
			message += "\n" + ", ".join(product_not_exists)

			create_shopify_log(status="Error", exception=message, rollback=True)

			return ""

		# B1: Extract order tags
		order_tags = shopify_order.get("tags", "")

		# B8: Extract discount code names
		discount_codes = shopify_order.get("discount_codes", [])
		discount_code_names = ", ".join(dc.get("code", "") for dc in discount_codes) if discount_codes else ""

		taxes = get_order_taxes(shopify_order, setting, items)
		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"naming_series": setting.sales_order_series or "SO-Shopify-",
				ORDER_ID_FIELD: str(shopify_order.get("id")),
				ORDER_NUMBER_FIELD: shopify_order.get("name"),
				ORDER_STATUS_FIELD: order_tags,  # B1: order tags
				ORDER_FINANCIAL_STATUS_FIELD: shopify_order.get("financial_status", ""),  # B10
				ORDER_FULFILLMENT_STATUS_FIELD: shopify_order.get("fulfillment_status", ""),  # B10
				ORDER_DISCOUNT_CODES_FIELD: discount_code_names,  # B8
				ORDER_TIP_AMOUNT_FIELD: tip_total,  # B9
				"customer": customer,
				"transaction_date": getdate(shopify_order.get("created_at")) or nowdate(),
				"delivery_date": getdate(shopify_order.get("created_at")) or nowdate(),
				"company": setting.company,
				"selling_price_list": get_dummy_price_list(),
				"ignore_pricing_rule": 1,
				"items": items,
				"taxes": taxes,
				"tax_category": get_dummy_tax_category(),
			}
		)

		if company:
			so.update({"company": company, "status": "Draft"})
		so.flags.ignore_mandatory = True
		so.flags.shopiy_order_json = json.dumps(shopify_order)
		so.save(ignore_permissions=True)
		so.submit()

		if shopify_order.get("note"):
			so.add_comment(text=f"Order Note: {shopify_order.get('note')}")

	else:
		so = frappe.get_doc("Sales Order", so)

	return so


def _separate_tips(line_items):
	"""B9: Separate tip line items from regular line items.

	Tips arrive as line items with title "Tip". Extract them, sum the amount,
	and return only non-tip items for SO creation.
	"""
	regular_items = []
	tip_total = 0.0

	for item in line_items:
		if cstr(item.get("title")).strip().lower() == "tip":
			tip_total += flt(item.get("price", 0)) * cint(item.get("quantity", 1))
		else:
			regular_items.append(item)

	return regular_items, tip_total


def get_order_items(order_items, setting, delivery_date, taxes_inclusive):
	items = []

	for shopify_item in order_items:
		item_code = None

		if shopify_item.get("product_exists") and shopify_item.get("product_id"):
			item_code = get_item_code(shopify_item)

		# B12: Fallback to MISC-MANUAL for unmatched items
		if not item_code:
			item_code = UNMATCHED_ITEM_CODE
			# Ensure the MISC-MANUAL item exists
			_ensure_misc_manual_item(setting)

		# B2: Extract line item properties (manufacturing instructions)
		properties = shopify_item.get("properties", [])
		properties_json = json.dumps(properties) if properties else ""

		# B7: Resolve shipping method from product tags
		shipping_method = _resolve_shipping_method(shopify_item)

		item_row = {
			"item_code": item_code,
			"item_name": shopify_item.get("name") or shopify_item.get("title"),
			"rate": _get_item_price(shopify_item, taxes_inclusive),
			"delivery_date": delivery_date,
			"qty": shopify_item.get("quantity"),
			"stock_uom": shopify_item.get("uom") or "Nos",
			"warehouse": setting.warehouse,
			ORDER_ITEM_DISCOUNT_FIELD: (
				_get_total_discount(shopify_item) / cint(shopify_item.get("quantity"))
			),
			ORDER_ITEM_PROPERTIES_FIELD: properties_json,  # B2
			ORDER_ITEM_SHIPPING_METHOD_FIELD: shipping_method,  # B7
		}

		# B12: Store original title in description for unmatched items
		if item_code == UNMATCHED_ITEM_CODE:
			item_row["description"] = (
				f"[UNMATCHED] {shopify_item.get('title', '')} "
				f"(Shopify product_id: {shopify_item.get('product_id', 'N/A')}, "
				f"variant_id: {shopify_item.get('variant_id', 'N/A')})"
			)

		items.append(item_row)

	return items


def _ensure_misc_manual_item(setting):
	"""B12: Create the MISC-MANUAL catch-all item if it doesn't exist."""
	if not frappe.db.exists("Item", UNMATCHED_ITEM_CODE):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": UNMATCHED_ITEM_CODE,
				"item_name": "Unmatched Shopify Line Item",
				"description": "Placeholder for Shopify line items that could not be matched to an ERPNext Item. Requires manual resolution.",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"default_warehouse": setting.warehouse,
			}
		).insert(ignore_permissions=True)


def _resolve_shipping_method(shopify_item):
	"""B7: Determine ship-sea or ship-air from the product's tags.

	Looks up the ERPNext Item linked to this Shopify product and reads
	the shopify_tags custom field. Returns 'ship-sea', 'ship-air', or ''.
	"""
	product_id = shopify_item.get("product_id")
	if not product_id:
		return ""

	# Look up Ecommerce Item → ERPNext Item → shopify_tags
	from ecommerce_integrations.shopify.constants import ITEM_TAGS_FIELD

	erpnext_item_code = frappe.db.get_value(
		"Ecommerce Item",
		{"integration": "shopify", "integration_item_code": str(product_id)},
		"erpnext_item_code",
	)
	if not erpnext_item_code:
		return ""

	tags = frappe.db.get_value("Item", erpnext_item_code, ITEM_TAGS_FIELD) or ""
	tags_lower = tags.lower()

	if "ship-sea" in tags_lower:
		return "ship-sea"
	elif "ship-air" in tags_lower:
		return "ship-air"
	return ""


def _get_item_price(line_item, taxes_inclusive: bool) -> float:
	price = flt(line_item.get("price"))
	qty = cint(line_item.get("quantity"))

	# remove line item level discounts
	total_discount = _get_total_discount(line_item)

	if not taxes_inclusive:
		return price - (total_discount / qty)

	total_taxes = 0.0
	for tax in line_item.get("tax_lines"):
		total_taxes += flt(tax.get("price"))

	return price - (total_taxes + total_discount) / qty


def _get_total_discount(line_item) -> float:
	discount_allocations = line_item.get("discount_allocations") or []
	return sum(flt(discount.get("amount")) for discount in discount_allocations)


def get_order_taxes(shopify_order, setting, items):
	taxes = []
	line_items = shopify_order.get("line_items")

	for line_item in line_items:
		item_code = get_item_code(line_item)
		for tax in line_item.get("tax_lines"):
			taxes.append(
				{
					"charge_type": "Actual",
					"account_head": get_tax_account_head(tax, charge_type="sales_tax"),
					"description": (
						get_tax_account_description(tax)
						or f"{tax.get('title')} - {tax.get('rate') * 100.0:.2f}%"
					),
					"tax_amount": tax.get("price"),
					"included_in_print_rate": 0,
					"cost_center": setting.cost_center,
					"item_wise_tax_detail": {item_code: [flt(tax.get("rate")) * 100, flt(tax.get("price"))]},
					"dont_recompute_tax": 1,
				}
			)

	update_taxes_with_shipping_lines(
		taxes,
		shopify_order.get("shipping_lines"),
		setting,
		items,
		taxes_inclusive=shopify_order.get("taxes_included"),
	)

	if cint(setting.consolidate_taxes):
		taxes = consolidate_order_taxes(taxes)

	for row in taxes:
		tax_detail = row.get("item_wise_tax_detail")
		if isinstance(tax_detail, dict):
			row["item_wise_tax_detail"] = json.dumps(tax_detail)

	return taxes


def consolidate_order_taxes(taxes):
	tax_account_wise_data = {}
	for tax in taxes:
		account_head = tax["account_head"]
		tax_account_wise_data.setdefault(
			account_head,
			{
				"charge_type": "Actual",
				"account_head": account_head,
				"description": tax.get("description"),
				"cost_center": tax.get("cost_center"),
				"included_in_print_rate": 0,
				"dont_recompute_tax": 1,
				"tax_amount": 0,
				"item_wise_tax_detail": {},
			},
		)
		tax_account_wise_data[account_head]["tax_amount"] += flt(tax.get("tax_amount"))
		if tax.get("item_wise_tax_detail"):
			tax_account_wise_data[account_head]["item_wise_tax_detail"].update(tax["item_wise_tax_detail"])

	return tax_account_wise_data.values()


def get_tax_account_head(tax, charge_type: Literal["shipping", "sales_tax"] | None = None):
	tax_title = str(tax.get("title"))

	tax_account = frappe.db.get_value(
		"Shopify Tax Account",
		{"parent": SETTING_DOCTYPE, "shopify_tax": tax_title},
		"tax_account",
	)

	if not tax_account and charge_type:
		tax_account = frappe.db.get_single_value(SETTING_DOCTYPE, DEFAULT_TAX_FIELDS[charge_type])

	if not tax_account:
		frappe.throw(_("Tax Account not specified for Shopify Tax {0}").format(tax.get("title")))

	return tax_account


def get_tax_account_description(tax):
	tax_title = tax.get("title")

	tax_description = frappe.db.get_value(
		"Shopify Tax Account",
		{"parent": SETTING_DOCTYPE, "shopify_tax": tax_title},
		"tax_description",
	)

	return tax_description


def update_taxes_with_shipping_lines(taxes, shipping_lines, setting, items, taxes_inclusive=False):
	"""Shipping lines represents the shipping details,
	each such shipping detail consists of a list of tax_lines"""
	shipping_as_item = cint(setting.add_shipping_as_item) and setting.shipping_item
	for shipping_charge in shipping_lines:
		if shipping_charge.get("price"):
			shipping_discounts = shipping_charge.get("discount_allocations") or []
			total_discount = sum(flt(discount.get("amount")) for discount in shipping_discounts)

			shipping_taxes = shipping_charge.get("tax_lines") or []
			total_tax = sum(flt(discount.get("price")) for discount in shipping_taxes)

			shipping_charge_amount = flt(shipping_charge["price"]) - flt(total_discount)
			if bool(taxes_inclusive):
				shipping_charge_amount -= total_tax

			if shipping_as_item:
				items.append(
					{
						"item_code": setting.shipping_item,
						"rate": shipping_charge_amount,
						"delivery_date": items[-1]["delivery_date"] if items else nowdate(),
						"qty": 1,
						"stock_uom": "Nos",
						"warehouse": setting.warehouse,
					}
				)
			else:
				taxes.append(
					{
						"charge_type": "Actual",
						"account_head": get_tax_account_head(shipping_charge, charge_type="shipping"),
						"description": get_tax_account_description(shipping_charge)
						or shipping_charge["title"],
						"tax_amount": shipping_charge_amount,
						"cost_center": setting.cost_center,
					}
				)

		for tax in shipping_charge.get("tax_lines"):
			taxes.append(
				{
					"charge_type": "Actual",
					"account_head": get_tax_account_head(tax, charge_type="sales_tax"),
					"description": (
						get_tax_account_description(tax)
						or f"{tax.get('title')} - {tax.get('rate') * 100.0:.2f}%"
					),
					"tax_amount": tax["price"],
					"cost_center": setting.cost_center,
					"item_wise_tax_detail": {
						setting.shipping_item: [flt(tax.get("rate")) * 100, flt(tax.get("price"))]
					}
					if shipping_as_item
					else {},
					"dont_recompute_tax": 1,
				}
			)


def get_sales_order(order_id):
	"""Get ERPNext sales order using shopify order id."""
	sales_order = frappe.db.get_value("Sales Order", filters={ORDER_ID_FIELD: order_id})
	if sales_order:
		return frappe.get_doc("Sales Order", sales_order)


def cancel_order(payload, request_id=None):
	"""Called by order/cancelled event.

	When shopify order is cancelled there could be many different someone handles it.

	Updates document with custom field showing order status.

	IF sales invoice / delivery notes are not generated against an order, then cancel it.
	"""
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	order = payload

	try:
		order_id = order["id"]
		order_status = order["financial_status"]

		sales_order = get_sales_order(order_id)

		if not sales_order:
			create_shopify_log(status="Invalid", message="Sales Order does not exist")
			return

		sales_invoice = frappe.db.get_value("Sales Invoice", filters={ORDER_ID_FIELD: order_id})
		delivery_notes = frappe.db.get_list("Delivery Note", filters={ORDER_ID_FIELD: order_id})

		if sales_invoice:
			frappe.db.set_value("Sales Invoice", sales_invoice, ORDER_STATUS_FIELD, order_status)

		for dn in delivery_notes:
			frappe.db.set_value("Delivery Note", dn.name, ORDER_STATUS_FIELD, order_status)

		if not sales_invoice and not delivery_notes and sales_order.docstatus == 1:
			sales_order.cancel()
		else:
			frappe.db.set_value("Sales Order", sales_order.name, ORDER_STATUS_FIELD, order_status)

	except Exception as e:
		create_shopify_log(status="Error", exception=e)
	else:
		create_shopify_log(status="Success")


def handle_order_edited(payload, request_id=None):
	"""B13: Handle orders/edited webhook.

	Compares the incoming edited order against the existing ERPNext Sales Order.
	Creates a ToDo (task) for the operator with a human-readable diff.
	Does NOT auto-modify the SO — operator decides the action.

	Covers: warranty parts added, quantity changes, price adjustments,
	item removals, address changes.
	"""
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	order = payload
	try:
		order_id = order.get("id")
		sales_order = get_sales_order(order_id)

		if not sales_order:
			create_shopify_log(
				status="Invalid",
				message=f"Order edited webhook received but SO not found for Shopify order {order_id}",
			)
			return

		# Build a diff summary
		diff_lines = _build_order_edit_diff(sales_order, order)

		if not diff_lines:
			create_shopify_log(status="Success", message="Order edited but no material changes detected")
			return

		# Create a ToDo for the operator
		diff_text = "\n".join(diff_lines)
		todo_description = (
			f"<b>Shopify Order Edited: {order.get('name', order_id)}</b><br><br>"
			f"<b>Sales Order:</b> {sales_order.name}<br>"
			f"<b>Customer:</b> {sales_order.customer}<br><br>"
			f"<b>Changes detected:</b><br><pre>{diff_text}</pre><br><br>"
			f"<b>Action required:</b> Review and decide whether to amend the SO, "
			f"create a new SO, or ignore."
		)

		frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": todo_description,
				"reference_type": "Sales Order",
				"reference_name": sales_order.name,
				"allocated_to": frappe.db.get_single_value(SETTING_DOCTYPE, "owner") or "Administrator",
				"priority": "Medium",
			}
		).insert(ignore_permissions=True)

		# Update order tags/status if changed
		new_tags = order.get("tags", "")
		if new_tags != (sales_order.get(ORDER_STATUS_FIELD) or ""):
			frappe.db.set_value("Sales Order", sales_order.name, ORDER_STATUS_FIELD, new_tags)

		# Update financial/fulfillment status
		frappe.db.set_value(
			"Sales Order",
			sales_order.name,
			{
				ORDER_FINANCIAL_STATUS_FIELD: order.get("financial_status", ""),
				ORDER_FULFILLMENT_STATUS_FIELD: order.get("fulfillment_status", ""),
			},
		)

	except Exception as e:
		create_shopify_log(status="Error", exception=e)
	else:
		create_shopify_log(status="Success")


def _build_order_edit_diff(sales_order, shopify_order):
	"""Compare existing SO items against the edited Shopify order line items."""
	diff_lines = []

	# Build lookup of current SO items by item_code
	so_items = {}
	for item in sales_order.items:
		key = item.item_code
		so_items.setdefault(key, []).append(item)

	# Build lookup of Shopify line items by title (more human-readable)
	shopify_items = {}
	for li in shopify_order.get("line_items", []):
		title = li.get("title", "Unknown")
		shopify_items.setdefault(title, []).append(li)

	# Detect new line items (by title, rough comparison)
	existing_titles = {item.item_name for item in sales_order.items}
	for title, items in shopify_items.items():
		if title not in existing_titles:
			for li in items:
				price = li.get("price", "0")
				qty = li.get("quantity", 1)
				diff_lines.append(f"+ NEW ITEM: {title} (qty: {qty}, price: ${price})")

	# Detect quantity/price changes for existing items
	for item in sales_order.items:
		matching = [
			li for li in shopify_order.get("line_items", [])
			if li.get("title") == item.item_name or li.get("sku") == item.item_code
		]
		for li in matching:
			new_qty = cint(li.get("quantity"))
			new_price = flt(li.get("price"))
			if new_qty != cint(item.qty):
				diff_lines.append(
					f"~ QTY CHANGE: {item.item_name} — {cint(item.qty)} → {new_qty}"
				)
			if abs(new_price - flt(item.rate)) > 0.01:
				diff_lines.append(
					f"~ PRICE CHANGE: {item.item_name} — ${flt(item.rate):.2f} → ${new_price:.2f}"
				)

	# Detect removed items
	shopify_titles = set()
	shopify_skus = set()
	for li in shopify_order.get("line_items", []):
		shopify_titles.add(li.get("title"))
		if li.get("sku"):
			shopify_skus.add(li.get("sku"))

	for item in sales_order.items:
		if item.item_name not in shopify_titles and item.item_code not in shopify_skus:
			diff_lines.append(f"- REMOVED: {item.item_name} (was qty: {cint(item.qty)})")

	# Check address changes
	shipping_addr = shopify_order.get("shipping_address", {})
	if shipping_addr:
		addr_str = f"{shipping_addr.get('address1', '')}, {shipping_addr.get('city', '')}, {shipping_addr.get('province', '')} {shipping_addr.get('zip', '')}"
		diff_lines.append(f"  SHIPPING ADDRESS: {addr_str}")

	return diff_lines


@temp_shopify_session
def sync_old_orders():
	shopify_setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not cint(shopify_setting.sync_old_orders):
		return

	orders = _fetch_old_orders(shopify_setting.old_orders_from, shopify_setting.old_orders_to)

	for order in orders:
		log = create_shopify_log(
			method=EVENT_MAPPER["orders/create"], request_data=json.dumps(order), make_new=True
		)
		sync_sales_order(order, request_id=log.name)

	shopify_setting = frappe.get_doc(SETTING_DOCTYPE)
	shopify_setting.sync_old_orders = 0
	shopify_setting.save()


def _fetch_old_orders(from_time, to_time):
	"""Fetch all shopify orders in specified range and return an iterator on fetched orders."""

	from_time = get_datetime(from_time).astimezone().isoformat()
	to_time = get_datetime(to_time).astimezone().isoformat()
	orders_iterator = PaginatedIterator(
		Order.find(created_at_min=from_time, created_at_max=to_time, limit=250)
	)

	for orders in orders_iterator:
		for order in orders:
			# Using generator instead of fetching all at once is better for
			# avoiding rate limits and reducing resource usage.
			yield order.to_dict()
