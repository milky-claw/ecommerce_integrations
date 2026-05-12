"""2026-05-13 supplier-sheet-3-issues bundle — yei test coverage.

Run with: ``python3 ecommerce_integrations/shopify/tests/test_supplier_sheet_3_issues.py``.

Covers:
  * Issue #1 — per-order Address creation in `create_sales_order`:
      address_title sourced from shipping_address (recipient, not payer).
  * Issue #2 — `shopify_line_item_id` population on SO Items (B5 fix)
      and rewritten `_match_so_item` / `_match_shopify_line_to_so_item`
      with line-id primary + SKU-translation fallback.
  * Issue #3 — `cancel_order` lifts SI/DN guards, deletes dead-code
      writes, catches `frappe.ValidationError` on AWB-blocking DNs.

Test infrastructure mirrors ``test_b24_refunds.py``: mock Frappe
before connector code imports; AST/text source-invariant proofs +
behavioral tests via inline copies of patched functions.
"""

import ast
import os
import re
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

# ── Mock frappe before importing any connector code ─────────────────
frappe_mock = MagicMock()
frappe_mock._ = lambda x: x
frappe_mock.utils.cint = lambda x: int(x or 0)
frappe_mock.utils.cstr = lambda x: str(x) if x else ""
frappe_mock.utils.flt = lambda x: float(x or 0)
frappe_mock.flags = MagicMock()


# Custom ValidationError class so refund/order code can catch it.
class _FakeValidationError(Exception):
	pass


frappe_mock.ValidationError = _FakeValidationError
frappe_mock.exceptions.ValidationError = _FakeValidationError

sys.modules["frappe"] = frappe_mock
sys.modules["frappe.utils"] = frappe_mock.utils
sys.modules["frappe.tests"] = MagicMock()
sys.modules["frappe.custom.doctype.custom_field.custom_field"] = MagicMock()
sys.modules["frappe.model.document"] = MagicMock()

# Shopify SDK shims so refund.py and order.py import cleanly.
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

_constants_mock = MagicMock()
_constants_mock.ADDRESS_ID_FIELD = "shopify_address_id"
_constants_mock.CURRENT_SUBTOTAL_PRICE_FIELD = "shopify_current_subtotal_price"
_constants_mock.CURRENT_TOTAL_PRICE_FIELD = "shopify_current_total_price"
_constants_mock.CURRENT_TOTAL_DISCOUNTS_FIELD = "shopify_current_total_discounts"
_constants_mock.CUSTOMER_ID_FIELD = "shopify_customer_id"
_constants_mock.ITEM_REFUNDED_FIELD = "shopify_refunded"
_constants_mock.ITEM_REFUNDED_AT_FIELD = "shopify_refunded_at"
_constants_mock.ITEM_SHIP_METHOD_FIELD = "item_ship_method"
_constants_mock.LINE_ITEM_ID_FIELD = "shopify_line_item_id"
_constants_mock.ORDER_DISCOUNT_CODES_FIELD = "shopify_discount_codes"
_constants_mock.ORDER_FINANCIAL_STATUS_FIELD = "shopify_financial_status"
_constants_mock.ORDER_FULFILLMENT_SOURCE_FIELD = "shopify_fulfillment_source"
_constants_mock.ORDER_FULFILLMENT_STATUS_FIELD = "shopify_fulfillment_status"
_constants_mock.ORDER_ID_FIELD = "shopify_order_id"
_constants_mock.ORDER_ITEM_PROPERTIES_FIELD = "shopify_line_item_properties"
_constants_mock.ORDER_NUMBER_FIELD = "shopify_order_number"
_constants_mock.ORDER_STATUS_FIELD = "shopify_order_status"
_constants_mock.ORDER_TIP_AMOUNT_FIELD = "shopify_tip_amount"
_constants_mock.SETTING_DOCTYPE = "Shopify Setting"
_constants_mock.SO_SHIP_CLASS_FIELD = "so_ship_class"
_constants_mock.UNMATCHED_ITEM_CODE = "MISC-MANUAL"
_constants_mock.EVENT_MAPPER = {}
sys.modules["ecommerce_integrations.shopify.constants"] = _constants_mock


