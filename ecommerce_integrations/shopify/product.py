import json
from typing import Optional

import frappe
from frappe import _, msgprint
from frappe.utils import cint, cstr
from frappe.utils.nestedset import get_root_of
from shopify.resources import Metafield, Product, Variant

from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_item import ecommerce_item
from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import (
	ITEM_METAFIELDS_FIELD,
	ITEM_TAGS_FIELD,
	MODULE_NAME,
	SETTING_DOCTYPE,
	SHOPIFY_VARIANTS_ATTR_LIST,
	SUPPLIER_ID_FIELD,
	WEIGHT_TO_ERPNEXT_UOM_MAP,
)
from ecommerce_integrations.shopify.utils import create_shopify_log


def _guard_non_numeric_item_code(item_code: str, source: str) -> None:
	"""B21: item_code must never be a pure Shopify numeric ID.

	Raises at the boundary so any future path that tries to mint a
	numeric-ID Item fails fast. Defense-in-depth for f-020 / f-027
	(templated-Item leakage). SKU-less products must be skipped with
	an explicit B21 log, never stubbed with a numeric fallback.
	"""
	if str(item_code).isdigit():
		raise ValueError(
			f"{source}: attempted to create Item with numeric item_code "
			f"{item_code!r}. SKU-less products must be skipped, not stubbed."
		)


class ShopifyProduct:
	def __init__(
		self,
		product_id: str,
		variant_id: str | None = None,
		sku: str | None = None,
		has_variants: int | None = 0,
	):
		self.product_id = str(product_id)
		self.variant_id = str(variant_id) if variant_id else None
		self.sku = str(sku) if sku else None
		self.has_variants = has_variants
		self.setting = frappe.get_doc(SETTING_DOCTYPE)

		if not self.setting.is_enabled():
			frappe.throw(_("Can not create Shopify product when integration is disabled."))

	def is_synced(self) -> bool:
		return ecommerce_item.is_synced(
			MODULE_NAME,
			integration_item_code=self.product_id,
			variant_id=self.variant_id,
			sku=self.sku,
		)

	def get_erpnext_item(self):
		return ecommerce_item.get_erpnext_item(
			MODULE_NAME,
			integration_item_code=self.product_id,
			variant_id=self.variant_id,
			sku=self.sku,
			has_variants=self.has_variants,
		)

	@temp_shopify_session
	def sync_product(self):
		if not self.is_synced():
			shopify_product = Product.find(self.product_id)
			product_dict = shopify_product.to_dict()
			self._make_item(product_dict)

			# B5+B6: Sync tags and metafields to the ERPNext Item
			_sync_tags_and_metafields(self.product_id, product_dict)

	def _make_item(self, product_dict):
		"""B21: Flat-Item convention. Shopify product templates are NEVER
		imported as ERPNext Items. Each variant becomes a top-level Item
		with item_code = variant.sku. B14 SKU-match handles existing
		Items; unmapped SKUs create new flat Items with has_variants=0.
		Variants without SKU are skipped with an explicit B21 log —
		manual Item creation is required (numeric IDs forbidden).
		"""
		_add_weight_details(product_dict)
		warehouse = self.setting.warehouse

		variants = product_dict.get("variants") or []
		if not variants:
			create_shopify_log(
				status="Invalid",
				message=f"B21 skip: product {product_dict.get('id')} has no variants",
			)
			return

		for variant in variants:
			sku = variant.get("sku")
			if not sku:
				create_shopify_log(
					status="Success",
					message=(
						f"B21 skip: product {product_dict.get('id')} "
						f"variant {variant.get('id')} has no SKU — "
						f"manual Item creation required (numeric IDs forbidden)"
					),
				)
				continue
			self._create_flat_variant(product_dict, variant, warehouse)

	def _create_flat_variant(self, product_dict, variant, warehouse):
		"""B21: Create one flat ERPNext Item per Shopify variant with
		item_code = variant.sku. Links to an existing Item via B14
		SKU-match if one exists; otherwise creates a fresh flat Item
		with has_variants=0. Never produces a numeric item_code — the
		_guard_non_numeric_item_code boundary helper raises loudly if
		anything tries."""
		sku = variant["sku"]
		_guard_non_numeric_item_code(sku, "_create_flat_variant")

		title = product_dict.get("title", "").strip()
		variant_title = (variant.get("title") or "").strip()
		if variant_title and variant_title not in ("Default Title",):
			item_name = f"{title} - {variant_title}"
		else:
			item_name = title

		item_dict = {
			"is_stock_item": 1,
			"item_code": sku,
			"item_name": item_name,
			"description": product_dict.get("body_html") or product_dict.get("title"),
			"item_group": self._get_item_group(product_dict.get("product_type")),
			"has_variants": 0,
			"stock_uom": _("Nos"),
			"sku": sku,
			"default_warehouse": warehouse,
			"image": _get_item_image(product_dict),
			"weight_uom": WEIGHT_TO_ERPNEXT_UOM_MAP[variant.get("weight_unit")],
			"weight_per_unit": variant.get("weight"),
			"default_supplier": self._get_supplier(product_dict),
		}

		integration_item_code = product_dict["id"]
		variant_id = variant.get("id")

		if not _match_sku_and_link_item(item_dict, integration_item_code, variant_id):
			ecommerce_item.create_ecommerce_item(
				MODULE_NAME,
				integration_item_code,
				item_dict,
				variant_id=variant_id,
				sku=sku,
				has_variants=0,
			)

	def _get_item_group(self, product_type=None):
		parent_item_group = get_root_of("Item Group")

		if not product_type:
			return parent_item_group

		if frappe.db.get_value("Item Group", product_type, "name"):
			return product_type
		item_group = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": product_type,
				"parent_item_group": parent_item_group,
				"is_group": "No",
			}
		).insert()
		return item_group.name

	def _get_supplier(self, product_dict):
		if product_dict.get("vendor"):
			supplier = frappe.db.sql(
				f"""select name from tabSupplier
				where name = %s or {SUPPLIER_ID_FIELD} = %s """,
				(product_dict.get("vendor"), product_dict.get("vendor").lower()),
				as_list=1,
			)

			if supplier:
				return product_dict.get("vendor")
			supplier = frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": product_dict.get("vendor"),
					SUPPLIER_ID_FIELD: product_dict.get("vendor").lower(),
					"supplier_group": self._get_supplier_group(),
				}
			).insert()
			return supplier.name
		else:
			return ""

	def _get_supplier_group(self):
		supplier_group = frappe.db.get_value("Supplier Group", _("Shopify Supplier"))
		if not supplier_group:
			supplier_group = frappe.get_doc(
				{"doctype": "Supplier Group", "supplier_group_name": _("Shopify Supplier")}
			).insert()
			return supplier_group.name
		return supplier_group


