"""B24: Add the 7 refund Custom Fields to Sales Order, Sales Order Item,
and Delivery Note Item.

  * SO   — shopify_current_subtotal_price, shopify_current_total_price,
           shopify_current_total_discounts (3 Currency, read-only, allow_on_submit).
  * SOI  — shopify_refunded (Check), shopify_refunded_at (Datetime).
  * DNI  — shopify_refunded (Check), shopify_refunded_at (Datetime).

Idempotent: re-runs ``setup_custom_fields()`` which is extended (per B24)
to include the new fields. Frappe's ``create_custom_fields`` updates in
place when the field already exists.

See: stages/04c-data-sync/references/b24-impl-removed-items.md
"""
import frappe

from ecommerce_integrations.shopify.constants import SETTING_DOCTYPE
from ecommerce_integrations.shopify.doctype.shopify_setting.shopify_setting import (
	setup_custom_fields,
)


def execute():
	frappe.reload_doc("shopify", "doctype", "shopify_setting")

	settings = frappe.get_doc(SETTING_DOCTYPE)
	if settings.is_enabled():
		setup_custom_fields()