SHOPIFY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORDER_PY = os.path.join(SHOPIFY_DIR, "order.py")
REFUND_PY = os.path.join(SHOPIFY_DIR, "refund.py")
SETTING_PY = os.path.join(
	SHOPIFY_DIR, "doctype", "shopify_setting", "shopify_setting.py",
)
CONSTANTS_PY = os.path.join(SHOPIFY_DIR, "constants.py")
PATCH_PY = os.path.join(
	os.path.dirname(SHOPIFY_DIR), "patches", "add_shopify_line_item_id_fields.py",
)
PATCHES_TXT = os.path.join(
	os.path.dirname(SHOPIFY_DIR), "patches.txt",
)


def _source_of(fn_name: str, source: str) -> str:
	tree = ast.parse(source)
	for node in tree.body:
		if isinstance(node, ast.FunctionDef) and node.name == fn_name:
			return ast.get_source_segment(source, node) or ""
	raise AssertionError(f"function {fn_name!r} not found")


# ───────────────────────────────────────────────────────────────────────
# Schema-bump source-invariants (Fork A authorization)
# ───────────────────────────────────────────────────────────────────────


class TestSchemaBumpSourceInvariant(unittest.TestCase):

	@classmethod
	def setUpClass(cls):
		with open(SETTING_PY, "r", encoding="utf-8") as f:
			cls.setting_src = f.read()
		with open(CONSTANTS_PY, "r", encoding="utf-8") as f:
			cls.constants_src = f.read()
		with open(PATCHES_TXT, "r", encoding="utf-8") as f:
			cls.patches_txt = f.read()
		with open(PATCH_PY, "r", encoding="utf-8") as f:
			cls.patch_src = f.read()

	def test_constants_declares_line_item_id_field(self):
		self.assertIn(
			'LINE_ITEM_ID_FIELD = "shopify_line_item_id"', self.constants_src,
			"LINE_ITEM_ID_FIELD constant must be declared",
		)

	def _slice_block(self, doctype: str) -> str:
		marker = f'"{doctype}": ['
		start = self.setting_src.find(marker)
		self.assertNotEqual(start, -1, f"'{doctype}' block not found")
		after_start = start + len(marker)
		next_key = re.search(
			r'\n\t\t"[\w ]+":\s*\[', self.setting_src[after_start:],
		)
		end = after_start + (
			next_key.start() if next_key else len(self.setting_src)
		)
		return self.setting_src[start:end]

	def test_so_item_block_declares_line_item_id(self):
		block = self._slice_block("Sales Order Item")
		self.assertIn("LINE_ITEM_ID_FIELD", block,
			"SO Item block must declare LINE_ITEM_ID_FIELD (Issue #2 B5 fix)")

	def test_dn_item_block_declares_line_item_id(self):
		block = self._slice_block("Delivery Note Item")
		self.assertIn("LINE_ITEM_ID_FIELD", block,
			"DN Item block must declare LINE_ITEM_ID_FIELD (Issue #2 cascade)")

	def test_line_item_id_field_is_data_with_allow_on_submit(self):
		# First match is SO Item; second is DN Item. Both must be Data
		# + allow_on_submit=1 (refund webhook fires on submitted docs).
		matches = list(re.finditer(
			r'fieldname\s*=\s*LINE_ITEM_ID_FIELD\b[^)]*?\)',
			self.setting_src, re.DOTALL,
		))
		self.assertEqual(len(matches), 2,
			"LINE_ITEM_ID_FIELD must appear exactly twice (SO Item + DN Item)")
		for m in matches:
			block = m.group(0)
			self.assertIn('"Data"', block,
				f"LINE_ITEM_ID_FIELD must be Data type: {block!r}")
			self.assertIn("allow_on_submit=1", block,
				f"LINE_ITEM_ID_FIELD must allow_on_submit=1: {block!r}")

	def test_patch_module_exists(self):
		self.assertTrue(os.path.exists(PATCH_PY),
			"Install patch add_shopify_line_item_id_fields.py must exist")
		self.assertIn("setup_custom_fields", self.patch_src,
			"Install patch must call setup_custom_fields()")

	def test_patch_registered_in_patches_txt(self):
		self.assertIn(
			"ecommerce_integrations.patches.add_shopify_line_item_id_fields",
			self.patches_txt,
			"Install patch must be registered in patches.txt",
		)


# ───────────────────────────────────────────────────────────────────────
# Issue #1 — per-order Address creation (source-invariant + behavioural)
# ───────────────────────────────────────────────────────────────────────