def _add_weight_details(product_dict):
	variants = product_dict.get("variants")
	if variants:
		product_dict["weight"] = variants[0]["weight"]
		product_dict["weight_unit"] = variants[0]["weight_unit"]


def _has_variants(product_dict) -> bool:
	options = product_dict.get("options")
	return bool(options and "Default Title" not in options[0]["values"])


def _get_sku(product_dict):
	if product_dict.get("variants"):
		return product_dict.get("variants")[0].get("sku")
	return ""


def _get_item_image(product_dict):
	if product_dict.get("image"):
		return product_dict.get("image").get("src")
	return None


def _match_sku_and_link_item(item_dict, product_id, variant_id) -> bool:
	"""B14+B21: Link to an existing flat Item by SKU == item_code.

	Always called with flat Items post-B21 (the templated-Item path was
	removed). Returns True if an existing Item with item_code == sku was
	found and a matching Ecommerce Item mapping was inserted.
	"""
	sku = item_dict["sku"]
	if not sku:
		return False

	item_name = frappe.db.get_value("Item", {"item_code": sku})
	if item_name:
		try:
			ecommerce_item = frappe.get_doc(
				{
					"doctype": "Ecommerce Item",
					"integration": MODULE_NAME,
					"erpnext_item_code": item_name,
					"integration_item_code": product_id,
					"has_variants": 0,
					"variant_id": cstr(variant_id),
					"sku": sku,
				}
			)

			ecommerce_item.insert()
			return True
		except Exception:
			return False


def create_items_if_not_exist(order):
	"""Using shopify order, sync all items that are not already synced."""
	for item in order.get("line_items", []):
		product_id = item["product_id"]
		variant_id = item.get("variant_id")
		sku = item.get("sku")
		product = ShopifyProduct(product_id, variant_id=variant_id, sku=sku)

		if not product.is_synced():
			product.sync_product()


