"""yei-v1.3.3 — Property Setter ``Sales Order Item allow_on_submit=1`` (table).

The Issue #2 ``handle_order_edited`` line-reconciliation (``order.py``
``_reconcile_so_line_items``) appends new ``Sales Order Item`` child rows
on submitted SOs via ``sales_order.append("items", ...)`` + ``save()``.
Frappe's submit-time write protection rejects child-table mutations on
docstatus=1 unless the child doctype carries ``allow_on_submit=1``.

Property Setter is the documented Frappe surface for relaxing DocField
attributes on a NATIVE doctype. Direct precedents in this fork:

  * ``add_so_shipping_address_name_allow_on_submit.py`` (yei-v1.3.2)
    — same pattern on the SO.shipping_address_name field.
  * ``ygh_fedex/patches/relabel_alpha26_dn_lr_fields.py`` (alpha26)
    — sister-app precedent for native-field metadata mutation.

Scope: ``Sales Order Item`` table-level allow_on_submit (NOT a single
field). This unlocks ALL child-row writes on submitted SOs — read by
the reconciler when Shopify sends ``orders/edited``. Idempotent via
``make_property_setter`` upsert on (doctype, fieldname, property).
"""
from __future__ import annotations

import frappe


def execute():
	frappe.make_property_setter({
		"doctype": "Sales Order",
		"fieldname": "items",
		"property": "allow_on_submit",
		"value": "1",
		"property_type": "Check",
	})

	frappe.db.commit()
	frappe.clear_cache(doctype="Sales Order")
