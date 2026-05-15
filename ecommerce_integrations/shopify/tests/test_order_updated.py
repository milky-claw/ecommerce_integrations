"""yei-v1.4.0: unit tests for orders/updated webhook handler.

Run with: python3 ecommerce_integrations/shopify/tests/test_order_updated.py

Bench-independent: mocks Frappe before importing connector code, same
pattern as test_b24_refunds.py. Two categories:

  * Source-invariant tests (AST/text inspection) — proof that:
      - WEBHOOK_EVENTS includes "orders/updated"
      - EVENT_MAPPER routes "orders/updated" → handle_order_updated
      - handle_order_updated exists in order.py
      - Handler narrow-scope: does NOT touch shopify_financial_status
        in its delta computation (financial_status is explicit non-goal)

  * Behavioral tests on inlined helpers:
      - _SHIPPING_ADDR_FIELD_MAP coverage
      - Tier computation branches (mock DN states)
"""

import ast
import os
import re
import sys
import unittest
from unittest.mock import MagicMock

# ── Mock frappe before importing any connector code
frappe_mock = MagicMock()
frappe_mock._ = lambda x: x
frappe_mock.utils.cint = lambda x: int(x or 0)
frappe_mock.utils.cstr = lambda x: str(x) if x else ""
frappe_mock.utils.flt = lambda x: float(x or 0)
frappe_mock.flags = MagicMock()

sys.modules["frappe"] = frappe_mock
sys.modules["frappe.utils"] = frappe_mock.utils
sys.modules["frappe.tests"] = MagicMock()
sys.modules["frappe.custom.doctype.custom_field.custom_field"] = MagicMock()
sys.modules["frappe.model.document"] = MagicMock()

shopify_mock = MagicMock()
sys.modules["shopify"] = shopify_mock
sys.modules["shopify.resources"] = MagicMock()
sys.modules["shopify.collection"] = MagicMock()

_ei_shopify_mock = MagicMock()
_ei_shopify_mock.connection = MagicMock()
_ei_shopify_mock.connection.temp_shopify_session = lambda fn: fn
sys.modules["ecommerce_integrations"] = MagicMock()
sys.modules["ecommerce_integrations.shopify"] = _ei_shopify_mock
sys.modules["ecommerce_integrations.shopify.connection"] = _ei_shopify_mock.connection

SHOPIFY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORDER_PY = os.path.join(SHOPIFY_DIR, "order.py")
CONSTANTS_PY = os.path.join(SHOPIFY_DIR, "constants.py")


