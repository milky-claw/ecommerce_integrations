"""2026-05-13 Supplier-sheet-3-issues bundle — Install
``shopify_line_item_id`` (Data) on Sales Order Item + Delivery Note Item.

Re-runs ``setup_custom_fields()`` which is extended (per this release)
to include the two new fields beside the existing B24 refund flags.
Idempotent — Frappe's ``create_custom_fields`` updates in place if the
field already exists.

Companion: ygh_fedex 0.5.1 (split.py cascade + refund.py rewrite).
See ERPNext workspace ``stages/04c-data-sync/working/supplier-sheet-3-issues-2026-05-12/PLAN.md``.
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