class TestIssue1PerOrderAddressSourceInvariant(unittest.TestCase):

	@classmethod
	def setUpClass(cls):
		with open(ORDER_PY, "r", encoding="utf-8") as f:
			cls.source = f.read()

	def test_helper_defined(self):
		self.assertIn(
			"def _create_per_order_shipping_address(", self.source,
			"Issue #1: per-order shipping Address helper must exist",
		)

	def test_helper_reads_shipping_address_first_last_name(self):
		body = _source_of("_create_per_order_shipping_address", self.source)
		self.assertIn('shipping_address', body,
			"helper must read order.shipping_address (not customer.billing)")
		self.assertIn('first_name', body)
		self.assertIn('last_name', body)
		self.assertIn('address_title', body)
		self.assertIn('"Shipping"', body,
			"new Address must be address_type='Shipping'")
		self.assertIn('link_doctype', body,
			"new Address must be linked to the Customer via dynamic-link")

	def test_create_sales_order_calls_helper(self):
		body = _source_of("create_sales_order", self.source)
		self.assertIn("_create_per_order_shipping_address(", body,
			"create_sales_order must invoke the per-order Address helper")
		self.assertIn('"shipping_address_name"', body,
			"create_sales_order must set so_dict['shipping_address_name']")


class TestIssue1PerOrderAddressBehaviour(unittest.TestCase):
	"""Behavioural test on the real helper, mocking frappe.get_doc."""

	@classmethod
	def setUpClass(cls):
		cls.order = _import_order_module()

	def _make_helper(self):
		return self.order._create_per_order_shipping_address

	def setUp(self):
		# Reset the frappe.get_doc mock between tests.
		frappe_mock.get_doc.reset_mock()
		# Default: get_doc returns a SimpleNamespace whose .insert()
		# returns None and .name reads back the simulated docname.
		def _get_doc(payload, *args, **kwargs):
			doc = MagicMock()
			doc.name = "ADDR-MOCK-001"
			doc.flags = MagicMock()
			doc.insert = MagicMock(return_value=None)
			doc._payload = payload
			return doc
		frappe_mock.get_doc.side_effect = _get_doc

	def test_helper_returns_address_name_when_shipping_address_present(self):
		fn = self._make_helper()
		order = {
			"id": 1234,
			"shipping_address": {
				"id": 99,
				"first_name": "Jane",
				"last_name": "Doe",
				"address1": "123 Main St",
				"city": "Boston",
				"province": "MA",
				"zip": "02101",
				"country": "United States",
				"phone": "+1-555-0100",
			},
		}
		result = fn(order, "John Smith")
		self.assertEqual(result, "ADDR-MOCK-001")
		# Inspect the payload passed to frappe.get_doc.
		call_args = frappe_mock.get_doc.call_args_list[0]
		payload = call_args[0][0]
		self.assertEqual(payload["address_title"], "Jane Doe",
			"address_title must be recipient (NOT 'John Smith' payer name)")
		self.assertEqual(payload["address_type"], "Shipping")
		self.assertEqual(payload["address_line1"], "123 Main St")
		self.assertEqual(payload["city"], "Boston")
		self.assertEqual(payload["pincode"], "02101")
		# Linked to the customer via dynamic-link.
		links = payload.get("links") or []
		self.assertEqual(len(links), 1)
		self.assertEqual(links[0]["link_doctype"], "Customer")
		self.assertEqual(links[0]["link_name"], "John Smith")

	def test_helper_returns_none_when_no_shipping_address(self):
		fn = self._make_helper()
		# Empty shipping_address → helper falls back (caller uses
		# Customer's primary Address).
		result = fn({"id": 1234, "shipping_address": {}}, "John Smith")
		self.assertIsNone(result)
		# No Address created.
		self.assertEqual(frappe_mock.get_doc.call_count, 0)

	def test_helper_falls_back_to_customer_name_when_recipient_empty(self):
		fn = self._make_helper()
		order = {
			"id": 1234,
			"shipping_address": {
				# Recipient names empty — use payer as last resort.
				"first_name": "",
				"last_name": "",
				"address1": "1 Empty Way",
				"city": "Nowhere",
				"province": "XX",
				"zip": "00000",
				"country": "X",
			},
		}
		result = fn(order, "Customer Falls Back")
		self.assertEqual(result, "ADDR-MOCK-001")
		payload = frappe_mock.get_doc.call_args_list[0][0][0]
		self.assertEqual(payload["address_title"], "Customer Falls Back")


# ───────────────────────────────────────────────────────────────────────
# Issue #2 — `shopify_line_item_id` population + match function rewrites
# ───────────────────────────────────────────────────────────────────────


