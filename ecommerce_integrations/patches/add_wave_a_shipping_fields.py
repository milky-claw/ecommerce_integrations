"""Wave A — additive new shipping-classification fields (2026-05-12).

Companion to ygf Wave A `add_wave_a_shipping_fields` (ygh_fedex side).
Part of the multi-wave expand-contract consolidation of shipping-class
fields (see ERPNext workspace
``stages/04c-data-sync/references/shipping-fields-consolidation-2026-05-12.md``).

This patch is **purely additive** — old fields `Sales Order.shopify_freight_class`
and `Sales Order Item.shopify_freight_class` are unchanged. Two new fields
are installed beside the old ones:

  Sales Order.so_ship_class      Select air|sea|dropship|split|""
  Sales Order Item.item_ship_method Select ship-air|ship-sea|ship-dropship|""

During Wave A, both yei production write sites (order.py SO sync and
freight_class.py recompute) dual-write old + new. Reader cutover happens
in Wave B. Old fields are dropped in Wave C.

Re-runs `setup_custom_fields()` which is extended to include the new
fields; Frappe's `create_custom_fields` updates in place if a field
already exists. Idempotent — safe to re-run.
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
