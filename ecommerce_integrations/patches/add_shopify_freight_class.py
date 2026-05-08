"""B23: Add `shopify_freight_class` Custom Fields to Sales Order + Sales Order Item.

Re-runs ``setup_custom_fields()`` which is now extended (per B23) to
include the two new Select fields. Idempotent — Frappe's
``create_custom_fields`` updates in place if the field already exists.
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
