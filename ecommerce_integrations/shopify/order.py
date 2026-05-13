import datetime
import json
import re
from typing import Literal, Optional

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate, nowdate
from shopify.collection import PaginatedIterator
from shopify.resources import Order

from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import (
	ADDRESS_ID_FIELD,
	CUSTOMER_ID_FIELD,
	EVENT_MAPPER,
	ITEM_SHIP_METHOD_FIELD,
	LINE_ITEM_ID_FIELD,
	ORDER_DISCOUNT_CODES_FIELD,
	ORDER_FINANCIAL_STATUS_FIELD,
	ORDER_FULFILLMENT_SOURCE_FIELD,
	ORDER_FULFILLMENT_STATUS_FIELD,
	ORDER_ID_FIELD,
	ORDER_ITEM_PROPERTIES_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	ORDER_TIP_AMOUNT_FIELD,
	SETTING_DOCTYPE,
	SO_SHIP_CLASS_FIELD,
	UNMATCHED_ITEM_CODE,
)
from ecommerce_integrations.shopify.freight_class import (
	make_live_fetcher,
	resolve_for_order,
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

		# B17: compute delivery_date from shipping_lines titles (upper-bound
		# business days or explicit preorder date), falling back to order_date.
		order_date = getdate(shopify_order.get("created_at")) or nowdate()
		delivery_date = _resolve_delivery_date(shopify_order, fallback=order_date)

		items = get_order_items(
			line_items,
			setting,
			delivery_date,
			taxes_inclusive=shopify_order.get("taxes_included"),
		)

		# B23: stamp shopify_freight_class per line + SO-level rollup.
		# Lives here (not in get_order_items) so the per-call product
		# fetcher cache spans the whole order — duplicate product lines
		# don't double-hit Shopify.
		freight_per_line, freight_rollup = resolve_for_order(
			{"line_items": line_items}, make_live_fetcher()
		)
		for idx, item_row in enumerate(items):
			if idx < len(freight_per_line):
				cls = freight_per_line[idx]
				# Wave B (2026-05-12): write ONLY ITEM_SHIP_METHOD_FIELD
				# with `ship-` prefix. Legacy FREIGHT_CLASS_FIELD on SO Item
				# is no longer written; Custom Field row stays in DB until
				# Wave C deletes it.
				# Transform: air→ship-air, sea→ship-sea, dropship→ship-dropship.
				item_row[ITEM_SHIP_METHOD_FIELD] = f"ship-{cls}" if cls else ""

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

		# 2026-05-13 Issue #1 — per-order shipping Address record.
		# Customer.shipping_address (Billing) is the payer name; the
		# order-level shipping_address is the ship-to recipient. Create
		# a NEW Address record per SO from order.shipping_address so
		# Address.address_title = recipient_first + " " + recipient_last
		# (not customer billing name). Falls back to Customer's primary
		# Address if the order has no shipping_address (legacy POS,
		# pickup orders, etc.).
		per_order_ship_addr = _create_per_order_shipping_address(
			shopify_order, customer,
		)

		so_dict = {
			"doctype": "Sales Order",
			"naming_series": setting.sales_order_series or "SO-Shopify-",
			ORDER_ID_FIELD: str(shopify_order.get("id")),
			ORDER_NUMBER_FIELD: shopify_order.get("name"),
			ORDER_STATUS_FIELD: order_tags,  # B1: order tags
			ORDER_FINANCIAL_STATUS_FIELD: shopify_order.get("financial_status") or "",  # B10
			ORDER_FULFILLMENT_STATUS_FIELD: shopify_order.get("fulfillment_status") or "",  # B10
			ORDER_DISCOUNT_CODES_FIELD: discount_code_names,  # B8
			ORDER_TIP_AMOUNT_FIELD: tip_total,  # B9
			SO_SHIP_CLASS_FIELD: freight_rollup,  # B23 (Wave B — sole writer)
			"customer": customer,
			"transaction_date": order_date,
			"delivery_date": delivery_date,  # B17
			"company": setting.company,
			"selling_price_list": get_dummy_price_list(),
			"ignore_pricing_rule": 1,
			"items": items,
			"taxes": taxes,
			"tax_category": get_dummy_tax_category(),
		}
		if per_order_ship_addr:
			so_dict["shipping_address_name"] = per_order_ship_addr
		# B18: use Shopify order currency verbatim (USD for US store).
		# conversion_rate is intentionally not set — ERPNext handles FX at SI time.
		currency = _resolve_currency(shopify_order)
		if currency:
			so_dict["currency"] = currency
		so = frappe.get_doc(so_dict)

		if company:
			so.update({"company": company, "status": "Draft"})

		# B16: detect dropship — if any line is tagged ship-dropship, mark
		# the SO so downstream (reporting, FedEx skip) can branch on a
		# single field.
		# Wave B (2026-05-12): reader flipped to ITEM_SHIP_METHOD_FIELD
		# (the new field set by freight_per_line above). The old
		# ORDER_ITEM_SHIPPING_METHOD_FIELD is no longer the source of truth.
		has_dropship = any(
			item.get(ITEM_SHIP_METHOD_FIELD) == "ship-dropship"
			for item in items
		)
		so.update(
			{ORDER_FULFILLMENT_SOURCE_FIELD: "dropship" if has_dropship else "warehouse"}
		)

		so.flags.ignore_mandatory = True
		so.flags.shopiy_order_json = json.dumps(shopify_order)
		so.save(ignore_permissions=True)
		so.submit()

		# B19: rename to SH-YYYY-NNNNN for cross-platform lookup with Shopify.
		# Done post-submit so SI/DN created afterward reference the new name
		# via Frappe's link-table cascade. Non-fatal if rename fails — the SO
		# still exists under its naming_series default.
		desired_name = _format_shopify_so_name(shopify_order)
		if desired_name and so.name != desired_name:
			try:
				frappe.rename_doc("Sales Order", so.name, desired_name, force=True, merge=False)
				so = frappe.get_doc("Sales Order", desired_name)
			except Exception as rename_err:
				frappe.log_error(
					title="B19 Shopify SO rename failed",
					message=f"SO {so.name} → {desired_name}: {rename_err}",
				)

		if shopify_order.get("note"):
			so.add_comment(text=f"Order Note: {shopify_order.get('note')}")

	else:
		so = frappe.get_doc("Sales Order", so)

	return so


_RE_BIZ_DAYS = re.compile(r'\((\d+)\s*-\s*(\d+)\s+business days\)', re.IGNORECASE)
_RE_EXPLICIT_DATE = re.compile(
	r'Estimated (?:to be Delivered|Delivery by)\s+([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?',
	re.IGNORECASE,
)


def _add_business_days(start_date, days):
	"""Add N business days (Mon-Fri) to a date, skipping weekends."""
	current = start_date
	added = 0
	while added < days:
		current = current + datetime.timedelta(days=1)
		if current.weekday() < 5:
			added += 1
	return current


def _parse_month_day(month_str, day_str, year):
	"""Parse 'May 20' (or 'January 5') to a date in the given year."""
	for fmt in ("%B %d %Y", "%b %d %Y"):
		try:
			return datetime.datetime.strptime(
				f"{month_str} {day_str} {year}", fmt
			).date()
		except ValueError:
			continue
	return None


def _resolve_delivery_date(shopify_order, fallback):
	"""B17: delivery_date from shipping_lines titles — take max of all parsed dates.

	Handles two shapes seen in live data:
	  (a) "(X - Y business days)" → order_date + Y business days
	  (b) "Estimated (to be Delivered|Delivery by) <Mon> <Day>" → explicit date

	Across multiple shipping_lines (mixed carts), take LATER date. Returns
	`fallback` (typically order_date) if nothing parses — avoids crash at
	the cost of keeping the Overdue issue for that one SO.
	"""
	created_at = (shopify_order.get("created_at") or "")[:10]
	try:
		order_date = datetime.datetime.strptime(created_at, "%Y-%m-%d").date()
	except ValueError:
		return fallback

	candidates = []
	for line in shopify_order.get("shipping_lines") or []:
		title = line.get("title") or ""

		m_biz = _RE_BIZ_DAYS.search(title)
		if m_biz:
			upper_days = int(m_biz.group(2))
			candidates.append(_add_business_days(order_date, upper_days))
			continue

		m_date = _RE_EXPLICIT_DATE.search(title)
		if m_date:
			d = _parse_month_day(m_date.group(1), m_date.group(2), order_date.year)
			if d is None:
				continue
			if d < order_date:
				d = d.replace(year=order_date.year + 1)
			candidates.append(d)

	if candidates:
		return max(candidates)
	return fallback


def _format_shopify_so_name(shopify_order):
	"""B19: Build SO name for Shopify-sourced orders: SH-YYYY-NNNNN.

	Year is derived from created_at (not sync time) so orders keep their
	year-prefix across year boundaries. order_number is zero-padded to 5
	digits; wider numbers don't truncate. Returns None on malformed data
	— caller keeps naming_series default.
	"""
	order_number = shopify_order.get("order_number")
	if order_number is None or order_number == "":
		return None
	try:
		on_int = int(order_number)
	except (ValueError, TypeError):
		return None
	created_at = shopify_order.get("created_at", "") or ""
	year = created_at[:4] if len(created_at) >= 4 else ""
	if not year.isdigit():
		return None
	return f"SH-{year}-{on_int:05d}"


def _resolve_currency(shopify_order):
	"""B18: Return Shopify order currency (e.g. 'USD'), or None.

	None causes caller to omit the field so ERPNext falls back to
	Customer/Company default. conversion_rate is intentionally NOT set
	here — ERPNext applies current FX at Sales Invoice time, avoiding
	stale rates baked into the SO.
	"""
	c = shopify_order.get("currency")
	if not c or not isinstance(c, str):
		return None
	c = c.strip().upper()
	return c or None


def _create_per_order_shipping_address(shopify_order, customer_name):
	"""2026-05-13 Issue #1 — create a per-order Shipping Address record.

	Address.address_title = order shipping_address.first_name + last_name
	(the RECIPIENT name — what the supplier ships to). This differs from
	the customer-level Billing Address whose title is the payer name.

	Returns the new Address.name on success, ``None`` when the order has
	no shipping_address (e.g. pickup, POS) — caller falls back to
	Frappe's default (Customer's primary Address).

	The Address record uses Frappe's default autoname (per D1 in the
	consolidated design: supplier sheet col D renders `address_title` +
	address fields, never the docname). Linked to the customer via
	dynamic-link so it appears under the Customer's addresses.
	"""
	ship = shopify_order.get("shipping_address") or {}
	if not ship or not customer_name:
		return None

	# Build recipient name from shipping_address.first_name + last_name.
	# If both empty, fall back to whatever the customer record carries
	# (e.g. "Default Customer" for guest orders).
	first = cstr(ship.get("first_name")).strip()
	last = cstr(ship.get("last_name")).strip()
	recipient = (f"{first} {last}").strip() or customer_name

	addr_doc = {
		"doctype": "Address",
		"address_title": recipient,
		"address_type": "Shipping",
		ADDRESS_ID_FIELD: ship.get("id"),
		"address_line1": ship.get("address1") or "Address 1",
		"address_line2": ship.get("address2"),
		"city": ship.get("city"),
		"state": ship.get("province"),
		"pincode": ship.get("zip"),
		"country": ship.get("country"),
		"links": [{"link_doctype": "Customer", "link_name": customer_name}],
	}
	phone = ship.get("phone")
	if phone:
		addr_doc["phone"] = phone

	try:
		doc = frappe.get_doc(addr_doc)
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception as e:  # noqa: BLE001
		# Non-fatal — SO creation continues with Customer's primary
		# Address (the same fallback the connector used pre-fix).
		frappe.log_error(
			title="Issue #1 per-order Address create failed",
			message=f"shopify_order={shopify_order.get('id')}: {e}",
		)
		return None


def _separate_tips(line_items):
	"""B9: Separate tip line items from regular line items.

	Tips arrive as line items with title "Tip". Extract them, sum the amount,
	and return only non-tip items for SO creation.
	"""
	regular_items = []
	tip_total = 0.0

	for item in line_items:
		if cstr(item.get("title")).strip().lower() == "tip":
			# B24b: current_quantity is authoritative post-refund. Refunded
			# tips contribute 0; fall back to quantity for older payloads.
			qty = cint(item.get("current_quantity", item.get("quantity", 1)))
			tip_total += flt(item.get("price", 0)) * qty
		else:
			regular_items.append(item)

	return regular_items, tip_total


def get_order_items(order_items, setting, delivery_date, taxes_inclusive):
	items = []

	for shopify_item in order_items:
		# B24b: skip lines fully refunded at order creation. Shopify's
		# `current_quantity` is the authoritative post-refund qty; 0 means
		# the line was removed. New orders must not insert SO Items for
		# these lines. Fall back to `quantity` for older API responses.
		current_qty = cint(
			shopify_item.get("current_quantity", shopify_item.get("quantity", 1))
		)
		if current_qty == 0:
			continue

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

		# B7 retired Wave B: shipping_method was derived from Item.shopify_tags
		# via _resolve_shipping_method and written to ORDER_ITEM_SHIPPING_METHOD_FIELD.
		# Post-Wave-B the per-line ITEM_SHIP_METHOD_FIELD (set by freight_per_line
		# above from live Shopify product tags) is the sole source of truth.

		# B15: set rate AND price_list_rate to the SAME discounted dollar
		# value. Shopify's model is dollar amounts (discount_allocations),
		# not percentages — preserve that intent.
		#
		# Why set price_list_rate at all?  ERPNext's server-side
		# save()+submit() reconciles rate whenever price_list_rate differs
		# from the caller-supplied rate (confirmed via direct test: rate=0
		# alone survives, but rate=0 + plr=34.80 resets rate to 34.80).
		# ERPNext's `set_missing_values` pipeline can populate plr during
		# save from sources the connector can't easily predict. By setting
		# plr=rate explicitly, we short-circuit reconciliation — plr and
		# rate match, no override.
		# B24b: qty already resolved from current_quantity (with quantity
		# fallback) above; the current_qty == 0 guard means dividing by
		# it is safe without the `or 1` belt-and-braces.
		price = flt(shopify_item.get("price"))
		qty = current_qty
		total_discount = _get_total_discount(shopify_item)
		per_unit_discount = total_discount / qty

		if taxes_inclusive:
			per_unit_tax = sum(
				flt(tax.get("price")) for tax in (shopify_item.get("tax_lines") or [])
			) / qty
		else:
			per_unit_tax = 0.0

		effective_rate = price - per_unit_tax - per_unit_discount  # Shopify's dollars, net-of-tax, post-discount

		# B21: dollar-amount discount is stored natively on SOI via rate +
		# price_list_rate (both = effective_rate, prevents ERPNext
		# reconciliation). The old `shopify_item_discount` snapshot field
		# is retired — native discount_amount on SOI already represents
		# the same information at submit time if needed for reporting.
		# 2026-05-13 supplier-sheet-3-issues: stamp `shopify_line_item_id`
		# per row so refund.py:_match_so_item can disambiguate multi-line
		# SOs that share an ERPNext item_code (e.g. ship-air + ship-sea
		# variants of the same greenhouse). Stored as string — Shopify's
		# 64-bit ids exceed JS-safe integer range.
		line_item_id = shopify_item.get("id")
		item_row = {
			"item_code": item_code,
			"item_name": shopify_item.get("name") or shopify_item.get("title"),
			"rate": effective_rate,
			"price_list_rate": effective_rate,
			"delivery_date": delivery_date,
			"qty": qty,
			"stock_uom": shopify_item.get("uom") or "Nos",
			"warehouse": setting.warehouse,
			ORDER_ITEM_PROPERTIES_FIELD: properties_json,  # B2
			LINE_ITEM_ID_FIELD: str(line_item_id) if line_item_id is not None else "",
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


def _get_item_price(line_item, taxes_inclusive: bool) -> float:
	price = flt(line_item.get("price"))
	# B24b: current_quantity is authoritative post-refund.
	qty = cint(line_item.get("current_quantity", line_item.get("quantity")))

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
	"""Called by ``orders/cancelled`` webhook.

	2026-05-13 Issue #3 — single-signal discipline. ``SO.docstatus == 2``
	is the canonical "should the supplier ship / produce this?" gate
	(read by ygf writer for col A "Cancelled" propagation). The legacy
	guard suppressed ``.cancel()`` whenever a DN existed, leaving most
	cancellations stuck at docstatus=1 and the supplier-sheet col A on
	"New". Lifted here.

	Dead-code purge: the legacy ``frappe.db.set_value`` writes onto
	Sales Invoice and Delivery Note ``shopify_order_status`` had zero
	readers in ygf (verified by missed-audit 2026-05-12) and SI flow
	is disabled (``sync_sales_invoice=0``). Removed.

	``shopify_order_status`` is still updated on the SO as a Shopify
	Tags / financial-state mirror (diagnostic only — NOT consumed by
	cancellation decisions).
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

		# Demoted financial-mirror write — keep so the field stays in
		# sync with Shopify's current financial_status even when
		# ``.cancel()`` raises below.
		#
		# yei-v1.3.3 Patch 3: ``update_modified=False`` so this write
		# doesn't bump SO.modified on the DB row. Without this, the
		# in-memory ``sales_order`` object held by this function still
		# carries the OLD timestamp; ``sales_order.cancel()`` then trips
		# ``TimestampMismatchError`` via Frappe's ``check_if_latest``,
		# which used to be caught by the ValidationError clause below
		# and misreported as "FedEx AWB" / "manual intervention required"
		# (8 EIL Error rows on 2026-05-13). The field is a diagnostic
		# mirror of Shopify's financial_status; no downstream code keys
		# on its modified timestamp.
		frappe.db.set_value(
			"Sales Order", sales_order.name, ORDER_STATUS_FIELD, order_status,
			update_modified=False,
		)

		if sales_order.docstatus == 1:
			try:
				# alpha26 hook (``ygh_fedex.split.cancel_dns_or_refuse``)
				# cascades DN.cancel() and refuses if any DN has lr_no
				# (FedEx AWB minted). The cascade raises
				# ``frappe.ValidationError`` per D6 with "FedEx AWB" in
				# the message text.
				sales_order.cancel()
			except frappe.ValidationError as e:
				# yei-v1.3.3 Patch 3: tighten the except to genuinely
				# match the AWB-blocking case ("FedEx AWB" in the
				# cascade's frappe.throw message). Any other
				# ValidationError (e.g. lingering TimestampMismatch from
				# concurrent writes) propagates to the outer Exception
				# handler so it surfaces as a real Error with diagnostic
				# detail rather than the misleading "manual intervention
				# required" label.
				if "FedEx AWB" not in str(e):
					raise
				# True D6 edge: DN has AWB → package en route. Customer-
				# service / refund territory; SO stays at docstatus=1
				# deliberately.
				create_shopify_log(
					status="Error",
					message=(
						f"Shopify cancel cannot propagate to SO {sales_order.name}: "
						f"manual intervention required ({e})"
					),
				)
				return

	except Exception as e:
		create_shopify_log(status="Error", exception=e)
	else:
		create_shopify_log(status="Success")


def handle_order_edited(payload, request_id=None):
	"""B13 / yei-v1.3.3: Handle ``orders/edited`` webhook.

	Shopify's ``orders/edited`` payload nests under
	``payload["order_edit"]["order_id"]`` — NOT a top-level ``id``. Prior
	versions read ``payload.get("id")`` (None) and silently dropped every
	line addition (136 EIL Invalid rows since 2026-04-17). This rewrite:

	* Extracts ``order_id`` from the correct nested key (defensive fallback
	  to top-level ``id`` for flat replay payloads / older API versions).
	* Reconciles ``Sales Order.items`` against the current Shopify line
	  items: inserts SOIs for additions; flags ``current_quantity == 0``
	  lines as refunded via the existing ``flag_so_item_refunded`` helper.
	* Falls back to a diff-only ToDo when no actionable changes detected
	  (legacy audit-trail behaviour preserved for ops review).

	The webhook delta only carries ``{additions, removals}`` of
	line_item_ids — no SKUs/prices. Full reconciliation requires a Shopify
	REST fetch of the current order, then a diff by ``shopify_line_item_id``
	(stamped on every SOI since v1.3.1 via Phase-A backfill).
	"""
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	try:
		# yei-v1.3.3 Patch 1: read the correct nested key.
		order_edit = (payload.get("order_edit") if isinstance(payload, dict) else None) or {}
		order_id = order_edit.get("order_id") or (payload.get("id") if isinstance(payload, dict) else None)

		sales_order = get_sales_order(order_id) if order_id else None

		if not sales_order:
			create_shopify_log(
				status="Invalid",
				message=f"Order edited webhook received but SO not found for Shopify order {order_id}",
			)
			return

		# Fetch full current state from Shopify — the webhook delta alone
		# lacks SKUs/prices/titles needed for SOI construction.
		try:
			shopify_order_resource = Order.find(str(order_id))
			full_order = shopify_order_resource.to_dict()
		except Exception as fetch_err:  # noqa: BLE001
			create_shopify_log(
				status="Error",
				message=f"orders/edited: Shopify REST fetch failed for order {order_id}: {fetch_err}",
				exception=fetch_err,
			)
			return

		added, refunded = _reconcile_so_line_items(sales_order, full_order)

		# Audit-trail ToDo (preserves legacy operator-visibility behaviour).
		diff_lines = _build_order_edit_diff(sales_order, full_order)
		if diff_lines:
			diff_text = "\n".join(diff_lines)
			todo_description = (
				f"<b>Shopify Order Edited: {full_order.get('name', order_id)}</b><br><br>"
				f"<b>Sales Order:</b> {sales_order.name}<br>"
				f"<b>Customer:</b> {sales_order.customer}<br><br>"
				f"<b>Reconciled:</b> +{len(added)} item(s), {len(refunded)} flagged refunded<br><br>"
				f"<b>Changes detected:</b><br><pre>{diff_text}</pre>"
			)
			try:
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
			except Exception:  # noqa: BLE001
				# ToDo is non-load-bearing — don't fail the whole handler over it.
				pass

		# Update order tags/status if changed.
		new_tags = full_order.get("tags", "")
		if new_tags != (sales_order.get(ORDER_STATUS_FIELD) or ""):
			frappe.db.set_value(
				"Sales Order", sales_order.name, ORDER_STATUS_FIELD, new_tags,
				update_modified=False,
			)

		# Update financial/fulfillment status mirror fields.
		frappe.db.set_value(
			"Sales Order",
			sales_order.name,
			{
				ORDER_FINANCIAL_STATUS_FIELD: full_order.get("financial_status") or "",
				ORDER_FULFILLMENT_STATUS_FIELD: full_order.get("fulfillment_status") or "",
			},
			update_modified=False,
		)

	except Exception as e:
		create_shopify_log(status="Error", exception=e)
	else:
		create_shopify_log(
			status="Success",
			message=f"orders/edited reconciled: +{len(added)} items, {len(refunded)} refund-flagged",
		)


@frappe.whitelist()
@temp_shopify_session
def replay_handle_order_edited(shopify_order_id, request_id=None):
	"""yei-v1.3.4: Admin-only entry point for backfilling historical
	``orders/edited`` webhook drops.

	Wraps ``handle_order_edited`` with a synthetic ``order_edit`` payload.
	Caller must hold System Manager (or be Administrator). Idempotent:
	``_reconcile_so_line_items`` skips lines already present.

	Bypasses the HMAC validation that ``_validate_request`` does for live
	webhooks; the admin-role gate is the explicit trust boundary.

	The ``@temp_shopify_session`` decorator establishes a Shopify session
	so the handler's ``Order.find(str(order_id))`` REST call succeeds.
	The live webhook flow gets its session via ``_validate_request`` in
	the webhook entry point; we have to set one up explicitly here since
	we bypass that path.

	``request_id`` is intentionally passed through as-is (default ``None``).
	When ``None``, ``create_shopify_log`` creates a fresh ``Ecommerce
	Integration Log`` row for the replay (consistent with live webhook
	fires). If a caller passes an existing EIL name, that log gets updated
	in place — useful when replaying a known-Invalid row to flip it to
	Success.
	"""
	if "System Manager" not in frappe.get_roles() and frappe.session.user != "Administrator":
		frappe.throw(_("System Manager role required for replay_handle_order_edited"))
	synthetic = {"order_edit": {"order_id": str(shopify_order_id)}}
	return handle_order_edited(synthetic, request_id=request_id)


def _reconcile_so_line_items(sales_order, full_order):
	"""yei-v1.3.3: reconcile ``sales_order.items`` against the current
	Shopify lineItems.

	Returns ``(added_skus, refunded_lids)``.

	* New Shopify lines (not on SO by ``shopify_line_item_id``) → append a
	  new SOI built from the Shopify line via ``_build_soi_from_shopify_line``.
	* Lines with ``current_quantity == 0`` that are on the SO → flag via
	  ``flag_so_item_refunded`` (idempotent; respects existing flag).
	* Idempotent: re-runs are no-ops since both branches are guarded by
	  membership / flag checks.

	Requires Property Setter ``Sales Order Item.allow_on_submit=1`` so the
	``save()`` call on a submitted SO doesn't raise
	``UpdateAfterSubmitError`` for the new child rows. Installed by patch
	``add_so_item_allow_on_submit`` (yei-v1.3.3).
	"""
	from ecommerce_integrations.shopify.refund import flag_so_item_refunded

	setting = frappe.get_doc(SETTING_DOCTYPE)
	shopify_lines = full_order.get("line_items") or []

	# Existing SOI index by shopify_line_item_id (stamped since v1.3.1).
	existing_by_lid = {}
	for soi in sales_order.items:
		lid = (soi.get(LINE_ITEM_ID_FIELD) or "")
		if lid:
			existing_by_lid[str(lid)] = soi

	added = []
	refunded = []
	taxes_inclusive = cint(getattr(setting, "taxes_inclusive", 0))
	now = nowdate()

	for li in shopify_lines:
		lid = str(li.get("id") or "")
		cq = cint(li.get("current_quantity", li.get("quantity", 1)))

		# Skip tip lines (mirror B9 behaviour in get_order_items).
		title = str(li.get("title") or "").strip().lower()
		if title == "tip":
			continue

		if cq == 0:
			# Refund territory — flag if present on the SO; skip if not (line
			# was added then fully refunded before reconcile — nothing to do).
			soi = existing_by_lid.get(lid)
			if soi and not cint(soi.get("shopify_refunded") or 0):
				flag_so_item_refunded(sales_order.name, soi.name, now)
				refunded.append(lid)
			continue

		if lid and lid in existing_by_lid:
			continue  # Already on the SO.

		# New line — build an SOI row from the Shopify line.
		new_row = _build_soi_from_shopify_line(li, setting, sales_order, taxes_inclusive)
		if new_row:
			sales_order.append("items", new_row)
			added.append(new_row.get("item_code"))

	if added:
		# Required to flush new child rows. Property Setter
		# Sales Order Item.allow_on_submit=1 unblocks the submit-time write.
		sales_order.save(ignore_permissions=True)

	return added, refunded


def _build_soi_from_shopify_line(shopify_item, setting, sales_order, taxes_inclusive):
	"""yei-v1.3.3: build a single SOI dict from a Shopify line_item.

	Mirrors per-row logic in ``get_order_items`` (B12 / B15 / B24b semantics)
	without re-walking the full order. Returns ``None`` if the line is a
	tip / unfillable / current_quantity == 0 (caller already filters these
	but defensive).
	"""
	current_qty = cint(
		shopify_item.get("current_quantity", shopify_item.get("quantity", 1))
	)
	if current_qty == 0:
		return None

	item_code = None
	if shopify_item.get("product_exists") and shopify_item.get("product_id"):
		item_code = get_item_code(shopify_item)
	if not item_code:
		item_code = UNMATCHED_ITEM_CODE
		_ensure_misc_manual_item(setting)

	properties = shopify_item.get("properties") or []
	properties_json = json.dumps(properties) if properties else ""

	price = flt(shopify_item.get("price"))
	qty = current_qty
	total_discount = _get_total_discount(shopify_item)
	per_unit_discount = total_discount / qty if qty else 0.0

	if taxes_inclusive:
		per_unit_tax = sum(
			flt(tax.get("price")) for tax in (shopify_item.get("tax_lines") or [])
		) / qty if qty else 0.0
	else:
		per_unit_tax = 0.0

	effective_rate = price - per_unit_tax - per_unit_discount

	line_item_id = shopify_item.get("id")
	# Derive delivery_date from the parent SO so the new row aligns with
	# existing line semantics (ERPNext requires per-row delivery_date).
	delivery_date = sales_order.get("delivery_date") or nowdate()

	# yei-v1.3.4 hotfix: append+save on a submitted parent SO does not
	# autofill ``uom`` + ``conversion_factor`` the way fresh-doc insert
	# does (the validate hook that would copy from Item.stock_uom is
	# bypassed under the allow_on_submit code path). Set them explicitly
	# so the new SOI rows pass validation.
	stock_uom = shopify_item.get("uom") or "Nos"
	row = {
		"item_code": item_code,
		"item_name": shopify_item.get("name") or shopify_item.get("title"),
		"rate": effective_rate,
		"price_list_rate": effective_rate,
		"delivery_date": delivery_date,
		"qty": qty,
		"stock_uom": stock_uom,
		"uom": stock_uom,
		"conversion_factor": 1.0,
		"warehouse": setting.warehouse,
		ORDER_ITEM_PROPERTIES_FIELD: properties_json,
		LINE_ITEM_ID_FIELD: str(line_item_id) if line_item_id is not None else "",
	}

	if item_code == UNMATCHED_ITEM_CODE:
		row["description"] = (
			f"[UNMATCHED] {shopify_item.get('title', '')} "
			f"(Shopify product_id: {shopify_item.get('product_id', 'N/A')}, "
			f"variant_id: {shopify_item.get('variant_id', 'N/A')})"
		)

	return row


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
				# B24b: current_quantity over quantity (post-refund authoritative).
				qty = li.get("current_quantity", li.get("quantity", 1))
				diff_lines.append(f"+ NEW ITEM: {title} (qty: {qty}, price: ${price})")

	# Detect quantity/price changes for existing items
	for item in sales_order.items:
		matching = [
			li for li in shopify_order.get("line_items", [])
			if li.get("title") == item.item_name or li.get("sku") == item.item_code
		]
		for li in matching:
			# B24b: current_quantity over quantity (post-refund authoritative).
			new_qty = cint(li.get("current_quantity", li.get("quantity")))
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
