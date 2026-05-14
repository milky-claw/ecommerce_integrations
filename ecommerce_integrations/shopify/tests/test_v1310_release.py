"""yei-v1.3.10 release tests — prepare_delivery_note mirror write.

Retroactive coverage for the v1.3.10 ship (commit b8863cc, deployed
2026-05-14). Asserts the mirror-write call is present in the
prepare_delivery_note function source.

Background: orders/fulfilled + orders/partially_fulfilled webhooks
created DN but never updated Sales Order.shopify_fulfillment_status,
causing ~240 SOs to drift in any 30-day window. v1.3.10 added a
frappe.db.set_value call after create_delivery_note.

Run via:
    cd /Users/milky/ecommerce_integrations && python3 -m pytest \\
        ecommerce_integrations/shopify/tests/test_v1310_release.py

Note: fulfillment.py has a top-level `import frappe` so it cannot be
imported outside a Frappe bench. These tests read the source file
directly via pathlib — no frappe runtime required.
"""
from __future__ import annotations

import pathlib
import re


def _get_prepare_delivery_note_src() -> str:
    """Return the source text of prepare_delivery_note from fulfillment.py."""
    here = pathlib.Path(__file__).parent
    src_path = here.parent / "fulfillment.py"
    full = src_path.read_text(encoding="utf-8")
    # Slice from the def line to the next top-level def/class (or EOF).
    match = re.search(r"^def prepare_delivery_note\b", full, re.MULTILINE)
    assert match, "prepare_delivery_note not found in fulfillment.py"
    start = match.start()
    # Next top-level def/class after our function
    next_top = re.search(r"^(?:def |class )", full[start + 1:], re.MULTILINE)
    end = start + 1 + next_top.start() if next_top else len(full)
    return full[start:end]


class TestV1310MirrorWriteSourceInvariant:
    """Source-level: prepare_delivery_note writes the SO fulfillment_status."""

    def test_mirror_write_present(self):
        src = _get_prepare_delivery_note_src()
        assert "frappe.db.set_value" in src, (
            "v1.3.10: prepare_delivery_note must call frappe.db.set_value "
            "to mirror fulfillment_status back to SO"
        )
        assert "ORDER_FULFILLMENT_STATUS_FIELD" in src, (
            "v1.3.10: mirror-write must reference ORDER_FULFILLMENT_STATUS_FIELD"
        )

    def test_uses_update_modified_false(self):
        """Must bypass allow_on_submit=0 guard via update_modified=False."""
        src = _get_prepare_delivery_note_src()
        assert "update_modified=False" in src, (
            "v1.3.10: mirror-write must use update_modified=False to bypass "
            "the allow_on_submit=0 guard on the Custom Field"
        )

    def test_defensive_empty_string_fallback(self):
        """Defensive: order.get('fulfillment_status') or '' (no None writes)."""
        src = _get_prepare_delivery_note_src()
        assert 'order.get("fulfillment_status") or ""' in src or \
               "order.get('fulfillment_status') or ''" in src, (
            "v1.3.10: mirror-write must coerce None → '' to avoid NULL writes"
        )

    def test_writes_after_create_delivery_note(self):
        """Mirror-write comes AFTER create_delivery_note (DN exists first)."""
        src = _get_prepare_delivery_note_src()
        idx_create = src.find("create_delivery_note(")
        idx_mirror = src.find("frappe.db.set_value")
        assert idx_create != -1 and idx_mirror != -1
        assert idx_create < idx_mirror, (
            "Mirror-write must be after create_delivery_note so DN exists "
            "before the SO mirror flips"
        )