def get_item_code(shopify_item):
	"""Get item code using shopify_item dict.

	Item should contain both product_id and variant_id.

	B16: Falls back to product-level Ecommerce Item match (without variant_id
	filter) when the specific sku/variant lookup fails. Handles SKU-less
	products (dropship, unassigned catalog items) where we maintain a
	product_id → representative item_code mapping.
	"""

	item = ecommerce_item.get_erpnext_item(
		integration=MODULE_NAME,
		integration_item_code=shopify_item.get("product_id"),
		variant_id=shopify_item.get("variant_id"),
		sku=shopify_item.get("sku"),
	)
	if item:
		return item.item_code

	# B16: product-level fallback (no SKU, no variant match) — used for
	# dropship products and any catalog items without pushed SKUs.
	if shopify_item.get("product_id"):
		item = ecommerce_item.get_erpnext_item(
			integration=MODULE_NAME,
			integration_item_code=shopify_item.get("product_id"),
			variant_id=None,  # explicit — don't filter by variant
			sku=None,
		)
		if item:
			return item.item_code


@temp_shopify_session
def upload_erpnext_item(doc, method=None):
	"""This hook is called when inserting new or updating existing `Item`.

	New items are pushed to shopify and changes to existing items are
	updated depending on what is configured in "Shopify Setting" doctype.
	"""
	template_item = item = doc  # alias for readability
	# a new item recieved from ecommerce_integrations is being inserted
	if item.flags.from_integration:
		return

	setting = frappe.get_doc(SETTING_DOCTYPE)

	if not setting.is_enabled() or not setting.upload_erpnext_items:
		return

	if frappe.flags.in_import:
		return

	if item.has_variants:
		return

	if len(item.attributes) > 3:
		msgprint(_("Template items/Items with 4 or more attributes can not be uploaded to Shopify."))
		return

	if doc.variant_of and not setting.upload_variants_as_items:
		msgprint(_("Enable variant sync in setting to upload item to Shopify."))
		return

	if item.variant_of:
		template_item = frappe.get_doc("Item", item.variant_of)

	product_id = frappe.db.get_value(
		"Ecommerce Item",
		{"erpnext_item_code": template_item.name, "integration": MODULE_NAME},
		"integration_item_code",
	)
	is_new_product = not bool(product_id)

	if is_new_product:
		product = Product()
		product.published = False
		product.status = "active" if setting.sync_new_item_as_active else "draft"

		map_erpnext_item_to_shopify(shopify_product=product, erpnext_item=template_item)
		is_successful = product.save()

		if is_successful:
			update_default_variant_properties(
				product,
				sku=template_item.item_code,
				price=template_item.standard_rate,
				is_stock_item=template_item.is_stock_item,
			)
			if item.variant_of:
				product.options = []
				product.variants = []
				variant_attributes = {
					"title": template_item.item_name,
					"sku": item.item_code,
					"price": item.standard_rate,
				}
				max_index_range = min(3, len(template_item.attributes))
				for i in range(0, max_index_range):
					attr = template_item.attributes[i]
					product.options.append(
						{
							"name": attr.attribute,
							"values": frappe.db.get_all(
								"Item Attribute Value", {"parent": attr.attribute}, pluck="attribute_value"
							),
						}
					)
					try:
						variant_attributes[f"option{i+1}"] = item.attributes[i].attribute_value
					except IndexError:
						frappe.throw(
							_("Shopify Error: Missing value for attribute {}").format(attr.attribute)
						)
				product.variants.append(Variant(variant_attributes))

			product.save()  # push variant

			ecom_items = list(set([item, template_item]))
			for d in ecom_items:
				ecom_item = frappe.get_doc(
					{
						"doctype": "Ecommerce Item",
						"erpnext_item_code": d.name,
						"integration": MODULE_NAME,
						"integration_item_code": str(product.id),
						"variant_id": "" if d.has_variants else str(product.variants[0].id),
						"sku": "" if d.has_variants else str(product.variants[0].sku),
						"has_variants": d.has_variants,
						"variant_of": d.variant_of,
					}
				)
				ecom_item.insert()

		write_upload_log(status=is_successful, product=product, item=item)
	elif setting.update_shopify_item_on_update:
		product = Product.find(product_id)
		if product:
			map_erpnext_item_to_shopify(shopify_product=product, erpnext_item=template_item)
			if not item.variant_of:
				update_default_variant_properties(
					product,
					is_stock_item=template_item.is_stock_item,
					price=item.standard_rate,
				)
			else:
				variant_attributes = {"sku": item.item_code, "price": item.standard_rate}
				product.options = []
				max_index_range = min(3, len(template_item.attributes))
				for i in range(0, max_index_range):
					attr = template_item.attributes[i]
					product.options.append(
						{
							"name": attr.attribute,
							"values": frappe.db.get_all(
								"Item Attribute Value", {"parent": attr.attribute}, pluck="attribute_value"
							),
						}
					)
					try:
						variant_attributes[f"option{i+1}"] = item.attributes[i].attribute_value
					except IndexError:
						frappe.throw(
							_("Shopify Error: Missing value for attribute {}").format(attr.attribute)
						)
				product.variants.append(Variant(variant_attributes))

			is_successful = product.save()
			if is_successful and item.variant_of:
				map_erpnext_variant_to_shopify_variant(product, item, variant_attributes)

			write_upload_log(status=is_successful, product=product, item=item, action="Updated")


