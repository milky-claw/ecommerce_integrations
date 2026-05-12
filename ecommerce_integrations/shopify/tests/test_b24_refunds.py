"""B24 standalone unit tests — refund handling + current_quantity flip.

Run with: python3 ecommerce_integrations/shopify/tests/test_b24_refunds.py

These tests mock Frappe so they execute without a bench. Two categories:

  * Source-invariant tests (AST/text inspection of the actual production
    files) — proof that the production code does not regress to bare
    `quantity` reads or amend logic.
  * Behavioral tests on inlined copies of the patched functions —
    same pattern as test_connector_patches.py.

Triggered by orders #2993, #4039 + 478-historical-orders refund
backlog. Design: stages/04c-data-sync/references/b24-impl-removed-items.md
"""

import ast
import os
import re
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

# ── Mock frappe before importing any connector code (mirror of
#    test_connector_patches.py, kept minimal to what B24 tests need).
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

# Mocks needed by refund.py at import time. ``temp_shopify_session``
# is a decorator from ecommerce_integrations.shopify.connection — when
# resolved through MagicMock attribute access, it acts as an identity
# decorator (MagicMock returns MagicMock; calling a MagicMock on a
# function returns a MagicMock; that's fine for import-time wiring
# since the pure-helper tests don't invoke the decorated function).
shopify_mock = MagicMock()
sys.modules["shopify"] = shopify_mock
sys.modules["shopify.resources"] = MagicMock()
sys.modules["shopify.collection"] = MagicMock()

# ecommerce_integrations.shopify package + its connection helper.
_ei_shopify_mock = MagicMock()
_ei_shopify_mock.connection = MagicMock()
# Make temp_shopify_session an identity decorator so the wrapped
# function survives import-time as a real callable.
_ei_shopify_mock.connection.temp_shopify_session = lambda fn: fn
sys.modules["ecommerce_integrations"] = MagicMock()
sys.modules["ecommerce_integrations.shopify"] = _ei_shopify_mock
sys.modules["ecommerce_integrations.shopify.connection"] = _ei_shopify_mock.connection
# The real constants module is loaded via importlib later — but the
# refund.py module-level `from ecommerce_integrations.shopify.constants
# import ...` needs SOMETHING. Provide a MagicMock that returns the
# field-name strings (matches the real constants for our tests).
_constants_mock = MagicMock()
_constants_mock.CURRENT_SUBTOTAL_PRICE_FIELD = "shopify_current_subtotal_price"
_constants_mock.CURRENT_TOTAL_PRICE_FIELD = "shopify_current_total_price"
_constants_mock.CURRENT_TOTAL_DISCOUNTS_FIELD = "shopify_current_total_discounts"
_constants_mock.ITEM_REFUNDED_FIELD = "shopify_refunded"
_constants_mock.ITEM_REFUNDED_AT_FIELD = "shopify_refunded_at"
_constants_mock.ORDER_ID_FIELD = "shopify_order_id"
_constants_mock.SETTING_DOCTYPE = "Shopify Setting"
sys.modules["ecommerce_integrations.shopify.constants"] = _constants_mock


SHOPIFY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORDER_PY = os.path.join(SHOPIFY_DIR, "order.py")


# ───────────────────────────────────────────────────────────────────────
# Source-invariant tests on the production order.py
# ───────────────────────────────────────────────────────────────────────


