"""yei-v1.3.2 — Property Setter ``Sales Order.shipping_address_name allow_on_submit=1``.

The Issue #1 backfill (per-order shipping Address records) needs to write
``Sales Order.shipping_address_name`` on submitted (docstatus=1) SOs. The
native Frappe field has ``allow_on_submit=0`` so REST ``set_value`` raises
``UpdateAfterSubmitError``.

Property Setter is the documented Frappe surface for relaxing DocField
attributes on a NATIVE (non-Custom-Field) field. Direct precedent:
``ygh_fedex/patches/relabel_alpha26_dn_lr_fields.py`` (alpha26 lr_no
relabel). For Custom Fields the equivalent pattern is to re-run
``setup_custom_fields()`` (see yei ``update_freight_class_allow_on_submit``).

Defensive backstop only — the v1.3.2 Issue #1 backfill primarily uses
edit-in-place on the Address doctype (non-submittable, no docstatus
constraint), so the SO field never has to be rewritten in the common
path. This Property Setter unlocks future SO-side mutation flows (Kete
form edits, follow-on v1.3.x backfills, the <5% non-unique-Address
fallback path).

Idempotent — ``make_property_setter`` upserts by (doctype, fieldname,
property). Safe to re-run.
"""
from __future__ import annotations

import frappe


def execute():
	frappe.make_property_setter({
		"doctype": "Sales Order",
		"fieldname": "shipping_address_name",
		"property": "allow_on_submit",
		"value": "1",
		"property_type": "Check",
	})

	frappe.db.commit()
	frappe.clear_cache(doctype="Sales Order")