def _source_of(fn_name: str, source: str) -> str:
    """Return the raw source of a top-level function from parsed Python."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {fn_name!r} not found in source")


# ───────────────────────────────────────────────────────────────────────
# Source-invariant tests on constants.py
# ───────────────────────────────────────────────────────────────────────


class TestConstantsRegistration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CONSTANTS_PY, "r", encoding="utf-8") as f:
            cls.source = f.read()

    def test_orders_updated_in_webhook_events(self):
        # "orders/updated" must be listed in WEBHOOK_EVENTS
        # Look for the literal string with surrounding quotes inside a list context
        self.assertRegex(
            self.source,
            r'WEBHOOK_EVENTS\s*=\s*\[[^\]]*"orders/updated"[^\]]*\]',
            "v1.4.0: WEBHOOK_EVENTS must contain 'orders/updated'",
        )

    def test_orders_updated_in_event_mapper(self):
        # Mapper must route to handle_order_updated
        self.assertRegex(
            self.source,
            r'"orders/updated"\s*:\s*"ecommerce_integrations\.shopify\.order\.handle_order_updated"',
            "v1.4.0: EVENT_MAPPER must route 'orders/updated' to handle_order_updated",
        )


# ───────────────────────────────────────────────────────────────────────
# Source-invariant tests on handle_order_updated
# ───────────────────────────────────────────────────────────────────────


class TestHandleOrderUpdatedSourceInvariant(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(ORDER_PY, "r", encoding="utf-8") as f:
            cls.source = f.read()

    def test_handler_exists(self):
        # AST-level: function must be defined
        try:
            body = _source_of("handle_order_updated", self.source)
        except AssertionError:
            self.fail("handle_order_updated must be defined in order.py")
        self.assertTrue(len(body) > 50, "handler body must be non-trivial")

    def test_handler_does_not_set_financial_status(self):
        # Narrow scope: handler must NOT write shopify_financial_status
        # (financial_status is explicit out-of-scope per design doc)
        body = _source_of("handle_order_updated", self.source)
        self.assertNotIn(
            "ORDER_FINANCIAL_STATUS_FIELD", body,
            "v1.4.0 scope: handle_order_updated must NOT touch financial_status mirror"
        )
        self.assertNotRegex(
            body, r'shopify_financial_status\s*=',
            "v1.4.0 scope: handle_order_updated must NOT write shopify_financial_status"
        )

    def test_handler_only_mirrors_fulfilled(self):
        # Only the literal "fulfilled" value mirrors; un-fulfill / partial / null ignored
        body = _source_of("handle_order_updated", self.source)
        # The handler must compare payload fulfillment_status against "fulfilled"
        self.assertRegex(
            body, r'fulfillment_status[^\n]*==\s*"fulfilled"',
            "Handler must gate fulfillment mirror on payload == 'fulfilled'"
        )

    def test_handler_uses_get_sales_order(self):
        # Handler must use the canonical SO lookup (handles non-unique field)
        body = _source_of("handle_order_updated", self.source)
        self.assertIn(
            "get_sales_order", body,
            "Handler must use get_sales_order() for SO lookup (non-unique field hazard)"
        )

    def test_shipping_addr_helpers_exist(self):
        # The 3 helpers must be defined
        for name in (
            "_shipping_address_differs",
            "_handle_shipping_address_change",
            "_compute_tier",
        ):
            try:
                body = _source_of(name, self.source)
                self.assertTrue(body)
            except AssertionError:
                self.fail(f"helper {name!r} must be defined in order.py")

    def test_shipping_addr_field_map_excludes_company_email(self):
        # Per spec out-of-scope: company / email NOT in label-subset
        # Find the _SHIPPING_ADDR_FIELD_MAP literal in source
        m = re.search(
            r'_SHIPPING_ADDR_FIELD_MAP\s*=\s*\{([^}]+)\}',
            self.source, re.MULTILINE,
        )
        self.assertIsNotNone(m, "_SHIPPING_ADDR_FIELD_MAP must be defined")
        map_body = m.group(1)
        for forbidden in ("company", "email", "latitude", "longitude"):
            self.assertNotRegex(
                map_body, rf'["\']{forbidden}["\']\s*:',
                f"_SHIPPING_ADDR_FIELD_MAP must NOT include {forbidden!r}"
            )

    def test_shipping_addr_field_map_includes_label_fields(self):
        # Must cover all label fields
        m = re.search(
            r'_SHIPPING_ADDR_FIELD_MAP\s*=\s*\{([^}]+)\}',
            self.source, re.MULTILINE,
        )
        map_body = m.group(1)
        for required in ("address1", "address2", "city", "province", "zip", "country", "phone"):
            self.assertRegex(
                map_body, rf'["\']{required}["\']\s*:',
                f"_SHIPPING_ADDR_FIELD_MAP must include {required!r} (label-subset coverage)"
            )

    def test_compute_tier_branches(self):
        # _compute_tier must branch on AWB presence and pickup state
        body = _source_of("_compute_tier", self.source)
        self.assertIn("fedex_awb_number", body,
            "_compute_tier must check fedex_awb_number (tier 1↔2 gate)")
        self.assertIn("fedex_pickup_status", body,
            "_compute_tier must check fedex_pickup_status (tier 3↔4 gate)")
        self.assertRegex(body, r'\b1\b', "_compute_tier must return tier 1")
        self.assertRegex(body, r'\b2\b', "_compute_tier must return tier 2")
        self.assertRegex(body, r'\b3\b', "_compute_tier must return tier 3")
        self.assertRegex(body, r'\b4\b', "_compute_tier must return tier 4")

    def test_handler_idempotent_pattern(self):
        # The handler must compare payload to current SO before writing
        body = _source_of("handle_order_updated", self.source)
        # Look for the read-then-compare pattern on fulfillment status
        self.assertRegex(
            body, r'sales_order\.get\(ORDER_FULFILLMENT_STATUS_FIELD\)\s*!=',
            "Handler must compare SO mirror to payload before write (idempotency)"
        )

    def test_handler_logs_alert_for_tier_3_4(self):
        # Tier 3+4 must trigger frappe.log_error for ops review
        body = _source_of("_handle_shipping_address_change", self.source)
        self.assertIn("log_error", body,
            "_handle_shipping_address_change must call frappe.log_error for tier 3/4 alerts")

    def test_handler_safety_gate_uses_linked_so_count(self):
        # v1.4.4: gate flipped from `not ADDRESS_ID_FIELD` (which was
        # inverted — refused per-order Addresses) to a true shared-Address
        # signal: count live SOs referencing this Address via
        # shipping_address_name.
        body = _source_of("_handle_shipping_address_change", self.source)
        self.assertIn("frappe.db.count", body,
            "_handle_shipping_address_change must call frappe.db.count to "
            "measure how many SOs share this Address")
        self.assertIn("shipping_address_name", body,
            "_handle_shipping_address_change must filter on shipping_address_name")
        self.assertRegex(body, r'linked_so_count\s*>\s*1',
            "_handle_shipping_address_change must refuse only when >1 SO links here")

    def test_per_order_address_stamps_synthetic_id(self):
        # v1.4.4: _create_per_order_shipping_address falls back to a
        # synthetic id "order_<shopify_order_id>_ship" when Shopify's
        # order-level shipping_address has no id (which is always).
        body = _source_of("_create_per_order_shipping_address", self.source)
        self.assertRegex(body, r'order_\{[^}]*shopify_order[^}]*\}_ship',
            "_create_per_order_shipping_address must synthesize "
            "'order_<id>_ship' when ship.get('id') is None")

    def test_handler_skips_when_so_not_mirrored(self):
        # If get_sales_order returns None, handler must return without error
        body = _source_of("handle_order_updated", self.source)
        # Look for the "not sales_order" guard
        self.assertRegex(
            body, r'if\s+not\s+sales_order',
            "Handler must guard against missing SO (out-of-order webhook delivery)"
        )


# ───────────────────────────────────────────────────────────────────────
# Source-invariant tests on shopify_setting.py
# ───────────────────────────────────────────────────────────────────────


class TestForceReregisterHelper(unittest.TestCase):
    SETTING_PY = os.path.join(
        SHOPIFY_DIR, "doctype", "shopify_setting", "shopify_setting.py"
    )

    @classmethod
    def setUpClass(cls):
        with open(cls.SETTING_PY, "r", encoding="utf-8") as f:
            cls.source = f.read()

    def test_force_reregister_helper_exists(self):
        # Module-level whitelisted helper for post-deploy webhook re-register
        try:
            body = _source_of("force_reregister_webhooks", self.source)
        except AssertionError:
            self.fail("force_reregister_webhooks must be defined in shopify_setting.py")
        self.assertIn("@frappe.whitelist", self.source[
            self.source.index("def force_reregister_webhooks") - 200:
            self.source.index("def force_reregister_webhooks")
        ], "force_reregister_webhooks must carry @frappe.whitelist decorator")

    def test_force_reregister_clears_child_table(self):
        body = _source_of("force_reregister_webhooks", self.source)
        self.assertIn("frappe.db.delete", body,
            "force_reregister_webhooks must clear Shopify Webhooks child rows")
        self.assertIn("Shopify Webhooks", body,
            "force_reregister_webhooks must target Shopify Webhooks child doctype")

    def test_force_reregister_role_gated(self):
        body = _source_of("force_reregister_webhooks", self.source)
        self.assertIn("System Manager", body,
            "force_reregister_webhooks must check System Manager role")


# ───────────────────────────────────────────────────────────────────────
# Behavioral tests (inlined helpers)
# ───────────────────────────────────────────────────────────────────────


# Replicate the actual field map for behavioral coverage (must stay in sync)
_FIELD_MAP_UNDER_TEST = {
    "address1": "address_line1",
    "address2": "address_line2",
    "city": "city",
    "province": "state",
    "zip": "pincode",
    "country": "country",
    "phone": "phone",
}


def _shipping_addr_differs_inline(addr_dict, payload_addr, field_map=_FIELD_MAP_UNDER_TEST):
    """Mirror of _shipping_address_differs core logic for behavioral coverage."""
    for shopify_key, addr_key in field_map.items():
        new_val = str(payload_addr.get(shopify_key) or "").strip()
        cur_val = str(addr_dict.get(addr_key) or "").strip()
        if new_val != cur_val:
            return True
    first = str(payload_addr.get("first_name") or "").strip()
    last = str(payload_addr.get("last_name") or "").strip()
    new_title = (f"{first} {last}").strip()
    if new_title and new_title != str(addr_dict.get("address_title") or "").strip():
        return True
    return False


class TestShippingAddressDiffersBehavioral(unittest.TestCase):
    """Verify the delta-detection logic by inline copy of _shipping_address_differs."""

    def test_identical_addresses_no_diff(self):
        addr = {
            "address_line1": "123 Main St", "address_line2": "Apt 4",
            "city": "Anytown", "state": "Ohio", "pincode": "44060",
            "country": "United States", "phone": "+15551234567",
            "address_title": "Jane Smith",
        }
        payload = {
            "address1": "123 Main St", "address2": "Apt 4",
            "city": "Anytown", "province": "Ohio", "zip": "44060",
            "country": "United States", "phone": "+15551234567",
            "first_name": "Jane", "last_name": "Smith",
        }
        self.assertFalse(_shipping_addr_differs_inline(addr, payload),
            "Identical address+title should produce no diff (idempotency)")

    def test_city_change_detected(self):
        addr = {"city": "Anytown"}
        payload = {"city": "Newtown"}
        self.assertTrue(_shipping_addr_differs_inline(addr, payload))

    def test_phone_change_detected(self):
        addr = {"phone": "+15551234567"}
        payload = {"phone": "+15559876543"}
        self.assertTrue(_shipping_addr_differs_inline(addr, payload))

    def test_recipient_name_change_detected(self):
        addr = {"address_title": "Jane Smith"}
        payload = {"first_name": "Jane", "last_name": "Doe"}
        self.assertTrue(_shipping_addr_differs_inline(addr, payload))

    def test_whitespace_normalized(self):
        # Trailing/leading whitespace shouldn't trigger diff
        addr = {"city": "Anytown"}
        payload = {"city": "  Anytown  "}
        self.assertFalse(_shipping_addr_differs_inline(addr, payload))

    def test_null_vs_empty_string_equivalent(self):
        addr = {"address_line2": ""}
        payload = {"address2": None}
        self.assertFalse(_shipping_addr_differs_inline(addr, payload))

    def test_address1_change_detected(self):
        addr = {"address_line1": "123 Main St"}
        payload = {"address1": "456 Elm St"}
        self.assertTrue(_shipping_addr_differs_inline(addr, payload))

    def test_country_change_detected(self):
        addr = {"country": "United States"}
        payload = {"country": "Canada"}
        self.assertTrue(_shipping_addr_differs_inline(addr, payload))


def _compute_tier_inline(dn_rows):
    """Mirror of _compute_tier logic, taking pre-fetched DN rows."""
    if not dn_rows:
        return 1, []
    has_awb = [r for r in dn_rows if r.get("fedex_awb_number")]
    if not has_awb:
        return 2, [r["name"] for r in dn_rows]
    picked = any(
        (r.get("fedex_pickup_status") or "").upper() in ("PICKED_UP", "IN_TRANSIT", "DELIVERED")
        for r in has_awb
    )
    return (4 if picked else 3), [r["name"] for r in dn_rows]


class TestComputeTierBehavioral(unittest.TestCase):
    def test_no_dn_returns_tier_1(self):
        tier, dns = _compute_tier_inline([])
        self.assertEqual(tier, 1)
        self.assertEqual(dns, [])

    def test_draft_dn_no_awb_returns_tier_2(self):
        rows = [{"name": "MAT-DN-001", "fedex_awb_number": None, "fedex_pickup_status": None}]
        tier, dns = _compute_tier_inline(rows)
        self.assertEqual(tier, 2)
        self.assertEqual(dns, ["MAT-DN-001"])

    def test_awb_no_pickup_returns_tier_3(self):
        rows = [{"name": "MAT-DN-001", "fedex_awb_number": "871451979049",
                 "fedex_pickup_status": None}]
        tier, dns = _compute_tier_inline(rows)
        self.assertEqual(tier, 3)

    def test_awb_with_picked_up_returns_tier_4(self):
        rows = [{"name": "MAT-DN-001", "fedex_awb_number": "871451979049",
                 "fedex_pickup_status": "PICKED_UP"}]
        tier, dns = _compute_tier_inline(rows)
        self.assertEqual(tier, 4)

    def test_awb_with_in_transit_returns_tier_4(self):
        rows = [{"name": "MAT-DN-001", "fedex_awb_number": "871451979049",
                 "fedex_pickup_status": "IN_TRANSIT"}]
        tier, dns = _compute_tier_inline(rows)
        self.assertEqual(tier, 4)

    def test_awb_with_delivered_returns_tier_4(self):
        rows = [{"name": "MAT-DN-001", "fedex_awb_number": "871451979049",
                 "fedex_pickup_status": "DELIVERED"}]
        tier, dns = _compute_tier_inline(rows)
        self.assertEqual(tier, 4)

    def test_multiple_dns_max_state_wins(self):
        # Two DNs: one tier-2 (no AWB), one tier-3 (AWB no pickup)
        # The has_awb filter promotes overall to tier 3
        rows = [
            {"name": "MAT-DN-001", "fedex_awb_number": None, "fedex_pickup_status": None},
            {"name": "MAT-DN-002", "fedex_awb_number": "871451979049",
             "fedex_pickup_status": None},
        ]
        tier, _ = _compute_tier_inline(rows)
        self.assertEqual(tier, 3, "Mixed states: tier 3 wins over tier 2")

    def test_case_insensitive_pickup_status(self):
        # Per impl: comparison .upper() — accept lowercase from API
        rows = [{"name": "MAT-DN-001", "fedex_awb_number": "871",
                 "fedex_pickup_status": "picked_up"}]
        tier, _ = _compute_tier_inline(rows)
        self.assertEqual(tier, 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