def _source_of(fn_name: str, source: str) -> str:
    """Return the raw source code of a top-level function `fn_name` from
    a parsed Python source string."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {fn_name!r} not found in source")


class TestB24bCurrentQuantitySourceInvariant(unittest.TestCase):
    """B24b: assert the 4 target functions in order.py read
    `current_quantity` (with fallback), not bare `quantity`.

    Triggered by orders #2993, #4039 — connector reads `quantity` (the
    order-creation value) and ignores `current_quantity` (post-refund).
    """

    @classmethod
    def setUpClass(cls):
        with open(ORDER_PY, "r", encoding="utf-8") as f:
            cls.source = f.read()

    # ── _separate_tips (call site :335) ──────────────────────────────
    def test_separate_tips_reads_current_quantity(self):
        body = _source_of("_separate_tips", self.source)
        self.assertIn("current_quantity", body,
            "B24b: _separate_tips must read current_quantity from tip line_items")

    def test_separate_tips_has_no_bare_quantity_read(self):
        body = _source_of("_separate_tips", self.source)
        # `.get("quantity", ...)` is allowed ONLY as the fallback inside a
        # `.get("current_quantity", .get("quantity", ...))` pattern. So
        # every line containing `.get("quantity"` must also reference
        # `current_quantity` on the same line.
        for line in body.splitlines():
            if re.search(r'\.get\(\s*["\']quantity["\']', line):
                self.assertIn("current_quantity", line,
                    f"B24b: _separate_tips has bare quantity read without "
                    f"current_quantity fallback context: {line!r}")

    # ── get_order_items (call site :377) ─────────────────────────────
    def test_get_order_items_reads_current_quantity(self):
        body = _source_of("get_order_items", self.source)
        self.assertIn("current_quantity", body,
            "B24b: get_order_items must read current_quantity")

    def test_get_order_items_has_no_bare_quantity_read(self):
        body = _source_of("get_order_items", self.source)
        for line in body.splitlines():
            if re.search(r'\.get\(\s*["\']quantity["\']', line):
                self.assertIn("current_quantity", line,
                    f"B24b: get_order_items has bare quantity read without "
                    f"current_quantity fallback context: {line!r}")

    def test_get_order_items_skips_current_quantity_zero(self):
        body = _source_of("get_order_items", self.source)
        # Must skip refunded-at-creation lines. Look for a `current_qty`
        # or `current_quantity` zero-guard plus a `continue`.
        has_zero_guard = (
            re.search(r'current_q(uantity|ty)\s*==\s*0', body)
            or re.search(r'not\s+current_q(uantity|ty)\b', body)
        )
        has_continue = "continue" in body
        self.assertTrue(has_zero_guard and has_continue,
            "B24b: get_order_items must skip lines with current_quantity == 0 "
            "(no SO Item row created for refunded-at-creation lines)")

    # ── _get_item_price (call site :477) ─────────────────────────────
    def test_get_item_price_reads_current_quantity(self):
        body = _source_of("_get_item_price", self.source)
        self.assertIn("current_quantity", body,
            "B24b: _get_item_price must read current_quantity for qty divisor")

    def test_get_item_price_has_no_bare_quantity_read(self):
        body = _source_of("_get_item_price", self.source)
        for line in body.splitlines():
            if re.search(r'\.get\(\s*["\']quantity["\']', line):
                self.assertIn("current_quantity", line,
                    f"B24b: _get_item_price has bare quantity read without "
                    f"current_quantity fallback context: {line!r}")

    # ── _build_order_edit_diff (call site :801, :811) ─────────────────
    def test_build_order_edit_diff_reads_current_quantity(self):
        body = _source_of("_build_order_edit_diff", self.source)
        self.assertIn("current_quantity", body,
            "B24b: _build_order_edit_diff must read current_quantity")

    def test_build_order_edit_diff_has_no_bare_quantity_read(self):
        body = _source_of("_build_order_edit_diff", self.source)
        for line in body.splitlines():
            if re.search(r'\.get\(\s*["\']quantity["\']', line):
                self.assertIn("current_quantity", line,
                    f"B24b: _build_order_edit_diff has bare quantity read "
                    f"without current_quantity fallback context: {line!r}")


# ───────────────────────────────────────────────────────────────────────
# Behavioral tests on inlined copies (pattern from test_connector_patches.py)
# ───────────────────────────────────────────────────────────────────────


def _separate_tips_inlined(line_items):
    """Patched B24b copy: reads current_quantity with fallback to quantity."""
    regular_items = []
    tip_total = 0.0
    for item in line_items:
        if str(item.get("title") or "").strip().lower() == "tip":
            qty = int(item.get("current_quantity", item.get("quantity", 1)) or 0)
            tip_total += float(item.get("price", 0)) * qty
        else:
            regular_items.append(item)
    return regular_items, tip_total


class TestB24bSeparateTipsBehavior(unittest.TestCase):
    """Tip line_items use current_quantity. Refunded tips contribute 0."""

    def test_tip_with_current_quantity_zero_contributes_nothing(self):
        items = [
            {"title": "Tip", "price": "5.00", "quantity": 1, "current_quantity": 0},
            {"title": "Greenhouse", "price": "1000", "quantity": 1, "current_quantity": 1},
        ]
        regular, tip_total = _separate_tips_inlined(items)
        self.assertEqual(tip_total, 0.0)
        self.assertEqual(len(regular), 1)

    def test_tip_with_current_quantity_one_contributes_normally(self):
        items = [{"title": "Tip", "price": "5.00", "quantity": 1, "current_quantity": 1}]
        regular, tip_total = _separate_tips_inlined(items)
        self.assertEqual(tip_total, 5.0)
        self.assertEqual(regular, [])

    def test_tip_without_current_quantity_falls_back_to_quantity(self):
        # Older Shopify API responses may omit current_quantity.
        items = [{"title": "Tip", "price": "3.00", "quantity": 2}]
        _, tip_total = _separate_tips_inlined(items)
        self.assertEqual(tip_total, 6.0)


# ───────────────────────────────────────────────────────────────────────
# B24a — constants.py wiring: new field-name constants + webhook subscription
# ───────────────────────────────────────────────────────────────────────


class TestB24aConstantsWiring(unittest.TestCase):
    """B24a: verify constants.py declares the refund-field names and
    subscribes the `refunds/create` webhook to the refund.py handler."""

    @classmethod
    def setUpClass(cls):
        # Read constants.py source directly to avoid import-time frappe deps.
        with open(os.path.join(SHOPIFY_DIR, "constants.py"), "r", encoding="utf-8") as f:
            cls.source = f.read()

    def test_current_subtotal_price_field_constant_present(self):
        self.assertIn('CURRENT_SUBTOTAL_PRICE_FIELD = "shopify_current_subtotal_price"',
            self.source,
            "B24a: CURRENT_SUBTOTAL_PRICE_FIELD must be declared in constants.py")

    def test_current_total_price_field_constant_present(self):
        self.assertIn('CURRENT_TOTAL_PRICE_FIELD = "shopify_current_total_price"',
            self.source)

    def test_current_total_discounts_field_constant_present(self):
        self.assertIn('CURRENT_TOTAL_DISCOUNTS_FIELD = "shopify_current_total_discounts"',
            self.source)

    def test_item_refunded_field_constant_present(self):
        self.assertIn('ITEM_REFUNDED_FIELD = "shopify_refunded"', self.source)

    def test_item_refunded_at_field_constant_present(self):
        self.assertIn('ITEM_REFUNDED_AT_FIELD = "shopify_refunded_at"', self.source)

    def test_refunds_create_in_webhook_events(self):
        # Parse WEBHOOK_EVENTS list and assert "refunds/create" is listed.
        # Source-level check, not import, to stay frappe-free.
        match = re.search(r'WEBHOOK_EVENTS\s*=\s*\[(.*?)\]', self.source, re.DOTALL)
        self.assertIsNotNone(match, "WEBHOOK_EVENTS list not found")
        self.assertIn('"refunds/create"', match.group(1),
            "B24a: 'refunds/create' must be in WEBHOOK_EVENTS")

    def test_event_mapper_routes_refunds_create_to_handler(self):
        match = re.search(r'EVENT_MAPPER\s*=\s*\{(.*?)\}', self.source, re.DOTALL)
        self.assertIsNotNone(match, "EVENT_MAPPER dict not found")
        mapper = match.group(1)
        self.assertIn('"refunds/create"', mapper,
            "B24a: 'refunds/create' must be a key in EVENT_MAPPER")
        self.assertIn(
            "ecommerce_integrations.shopify.refund.handle_refund_created",
            mapper,
            "B24a: 'refunds/create' must map to refund.handle_refund_created")


# ───────────────────────────────────────────────────────────────────────
# B24 — setup_custom_fields: 7 new Custom Field specs
# ───────────────────────────────────────────────────────────────────────


class TestB24SetupCustomFields(unittest.TestCase):
    """B24: verify setup_custom_fields() in shopify_setting.py declares
    the 7 new Custom Fields (3 SO + 2 SO Item + 2 DN Item).

    Source-level inspection, frappe-free."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(
            SHOPIFY_DIR, "doctype", "shopify_setting", "shopify_setting.py")
        with open(path, "r", encoding="utf-8") as f:
            cls.source = f.read()

    # ── SO totals (3 Currency, read-only) ────────────────────────────
    def test_so_current_subtotal_price_declared(self):
        self.assertRegex(self.source,
            r'fieldname\s*=\s*CURRENT_SUBTOTAL_PRICE_FIELD',
            "B24c: SO.shopify_current_subtotal_price must be set up")

    def test_so_current_total_price_declared(self):
        self.assertRegex(self.source,
            r'fieldname\s*=\s*CURRENT_TOTAL_PRICE_FIELD')

    def test_so_current_total_discounts_declared(self):
        self.assertRegex(self.source,
            r'fieldname\s*=\s*CURRENT_TOTAL_DISCOUNTS_FIELD')

    def test_so_current_totals_are_currency_read_only(self):
        # Heuristic: find the SO-level current_* block and assert the
        # surrounding spec has Currency + read_only=1.
        m = re.search(
            r'fieldname\s*=\s*CURRENT_SUBTOTAL_PRICE_FIELD[^}]*?\}',
            self.source, re.DOTALL)
        self.assertIsNotNone(m, "CURRENT_SUBTOTAL_PRICE_FIELD spec not found")
        block = m.group(0)
        self.assertIn('"Currency"', block)
        self.assertIn("read_only=1", block)

    # ── SO Item refund flag + refunded_at (read-only, allow_on_submit) ─
    def _slice_block(self, doctype: str) -> str:
        """Carve out the doctype's custom-fields block by string anchor —
        robust against nested `[]` in description strings."""
        start_marker = f'"{doctype}": ['
        start = self.source.find(start_marker)
        self.assertNotEqual(start, -1, f"'{doctype}' block not found")
        # End at the line that closes this doctype block: a `],` at the
        # start of a line with 2 indent levels (2 tabs). The next entry
        # always starts at column 0+2tabs with a quoted doctype name.
        # Find the next occurrence of `\n\t\t"` (next dict key) and back
        # up to the preceding `]` to mark end-of-block.
        after_start = start + len(start_marker)
        next_key = re.search(r'\n\t\t"[\w ]+":\s*\[', self.source[after_start:])
        end = after_start + (next_key.start() if next_key else len(self.source))
        return self.source[start:end]

    def test_so_item_refunded_flag_declared(self):
        block = self._slice_block("Sales Order Item")
        self.assertIn("ITEM_REFUNDED_FIELD", block,
            "B24a: SO Item.shopify_refunded must be set up")

    def test_so_item_refunded_at_declared(self):
        block = self._slice_block("Sales Order Item")
        self.assertIn("ITEM_REFUNDED_AT_FIELD", block,
            "B24a: SO Item.shopify_refunded_at must be set up")

    def test_so_item_refunded_allows_on_submit(self):
        # The refund webhook fires after SO submit — Custom Field must
        # accept writes on submitted docs.
        m = re.search(
            r'fieldname\s*=\s*ITEM_REFUNDED_FIELD,(.*?)\)',
            self.source, re.DOTALL)
        # Find the first SO Item match (DN Item is a second match later).
        self.assertIsNotNone(m, "ITEM_REFUNDED_FIELD spec not found")
        self.assertIn("allow_on_submit=1", m.group(1),
            "B24a: ITEM_REFUNDED_FIELD must have allow_on_submit=1")

    # ── DN Item refund flag + refunded_at ────────────────────────────
    def test_dn_item_block_present(self):
        # yei didn't previously install Custom Fields on DN Item.
        # B24 introduces the doctype to yei's setup_custom_fields.
        self.assertIn('"Delivery Note Item"', self.source,
            "B24a: 'Delivery Note Item' block must be added to setup_custom_fields")

    def test_dn_item_refunded_flag_declared(self):
        block = self._slice_block("Delivery Note Item")
        self.assertIn("ITEM_REFUNDED_FIELD", block,
            "B24a: DN Item.shopify_refunded must be set up")

    def test_dn_item_refunded_at_declared(self):
        block = self._slice_block("Delivery Note Item")
        self.assertIn("ITEM_REFUNDED_AT_FIELD", block,
            "B24a: DN Item.shopify_refunded_at must be set up")

    def test_constants_imported_into_setup(self):
        # Must import the new constants from .constants.
        imports_block_match = re.search(
            r'from\s+ecommerce_integrations\.shopify\.constants\s+import\s+\((.*?)\)',
            self.source, re.DOTALL)
        self.assertIsNotNone(imports_block_match,
            "constants import block not found")
        imports = imports_block_match.group(1)
        for required in (
            "CURRENT_SUBTOTAL_PRICE_FIELD",
            "CURRENT_TOTAL_PRICE_FIELD",
            "CURRENT_TOTAL_DISCOUNTS_FIELD",
            "ITEM_REFUNDED_FIELD",
            "ITEM_REFUNDED_AT_FIELD",
        ):
            self.assertIn(required, imports,
                f"B24: {required} must be imported in shopify_setting.py")