class TestIssue2LineItemIdSourceInvariant(unittest.TestCase):

	@classmethod
	def setUpClass(cls):
		with open(ORDER_PY, "r", encoding="utf-8") as f:
			cls.order_src = f.read()
		with open(REFUND_PY, "r", encoding="utf-8") as f:
			cls.refund_src = f.read()

	def test_get_order_items_stamps_line_item_id(self):
		body = _source_of("get_order_items", self.order_src)
		self.assertIn("LINE_ITEM_ID_FIELD", body,
			"get_order_items must stamp LINE_ITEM_ID_FIELD on each item_row")
		# Verify it reads shopify_item.get("id").
		self.assertIn('"id"', body,
			"get_order_items must read line_items[i].id")

	def test_match_so_item_uses_line_item_id_primary(self):
		body = _source_of("_match_so_item", self.refund_src)
		# Must check shopify_line_item_id first.
		self.assertIn("LINE_ITEM_ID_FIELD", body,
			"_match_so_item must use LINE_ITEM_ID_FIELD as primary match key")
		self.assertIn("get_item_code", body,
			"_match_so_item must translate Shopify SKU via get_item_code "
			"(fixes the silent-Baumera-refund-fail bug)")

	def test_match_shopify_line_to_so_item_uses_line_id_primary(self):
		body = _source_of("_match_shopify_line_to_so_item", self.refund_src)
		self.assertIn("LINE_ITEM_ID_FIELD", body,
			"_match_shopify_line_to_so_item must use LINE_ITEM_ID_FIELD")
		self.assertIn("get_item_code", body,
			"_match_shopify_line_to_so_item must translate Shopify SKU")


def _import_refund_module():
	"""Import refund.py by file spec — bypasses the parent-package mock."""
	import importlib.util
	spec = importlib.util.spec_from_file_location(
		"_refund_under_test_3issues", REFUND_PY,
	)
	if spec is None or spec.loader is None:
		raise ImportError("could not load refund.py spec")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _import_order_module():
	"""Import order.py by file spec — bypasses the parent-package mock."""
	import importlib.util
	# order.py also imports from `freight_class` + `customer` + `product`
	# at module load — stub those out as well.
	sys.modules.setdefault(
		"ecommerce_integrations.shopify.freight_class", MagicMock(),
	)
	sys.modules.setdefault(
		"ecommerce_integrations.shopify.customer", MagicMock(),
	)
	sys.modules.setdefault(
		"ecommerce_integrations.shopify.product", MagicMock(),
	)
	sys.modules.setdefault(
		"ecommerce_integrations.shopify.utils", MagicMock(),
	)
	sys.modules.setdefault(
		"ecommerce_integrations.utils", MagicMock(),
	)
	sys.modules.setdefault(
		"ecommerce_integrations.utils.price_list", MagicMock(),
	)
	sys.modules.setdefault(
		"ecommerce_integrations.utils.taxation", MagicMock(),
	)
	spec = importlib.util.spec_from_file_location(
		"_order_under_test_3issues", ORDER_PY,
	)
	if spec is None or spec.loader is None:
		raise ImportError("could not load order.py spec")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


import importlib.util  # noqa: E402  (used by helpers above)


class TestIssue2MatchSoItemBehaviour(unittest.TestCase):

	@classmethod
	def setUpClass(cls):
		cls.refund = _import_refund_module()

	def _make_match_fn(self):
		return self.refund._match_so_item

	def _so_item(self, name, item_code, item_name="", rate=0.0,
				 line_item_id=""):
		ns = SimpleNamespace(
			name=name, item_code=item_code, item_name=item_name, rate=rate,
		)
		# `.get(field)` must work for the LINE_ITEM_ID_FIELD probe.
		ns_dict = {"shopify_line_item_id": line_item_id}
		ns.get = lambda key, default=None: ns_dict.get(key, default)
		return ns

	def _sales_order(self, items):
		return SimpleNamespace(items=items)

	def test_matches_by_line_id_primary(self):
		fn = self._make_match_fn()
		so = self._sales_order([
			self._so_item("a", "WMP-FUL-1400X0300", line_item_id="111"),
			self._so_item("b", "WMP-FUL-1400X0300", line_item_id="222"),
		])
		hit = fn(so, {"id": 222, "sku": "WMP-FUL-1400X0300", "price": 999})
		self.assertEqual(hit.name, "b",
			"Multi-line same-item_code must disambiguate by line_id")

	def test_falls_back_to_sku_when_line_id_missing(self):
		fn = self._make_match_fn()
		# Legacy SO Item with no line_id stored.
		so = self._sales_order([
			self._so_item("legacy", "WMP-FUL-1400X0300"),
		])
		hit = fn(so, {"id": 333, "sku": "WMP-FUL-1400X0300", "price": 100})
		self.assertEqual(hit.name, "legacy",
			"Legacy row (no line_id) must still match via SKU")

	def test_no_match_returns_none(self):
		fn = self._make_match_fn()
		so = self._sales_order([
			self._so_item("a", "WMP-FUL-1400X0300", line_item_id="111"),
		])
		hit = fn(so, {"id": 999, "sku": "DOES-NOT-EXIST"})
		self.assertIsNone(hit)

	def test_returns_first_match_when_ambiguous_same_rate(self):
		fn = self._make_match_fn()
		# Same item_code, same rate, no line_id — return first (legacy
		# tie-break preserved).
		so = self._sales_order([
			self._so_item("a", "WMP-FUL-1400X0300", rate=500.0),
			self._so_item("b", "WMP-FUL-1400X0300", rate=500.0),
		])
		hit = fn(so, {"id": 1, "sku": "WMP-FUL-1400X0300", "price": 500.0})
		self.assertIn(hit.name, ("a", "b"))