def map_erpnext_variant_to_shopify_variant(shopify_product: Product, erpnext_item, variant_attributes):
	variant_product_id = frappe.db.get_value(
		"Ecommerce Item",
		{"erpnext_item_code": erpnext_item.name, "integration": MODULE_NAME},
		"integration_item_code",
	)
	if not variant_product_id:
		for variant in shopify_product.variants:
			if (
				variant.option1 == variant_attributes.get("option1")
				and variant.option2 == variant_attributes.get("option2")
				and variant.option3 == variant_attributes.get("option3")
			):
				variant_product_id = str(variant.id)
				if not frappe.flags.in_test:
					frappe.get_doc(
						{
							"doctype": "Ecommerce Item",
							"erpnext_item_code": erpnext_item.name,
							"integration": MODULE_NAME,
							"integration_item_code": str(shopify_product.id),
							"variant_id": variant_product_id,
							"sku": str(variant.sku),
							"variant_of": erpnext_item.variant_of,
						}
					).insert()
				break
		if not variant_product_id:
			msgprint(_("Shopify: Couldn't sync item variant."))
	return variant_product_id


def map_erpnext_item_to_shopify(shopify_product: Product, erpnext_item):
	"""Map erpnext fields to shopify, called both when updating and creating new products."""

	shopify_product.title = erpnext_item.item_name
	shopify_product.body_html = erpnext_item.description
	shopify_product.product_type = erpnext_item.item_group

	if erpnext_item.weight_uom in WEIGHT_TO_ERPNEXT_UOM_MAP.values():
		# reverse lookup for key
		uom = get_shopify_weight_uom(erpnext_weight_uom=erpnext_item.weight_uom)
		shopify_product.weight = erpnext_item.weight_per_unit
		shopify_product.weight_unit = uom

	if erpnext_item.disabled:
		shopify_product.status = "draft"
		shopify_product.published = False
		msgprint(_("Status of linked Shopify product is changed to Draft."))


def get_shopify_weight_uom(erpnext_weight_uom: str) -> str:
	for shopify_uom, erpnext_uom in WEIGHT_TO_ERPNEXT_UOM_MAP.items():
		if erpnext_uom == erpnext_weight_uom:
			return shopify_uom


def update_default_variant_properties(
	shopify_product: Product,
	is_stock_item: bool,
	sku: str | None = None,
	price: float | None = None,
):
	"""Shopify creates default variant upon saving the product.

	Some item properties are supposed to be updated on the default variant.
	Input: saved shopify_product, sku and price
	"""
	default_variant: Variant = shopify_product.variants[0]

	# this will create Inventory item and qty will be updated by scheduled job.
	if is_stock_item:
		default_variant.inventory_management = "shopify"

	if price is not None:
		default_variant.price = price
	if sku is not None:
		default_variant.sku = sku