# ───────────────────────────────────────────────────────────────────────
# B24a — refund.py module: existence, signatures, source-invariants,
#        and pure-function helpers (restock-type → ToDo title; priority).
# ───────────────────────────────────────────────────────────────────────


REFUND_PY = os.path.join(SHOPIFY_DIR, "refund.py")


class TestB24aRefundModuleExists(unittest.TestCase):
    """B24a: refund.py must exist with the API the design spec defines."""

    def test_refund_py_file_exists(self):
        self.assertTrue(os.path.isfile(REFUND_PY),
            f"B24a: refund.py must exist at {REFUND_PY}")


class TestB24aRefundSourceInvariants(unittest.TestCase):
    """B24a: refund.py must NOT contain amend logic. Catches regressions
    where future edits accidentally introduce SO/DN amendment code."""

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(REFUND_PY):
            cls.source = ""
            return
        with open(REFUND_PY, "r", encoding="utf-8") as f:
            cls.source = f.read()

    def test_no_copy_doc(self):
        # `frappe.copy_doc(...)` is the canonical "create amended version"
        # primitive. Forbidden — flag-only model.
        self.assertNotIn("copy_doc(", self.source,
            "B24a: refund.py must NOT use copy_doc — amend forbidden")

    def test_no_dot_cancel(self):
        # `.cancel()` cancels a submitted doctype. Forbidden.
        self.assertNotIn(".cancel()", self.source,
            "B24a: refund.py must NOT call .cancel() — amend forbidden")

    def test_no_dot_submit_outside_comments(self):
        # `.submit()` on existing docs would mutate state. Forbidden.
        # Strip comments before checking (comment narrative is fine).
        body_no_comments = re.sub(r'#.*', '', self.source)
        self.assertNotIn(".submit()", body_no_comments,
            "B24a: refund.py must NOT call .submit() — amend forbidden")