# ───────────────────────────────────────────────────────────────────────
# Issue #3 — cancel_order rewrite (source-invariant)
# ───────────────────────────────────────────────────────────────────────


class TestIssue3CancelOrderSourceInvariant(unittest.TestCase):

	@classmethod
	def setUpClass(cls):
		with open(ORDER_PY, "r", encoding="utf-8") as f:
			cls.source = f.read()

	def test_cancel_order_lifts_dn_guard(self):
		body = _source_of("cancel_order", self.source)
		# No longer guards on `not delivery_notes`.
		self.assertNotRegex(body, r'not\s+delivery_notes\s+and\s+sales_order\.docstatus',
			"cancel_order must no longer suppress .cancel() when DN exists")

	def test_cancel_order_unguarded_calls_so_cancel(self):
		body = _source_of("cancel_order", self.source)
		self.assertRegex(body, r'sales_order\.cancel\(\)',
			"cancel_order must always attempt sales_order.cancel() "
			"on submitted SOs (Issue #3 single-signal discipline)")

	def test_cancel_order_catches_validation_error(self):
		body = _source_of("cancel_order", self.source)
		self.assertIn("frappe.ValidationError", body,
			"cancel_order must catch ValidationError raised by the "
			"alpha26 SO on_cancel hook when a DN has lr_no (AWB)")

	def test_cancel_order_deletes_si_dead_write(self):
		body = _source_of("cancel_order", self.source)
		# Strip docstring before grepping; the docstring legitimately
		# mentions "Sales Invoice" in the dead-code purge note.
		without_doc = re.sub(r'""".*?"""', "", body, count=1, flags=re.DOTALL)
		self.assertNotRegex(without_doc, r'set_value\(\s*["\']Sales Invoice["\']',
			"cancel_order must no longer write ORDER_STATUS_FIELD onto "
			"Sales Invoice (dead-code purge — zero readers, "
			"sync_sales_invoice=0)")

	def test_cancel_order_deletes_dn_dead_write(self):
		body = _source_of("cancel_order", self.source)
		without_doc = re.sub(r'""".*?"""', "", body, count=1, flags=re.DOTALL)
		# The lookup of `Delivery Note` for the loop is gone.
		self.assertNotRegex(without_doc, r'frappe\.db\.get_list\(["\']Delivery Note["\']',
			"cancel_order must no longer fetch DN rows just to "
			"set_value(ORDER_STATUS_FIELD) (dead-code purge — "
			"zero readers in ygf source)")
		self.assertNotRegex(without_doc, r'set_value\(\s*["\']Delivery Note["\']',
			"cancel_order must no longer set ORDER_STATUS_FIELD onto "
			"individual Delivery Notes")

	def test_cancel_order_demoted_status_field_still_written_on_so(self):
		body = _source_of("cancel_order", self.source)
		# The SO-level set_value(ORDER_STATUS_FIELD) is preserved
		# (diagnostic/financial-mirror, not cancellation decision).
		self.assertRegex(body, r'set_value\(\s*["\']Sales Order["\']',
			"cancel_order must keep the SO-level ORDER_STATUS_FIELD "
			"write (diagnostic mirror)")


# ───────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
	unittest.main(verbosity=2)