def _sync_tags_and_metafields(product_id, product_dict):
	"""B5+B6: Sync Shopify product tags and metafields to the ERPNext Item.

	Finds the ERPNext Item linked to this Shopify product via Ecommerce Item,
	then updates shopify_tags and shopify_metafields custom fields.
	"""
	# Find all ERPNext items linked to this product (template + variants)
	ecom_items = frappe.get_all(
		"Ecommerce Item",
		filters={"integration": MODULE_NAME, "integration_item_code": str(product_id)},
		pluck="erpnext_item_code",
	)

	if not ecom_items:
		return

	# B5: Tags come directly from product_dict
	tags = product_dict.get("tags", "")

	# B6: Fetch metafields from Shopify API
	metafields_json = ""
	try:
		metafields_raw = Product.find(product_id).metafields()
		if metafields_raw:
			metafields_list = [mf.to_dict() for mf in metafields_raw]
			# Keep only useful fields, strip internal Shopify metadata
			metafields_clean = [
				{
					"namespace": mf.get("namespace"),
					"key": mf.get("key"),
					"value": mf.get("value"),
					"type": mf.get("type"),
				}
				for mf in metafields_list
			]
			metafields_json = json.dumps(metafields_clean)
	except Exception:
		# Metafield fetch is non-critical — don't block product sync
		pass

	# Update all linked ERPNext Items
	for item_code in ecom_items:
		frappe.db.set_value(
			"Item",
			item_code,
			{ITEM_TAGS_FIELD: tags, ITEM_METAFIELDS_FIELD: metafields_json},
			update_modified=False,
		)


def sync_product_from_webhook(payload, request_id=None):
	"""B5 + B20: Handle products/create + products/update webhook events.

	Tag-sync-only. When the product is already mapped via Ecommerce Item,
	refresh `shopify_tags` on every linked ERPNext Item. When the product is
	NOT mapped, skip silently — the products/* webhook must NEVER create new
	ERPNext Items.

	B20 rationale (f-020, 2026-04-20): the previous create-new branch called
	`ShopifyProduct.sync_product()`, which builds `item_code` from
	`product_dict["id"]` (Shopify numeric ID) when no explicit item_code is
	set. For products with variants the template skips SKU matching entirely
	(`_match_sku_and_link_item` returns False when `has_variant=True`),
	producing duplicate numeric-ID Items for every product template. 70
	duplicates were created overnight before the webhooks were disabled.

	New-product promotion into ERPNext is handled via:
	  (a) Order sync — `create_items_if_not_exist` runs the B14 SKU-first
	      resolver per line item and links/creates correctly.
	  (b) Manual ERPNext Item creation (stage-04b-style import).

	B6 (metafields) intentionally NOT synced here — webhook payload doesn't
	include metafields, and the parked B6 spec hasn't been scoped.
	"""
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	product_dict = payload
	product_id = cstr(product_dict.get("id"))
	if not product_id:
		create_shopify_log(
			status="Invalid",
			message="products webhook payload missing id — skipped",
		)
		return

	try:
		action, linked_items = _resolve_product_sync_action(product_id)
		tags = product_dict.get("tags", "") or ""

		if action == "update_tags":
			for item_code in linked_items:
				frappe.db.set_value(
					"Item",
					item_code,
					ITEM_TAGS_FIELD,
					tags,
					update_modified=False,
				)
			create_shopify_log(
				status="Success",
				message=(
					f"B5 tag sync: product {product_id} → "
					f"{len(linked_items)} Item(s) updated with tags={tags!r}"
				),
			)
		else:
			create_shopify_log(
				status="Success",
				message=(
					f"B20 skip: product {product_id} has no ERPNext mapping — "
					f"webhook does not create Items (f-020 fix). "
					f"Promote via order sync or manual Item creation."
				),
			)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)


def _resolve_product_sync_action(product_id):
	"""B5: Decide whether a webhook should update tags on existing Items or
	trigger a new ShopifyProduct.sync_product() flow.

	Returns: ("update_tags", [erpnext_item_codes]) | ("create_new", [])
	"""
	linked = frappe.get_all(
		"Ecommerce Item",
		filters={"integration": MODULE_NAME, "integration_item_code": cstr(product_id)},
		pluck="erpnext_item_code",
	)
	if linked:
		return ("update_tags", linked)
	return ("create_new", [])


def write_upload_log(status: bool, product: Product, item, action="Created") -> None:
	if not status:
		msg = _("Failed to upload item to Shopify") + "<br>"
		msg += _("Shopify reported errors:") + " " + ", ".join(product.errors.full_messages())
		msgprint(msg, title="Note", indicator="orange")

		create_shopify_log(
			status="Error",
			request_data=product.to_dict(),
			message=msg,
			method="upload_erpnext_item",
		)
	else:
		create_shopify_log(
			status="Success",
			request_data=product.to_dict(),
			message=f"{action} Item: {item.name}, shopify product: {product.id}",
			method="upload_erpnext_item",
		)