class TestB24aRefundSignatures(unittest.TestCase):
    """B24a: required top-level callables per impl spec."""

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(REFUND_PY):
            cls.tree = None
            return
        with open(REFUND_PY, "r", encoding="utf-8") as f:
            cls.tree = ast.parse(f.read())

    def _has_function(self, name: str) -> bool:
        if self.tree is None:
            return False
        return any(
            isinstance(node, ast.FunctionDef) and node.name == name
            for node in self.tree.body
        )

    def test_handle_refund_created_exists(self):
        self.assertTrue(self._has_function("handle_refund_created"))

    def test_apply_refund_exists(self):
        self.assertTrue(self._has_function("apply_refund"))

    def test_flag_so_item_refunded_exists(self):
        self.assertTrue(self._has_function("flag_so_item_refunded"))

    def test_flag_dn_item_refunded_exists(self):
        self.assertTrue(self._has_function("flag_dn_item_refunded"))

    def test_populate_current_totals_exists(self):
        self.assertTrue(self._has_function("populate_current_totals"))

    def test_reconcile_so_against_current_quantity_exists(self):
        self.assertTrue(self._has_function("reconcile_so_against_current_quantity"))


# ── Pure helpers (no Frappe deps — safe to import directly) ──────────


def _import_refund_module():
    """Import refund module fresh, with frappe already mocked above."""
    import importlib
    # ecommerce_integrations.shopify package is itself mocked at module
    # top — so we import the actual file by spec, bypassing the mock.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_refund_under_test", REFUND_PY)
    if spec is None or spec.loader is None:
        raise ImportError("could not load refund.py spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestB24aRestockTypeMapping(unittest.TestCase):
    """B24a: restock_type → ToDo title per impl spec."""

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(REFUND_PY):
            cls.refund = None
            return
        cls.refund = _import_refund_module()

    def test_no_restock_title(self):
        self.assertIsNotNone(self.refund,
            "refund.py must exist before this test runs")
        title = self.refund._restock_type_to_todo_title("no_restock", True)
        self.assertIn("no stock movement", title.lower())

    def test_return_title(self):
        title = self.refund._restock_type_to_todo_title("return", True)
        self.assertIn("inbound stock movement", title.lower())

    def test_cancel_title(self):
        title = self.refund._restock_type_to_todo_title("cancel", True)
        self.assertIn("reverse stock entry", title.lower())

    def test_legacy_restock_treated_as_return(self):
        legacy = self.refund._restock_type_to_todo_title("legacy_restock", True)
        ret = self.refund._restock_type_to_todo_title("return", True)
        self.assertEqual(legacy, ret)

    def test_empty_refund_line_items_is_shipping_only(self):
        # `has_refund_line_items=False` regardless of restock_type
        title = self.refund._restock_type_to_todo_title("cancel", False)
        self.assertIn("shipping-only", title.lower())


class TestB24aTodoPriority(unittest.TestCase):
    """B24a: ToDo priority — High for refund_total ≥ $1,000."""

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(REFUND_PY):
            cls.refund = None
            return
        cls.refund = _import_refund_module()

    def test_high_at_threshold(self):
        self.assertEqual(self.refund._todo_priority(1000), "High")

    def test_high_above_threshold(self):
        self.assertEqual(self.refund._todo_priority(5680.99), "High")

    def test_medium_below_threshold(self):
        self.assertEqual(self.refund._todo_priority(999.99), "Medium")

    def test_medium_for_zero(self):
        # Shipping-only refund with 0 line-item amount still gets a ToDo
        # at Medium priority.
        self.assertEqual(self.refund._todo_priority(0), "Medium")


# ───────────────────────────────────────────────────────────────────────
# B24a — behavioral tests for apply_refund + handle_refund_created.
#         Frappe + Shopify mocked; verifies wiring without a bench.
# ───────────────────────────────────────────────────────────────────────


def _make_fake_so(docstatus=1, items=None, name="SAL-ORD-2026-00720-1",
                  shopify_order_id="11085557530987"):
    """Build a fake Sales Order doc with `.items`, `.docstatus`, `.add_comment`."""
    so = MagicMock()
    so.name = name
    so.docstatus = docstatus
    so.items = items or []
    so.get = lambda key: shopify_order_id if key == "shopify_order_id" else None
    so.add_comment = MagicMock()
    return so


def _make_fake_so_item(name, item_code, qty=1, rate=0.0, item_name=""):
    soi = MagicMock()
    soi.name = name
    soi.item_code = item_code
    soi.item_name = item_name or item_code
    soi.qty = qty
    soi.rate = rate
    return soi


class TestB24aApplyRefundNoAmend(unittest.TestCase):
    """B24a wiring invariants — apply_refund must flag in place, never amend."""

    def setUp(self):
        # Import refund module fresh per test so the module-level frappe
        # mock reference can be patched per scenario.
        self.refund = _import_refund_module()
        # Patch frappe inside refund.py to per-test mocks. Refund.py imports
        # frappe at module-level; reach in and replace.
        self._frappe_patch = MagicMock()
        self._frappe_patch.db.set_value = MagicMock()
        self._frappe_patch.db.get_value = MagicMock(return_value=None)
        self._frappe_patch.get_all = MagicMock(return_value=[])
        self._frappe_patch.get_doc = MagicMock()
        self.refund.frappe = self._frappe_patch
        # Stub the Shopify-fetch helper to a fixed payload (avoid network).
        self.refund._fetch_shopify_order = MagicMock(return_value={
            "current_subtotal_price": "6428.80",
            "current_total_price": "6428.80",
            "current_total_discounts": "0.00",
            "refunds": [],
            "line_items": [],
        })

    def _build_2993_refund(self):
        """Refund #1083753693547 against SO #2993 — YG-Arrow-14 no_restock."""
        return {
            "id": 1083753693547,
            "order_id": 11085557530987,
            "created_at": "2026-04-08T12:00:00-04:00",
            "refund_line_items": [
                {
                    "quantity": 1,
                    "restock_type": "no_restock",
                    "subtotal": "3804.54",
                    "line_item": {
                        "id": 1,
                        "sku": "YG-Arrow-14",
                        "title": "YG-Arrow-14",
                        "price": "3804.54",
                    },
                }
            ],
            "transactions": [{"amount": "3804.54", "kind": "refund"}],
        }

    def test_apply_refund_submitted_so_no_copy_doc(self):
        items = [_make_fake_so_item("soi1", "YG-Arrow-14", qty=1, rate=3804.54)]
        so = _make_fake_so(docstatus=1, items=items)
        self.refund.apply_refund(self._build_2993_refund(), so)

        # The frappe mock must not have been asked to copy_doc / cancel.
        self.assertFalse(
            getattr(self._frappe_patch, "copy_doc", MagicMock()).called,
            "B24a: apply_refund must not call frappe.copy_doc")
        # No `.cancel()` call on the SO mock either.
        self.assertFalse(so.cancel.called,
            "B24a: apply_refund must not cancel the SO")

    def test_apply_refund_submitted_so_flags_so_item(self):
        items = [_make_fake_so_item("soi1", "YG-Arrow-14", qty=1, rate=3804.54)]
        so = _make_fake_so(docstatus=1, items=items)
        self.refund.apply_refund(self._build_2993_refund(), so)

        # frappe.db.set_value should have been called with:
        #   ("Sales Order Item", "soi1", {refunded_field: 1, ..._at: "..."})
        calls = self._frappe_patch.db.set_value.call_args_list
        flag_calls = [
            c for c in calls
            if len(c.args) >= 2 and c.args[0] == "Sales Order Item" and c.args[1] == "soi1"
        ]
        self.assertTrue(len(flag_calls) >= 1,
            "B24a: flag_so_item_refunded must call db.set_value on the SO Item")
        # Verify the values dict has refunded=1.
        values_arg = flag_calls[0].args[2]
        self.assertEqual(values_arg.get("shopify_refunded"), 1)
        self.assertEqual(values_arg.get("shopify_refunded_at"),
            "2026-04-08T12:00:00-04:00")

    def test_apply_refund_draft_so_reduces_qty_in_place(self):
        items = [_make_fake_so_item("soi1", "YG-Arrow-14", qty=2, rate=3804.54)]
        so = _make_fake_so(docstatus=0, items=items)
        self.refund.apply_refund(self._build_2993_refund(), so)

        # set_value on SO Item.qty with 2 - 1 = 1
        qty_calls = [
            c for c in self._frappe_patch.db.set_value.call_args_list
            if len(c.args) >= 3 and c.args[0] == "Sales Order Item"
            and c.args[1] == "soi1" and c.args[2] == "qty"
        ]
        self.assertEqual(len(qty_calls), 1,
            "B24a: draft SO refund must reduce qty in-place via db.set_value")
        self.assertEqual(qty_calls[0].args[3], 1,
            "B24a: refund of 1 against qty=2 leaves qty=1")

    def test_apply_refund_adds_comment_with_refund_id(self):
        items = [_make_fake_so_item("soi1", "YG-Arrow-14", qty=1, rate=3804.54)]
        so = _make_fake_so(docstatus=1, items=items)
        self.refund.apply_refund(self._build_2993_refund(), so)

        self.assertTrue(so.add_comment.called)
        comment_kwargs = so.add_comment.call_args
        # `text=...` keyword carries the refund_id.
        text = comment_kwargs.kwargs.get("text") or (
            comment_kwargs.args[1] if len(comment_kwargs.args) >= 2 else "")
        self.assertIn("1083753693547", text,
            "Comment must mention the refund_id for audit trail")

    def test_apply_refund_calls_populate_current_totals(self):
        items = [_make_fake_so_item("soi1", "YG-Arrow-14", qty=1, rate=3804.54)]
        so = _make_fake_so(docstatus=1, items=items)
        self.refund.apply_refund(self._build_2993_refund(), so)

        # set_value on Sales Order (the parent) with the 3 current_* fields.
        so_value_calls = [
            c for c in self._frappe_patch.db.set_value.call_args_list
            if len(c.args) >= 3 and c.args[0] == "Sales Order"
            and isinstance(c.args[2], dict)
            and "shopify_current_subtotal_price" in c.args[2]
        ]
        self.assertEqual(len(so_value_calls), 1,
            "B24c: populate_current_totals must write the 3 current_* fields")


class TestB24aHandleRefundCreatedIdempotency(unittest.TestCase):
    """B24a: handle_refund_created must short-circuit when a ToDo already
    references this refund_id (webhook re-fires are safe to ignore)."""

    def setUp(self):
        self.refund = _import_refund_module()
        self._frappe_patch = MagicMock()
        self.refund.frappe = self._frappe_patch

    def test_existing_todo_skips_dispatch(self):
        # SO exists, ToDo for this refund_id already exists.
        self._frappe_patch.db.get_value = MagicMock(return_value="SAL-ORD-001")
        self._frappe_patch.get_all = MagicMock(
            return_value=[{"name": "TODO-001"}])
        # apply_refund should NOT be called → spy on it via attribute replace.
        self.refund.apply_refund = MagicMock()

        payload = {"id": 999, "order_id": 11085557530987,
                   "refund_line_items": [], "transactions": []}
        self.refund.handle_refund_created(payload)

        self.refund.apply_refund.assert_not_called()

    def test_no_so_match_skips_dispatch(self):
        self._frappe_patch.db.get_value = MagicMock(return_value=None)
        self.refund.apply_refund = MagicMock()

        payload = {"id": 999, "order_id": 99999,
                   "refund_line_items": [], "transactions": []}
        self.refund.handle_refund_created(payload)

        self.refund.apply_refund.assert_not_called()

    def test_missing_order_id_skips(self):
        self.refund.apply_refund = MagicMock()
        self.refund.handle_refund_created({"id": 999})
        self.refund.apply_refund.assert_not_called()


if __name__ == "__main__":
    unittest.main()
