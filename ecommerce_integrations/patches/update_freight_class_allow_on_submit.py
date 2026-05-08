"""B23 follow-up: flip ``allow_on_submit=1`` on the freight_class fields.

The original ``add_shopify_freight_class`` patch (yei-v1.2.4) created the
two ``shopify_freight_class`` Custom Fields with default
``allow_on_submit=0``, which blocked the workspace backfill from writing
to already-submitted SOs (UpdateAfterSubmitError). Re-runs
``setup_custom_fields()`` after the dict is corrected (yei-v1.2.5) to
flip the flag in place via ``create_custom_fields``' upsert behavior.
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
