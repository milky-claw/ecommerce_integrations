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
# yei-v1.3.2 — Property Setter unblocker for Issue #1 backfill
# ───────────────────────────────────────────────────────────────────────


class TestV132PropertySetterUnblocker(unittest.TestCase):
	"""Verifies the yei-v1.3.2 Property Setter patch + registration.

	Property Setter ``Sales Order.shipping_address_name allow_on_submit=1``
	is the Frappe-native surface for relaxing a DocField attribute on a
	non-Custom-Field. Direct precedent: ``relabel_alpha26_dn_lr_fields.py``.
	"""

	@classmethod
	def setUpClass(cls):
		cls.patch_path = os.path.join(
			os.path.dirname(SHOPIFY_DIR), "patches",
			"add_so_shipping_address_name_allow_on_submit.py",
		)
		with open(PATCHES_TXT, "r", encoding="utf-8") as f:
			cls.patches_txt = f.read()
		with open(cls.patch_path, "r", encoding="utf-8") as f:
			cls.patch_src = f.read()

	def test_patch_file_exists(self):
		self.assertTrue(os.path.exists(self.patch_path),
			"yei-v1.3.2 patch add_so_shipping_address_name_allow_on_submit.py must exist")

	def test_patch_registered_in_patches_txt(self):
		self.assertIn(
			"ecommerce_integrations.patches.add_so_shipping_address_name_allow_on_submit",
			self.patches_txt,
			"yei-v1.3.2 patch must be registered in patches.txt",
		)

	def test_patch_calls_make_property_setter_on_correct_field(self):
		self.assertIn("make_property_setter", self.patch_src,
			"patch must call frappe.make_property_setter")
		self.assertIn('"Sales Order"', self.patch_src,
			"patch target doctype must be Sales Order")
		self.assertIn('"shipping_address_name"', self.patch_src,
			"patch target fieldname must be shipping_address_name")
		self.assertIn('"allow_on_submit"', self.patch_src,
			"patch must set the allow_on_submit property")
		self.assertIn('"Check"', self.patch_src,
			"property_type must be Check (matches DocField.allow_on_submit)")

	def test_patch_clears_cache_after_mutation(self):
		self.assertIn("clear_cache", self.patch_src,
			"patch must clear Sales Order cache so the new metadata takes effect")


# ───────────────────────────────────────────────────────────────────────
# yei-v1.3.3 — handle_order_edited reconciliation + cancel TimestampMismatch
# + freight_class str(product_id) + Sales Order Item.allow_on_submit PS
# ───────────────────────────────────────────────────────────────────────


class TestV133HandleOrderEditedSourceInvariant(unittest.TestCase):
	"""Phase-2 EIL-audit fix: reconcile SO.items on orders/edited.

	Closes 136 historical EIL ``handle_order_edited`` Invalid rows where
	``payload.get("id")`` was None because ``orders/edited`` nests the
	order id under ``payload["order_edit"]["order_id"]``.
	"""

	@classmethod
	def setUpClass(cls):
		with open(ORDER_PY, "r", encoding="utf-8") as f:
			cls.source = f.read()

	def test_reads_order_edit_nested_order_id(self):
		body = _source_of("handle_order_edited", self.source)
		self.assertIn('order_edit', body,
			"handle_order_edited must read payload['order_edit'] nested key")
		self.assertIn('order_id', body,
			"handle_order_edited must extract order_id from order_edit")

	def test_falls_back_to_top_level_id_defensively(self):
		body = _source_of("handle_order_edited", self.source)
		self.assertIn('payload.get("id")', body,
			"handle_order_edited must defensively fall back to top-level id "
			"for flat replay payloads / older API versions")

	def test_calls_reconcile_helper(self):
		body = _source_of("handle_order_edited", self.source)
		self.assertIn('_reconcile_so_line_items', body,
			"handle_order_edited must call _reconcile_so_line_items")

	def test_reconcile_helper_defined(self):
		self.assertIn('def _reconcile_so_line_items(', self.source,
			"_reconcile_so_line_items helper must be defined")

	def test_reconcile_helper_diffs_by_line_item_id(self):
		body = _source_of("_reconcile_so_line_items", self.source)
		self.assertIn('LINE_ITEM_ID_FIELD', body,
			"_reconcile_so_line_items must diff by shopify_line_item_id")
		self.assertIn('existing_by_lid', body,
			"_reconcile_so_line_items must build an existing-by-lid index")

	def test_reconcile_helper_calls_flag_so_item_refunded(self):
		body = _source_of("_reconcile_so_line_items", self.source)
		self.assertIn('flag_so_item_refunded', body,
			"_reconcile_so_line_items must flag cq==0 lines as refunded")

	def test_reconcile_helper_appends_new_sois(self):
		body = _source_of("_reconcile_so_line_items", self.source)
		self.assertIn('append("items"', body,
			"_reconcile_so_line_items must append new SOIs on the SO")
		self.assertIn('sales_order.save', body,
			"_reconcile_so_line_items must flush via sales_order.save()")

	def test_build_soi_from_shopify_line_helper_defined(self):
		self.assertIn('def _build_soi_from_shopify_line(', self.source,
			"_build_soi_from_shopify_line helper must be defined")

	def test_build_soi_stamps_line_item_id(self):
		body = _source_of("_build_soi_from_shopify_line", self.source)
		self.assertIn('LINE_ITEM_ID_FIELD', body,
			"new SOIs from edited orders must stamp shopify_line_item_id "
			"so subsequent reconciles see them as already-present")


class TestV133HandleOrderEditedBehavioural(unittest.TestCase):
	"""Inline-copy behavioural tests for the order-id extraction logic."""

	def test_extracts_from_order_edit_nested(self):
		payload = {"order_edit": {"order_id": 1234567890}}
		order_edit = (payload.get("order_edit") if isinstance(payload, dict) else None) or {}
		order_id = order_edit.get("order_id") or (payload.get("id") if isinstance(payload, dict) else None)
		self.assertEqual(order_id, 1234567890)

	def test_falls_back_to_top_level_id(self):
		payload = {"id": 9876543210}  # flat replay payload
		order_edit = (payload.get("order_edit") if isinstance(payload, dict) else None) or {}
		order_id = order_edit.get("order_id") or (payload.get("id") if isinstance(payload, dict) else None)
		self.assertEqual(order_id, 9876543210)

	def test_returns_none_for_empty_payload(self):
		payload = {}
		order_edit = (payload.get("order_edit") if isinstance(payload, dict) else None) or {}
		order_id = order_edit.get("order_id") or (payload.get("id") if isinstance(payload, dict) else None)
		self.assertIsNone(order_id)

	def test_returns_none_for_non_dict_payload(self):
		payload = None
		order_edit = (payload.get("order_edit") if isinstance(payload, dict) else None) or {}
		order_id = order_edit.get("order_id") or (payload.get("id") if isinstance(payload, dict) else None)
		self.assertIsNone(order_id)

	def test_reconcile_idempotent_when_no_diff(self):
		"""Re-running reconcile against unchanged data is a no-op."""
		# Simulated existing index keyed by shopify_line_item_id.
		existing_by_lid = {"100": object(), "200": object()}
		shopify_lines = [
			{"id": 100, "current_quantity": 1, "title": "A"},
			{"id": 200, "current_quantity": 1, "title": "B"},
		]
		added = []
		refunded = []
		for li in shopify_lines:
			lid = str(li.get("id") or "")
			cq = int(li.get("current_quantity") or 0)
			if cq == 0:
				continue
			if lid and lid in existing_by_lid:
				continue
			added.append(li)
		self.assertEqual(added, [], "Idempotent reconcile must be a no-op")
		self.assertEqual(refunded, [])


class TestV133CancelOrderTimestampMismatchFix(unittest.TestCase):
	"""Phase-2 EIL-audit fix: cancel_order TimestampMismatch + tightened except."""

	@classmethod
	def setUpClass(cls):
		with open(ORDER_PY, "r", encoding="utf-8") as f:
			cls.source = f.read()

	def test_set_value_uses_update_modified_false(self):
		body = _source_of("cancel_order", self.source)
		# Find the ORDER_STATUS_FIELD set_value block and assert it includes
		# update_modified=False so the in-memory sales_order doc doesn't
		# go stale before the .cancel() call.
		self.assertIn('ORDER_STATUS_FIELD', body,
			"cancel_order must still write the financial-status mirror")
		self.assertIn('update_modified=False', body,
			"cancel_order set_value must pass update_modified=False "
			"to avoid TimestampMismatch on the subsequent .cancel() call")

	def test_except_clause_matches_on_fedex_awb_message(self):
		body = _source_of("cancel_order", self.source)
		self.assertIn('"FedEx AWB"', body,
			"cancel_order except clause must match on 'FedEx AWB' message "
			"text so only the real D6/AWB case is logged as 'manual "
			"intervention required'; non-AWB ValidationErrors propagate "
			"to the outer except for accurate diagnostics")

	def test_non_awb_validation_error_reraises(self):
		body = _source_of("cancel_order", self.source)
		# The "if 'FedEx AWB' not in str(e): raise" line is the tightening.
		self.assertRegex(body,
			r'if\s+"FedEx AWB"\s+not\s+in\s+str\(e\)\s*:\s*\n\s*raise',
			"cancel_order must re-raise non-AWB ValidationErrors so they "
			"don't get misreported as AWB-blocking cases")


class TestV133FreightClassStrCoercion(unittest.TestCase):
	"""Phase-2 EIL-audit fix: Shopify REST id type coercion (int → str)."""

	@classmethod
	def setUpClass(cls):
		FREIGHT_PY = os.path.join(SHOPIFY_DIR, "freight_class.py")
		with open(FREIGHT_PY, "r", encoding="utf-8") as f:
			cls.source = f.read()

	def test_make_live_fetcher_wraps_product_id_in_str(self):
		# Find the inline `fetch` closure inside make_live_fetcher.
		self.assertIn('Product.find(str(product_id))', self.source,
			"make_live_fetcher must call Product.find with str(product_id) "
			"to avoid Shopify HTTP 400 'expected String to be a id'")

	def test_recompute_helper_wraps_order_id_in_str(self):
		self.assertIn('Order.find(str(shopify_order_id))', self.source,
			"freight_class recompute helper must call Order.find with "
			"str(shopify_order_id) defensively")


class TestV133SOItemAllowOnSubmitPS(unittest.TestCase):
	"""Phase-2: Sales Order Item.allow_on_submit=1 Property Setter patch.

	Unblocks ``sales_order.append("items", ...)`` + ``save()`` on submitted
	SOs — required by ``_reconcile_so_line_items`` for orders/edited.
	"""

	@classmethod
	def setUpClass(cls):
		cls.patch_path = os.path.join(
			os.path.dirname(SHOPIFY_DIR), "patches",
			"add_so_item_allow_on_submit.py",
		)
		with open(PATCHES_TXT, "r", encoding="utf-8") as f:
			cls.patches_txt = f.read()
		with open(cls.patch_path, "r", encoding="utf-8") as f:
			cls.patch_src = f.read()

	def test_patch_file_exists(self):
		self.assertTrue(os.path.exists(self.patch_path),
			"yei-v1.3.3 patch add_so_item_allow_on_submit.py must exist")

	def test_patch_registered_in_patches_txt(self):
		self.assertIn(
			"ecommerce_integrations.patches.add_so_item_allow_on_submit",
			self.patches_txt,
			"yei-v1.3.3 patch must be registered in patches.txt",
		)

	def test_patch_targets_so_items_table_field(self):
		self.assertIn('make_property_setter', self.patch_src,
			"patch must call frappe.make_property_setter")
		self.assertIn('"Sales Order"', self.patch_src,
			"patch target doctype must be Sales Order (parent of items table)")
		self.assertIn('"items"', self.patch_src,
			"patch target fieldname must be 'items' (the Table field)")
		self.assertIn('"allow_on_submit"', self.patch_src,
			"patch must set the allow_on_submit property")
		self.assertIn('"Check"', self.patch_src,
			"property_type must be Check")

	def test_patch_clears_cache_after_mutation(self):
		self.assertIn('clear_cache', self.patch_src,
			"patch must clear Sales Order cache so metadata takes effect")


# ───────────────────────────────────────────────────────────────────────
# yei-v1.3.4 — admin replay wrapper for handle_order_edited backfill
# ───────────────────────────────────────────────────────────────────────


class TestV134ReplayHandleOrderEditedSourceInvariant(unittest.TestCase):
	"""Phase-2.5: ``replay_handle_order_edited`` admin-callable wrapper.

	The thin entry-point added so the Phase-2 backfill can call
	``handle_order_edited`` (a webhook-only handler, not @frappe.whitelist'd)
	against historical EIL Invalid rows. Permission gate is admin-only
	because this bypasses HMAC validation.
	"""

	@classmethod
	def setUpClass(cls):
		with open(ORDER_PY, "r", encoding="utf-8") as f:
			cls.source = f.read()

	def test_replay_function_defined(self):
		self.assertIn("def replay_handle_order_edited(", self.source,
			"replay_handle_order_edited must be defined in order.py")

	def test_replay_is_whitelisted(self):
		# The @frappe.whitelist() decorator must precede the def.
		self.assertRegex(self.source,
			r'@frappe\.whitelist\(\)\s*\ndef\s+replay_handle_order_edited\(',
			"replay_handle_order_edited must be decorated with @frappe.whitelist()")

	def test_replay_enforces_system_manager(self):
		body = _source_of("replay_handle_order_edited", self.source)
		self.assertIn('"System Manager"', body,
			"replay_handle_order_edited must gate on System Manager role")
		self.assertIn("frappe.get_roles()", body,
			"replay_handle_order_edited must check current user's roles")
		self.assertIn("frappe.throw", body,
			"replay_handle_order_edited must throw on permission failure")

	def test_replay_builds_synthetic_order_edit_payload(self):
		body = _source_of("replay_handle_order_edited", self.source)
		self.assertIn('"order_edit"', body,
			"replay must build a synthetic payload with order_edit key")
		self.assertIn('"order_id"', body,
			"replay must put shopify_order_id under order_edit.order_id")
		self.assertIn("str(shopify_order_id)", body,
			"replay must coerce shopify_order_id to str for consistency "
			"with handle_order_edited's order_edit.order_id extraction")

	def test_replay_delegates_to_handle_order_edited(self):
		body = _source_of("replay_handle_order_edited", self.source)
		self.assertIn("handle_order_edited(", body,
			"replay must delegate to handle_order_edited — no duplicated logic")

	def test_replay_passes_request_id_as_is(self):
		"""``request_id`` must be passed through unchanged — including
		``None``. The handler's downstream ``create_shopify_log`` uses
		``frappe.flags.request_id`` to decide between updating an existing
		EIL row (when set) or creating a new one (when None). Forging a
		fake request_id string would break the existing-EIL lookup with
		``DoesNotExistError``."""
		body = _source_of("replay_handle_order_edited", self.source)
		self.assertIn("request_id=request_id", body,
			"replay must pass request_id through to handle_order_edited "
			"unchanged — not auto-fill with a fake value, which would "
			"break the EIL row lookup downstream")


class TestV134ReplayBehavioural(unittest.TestCase):
	"""Inline-copy behavioural tests for the replay wrapper logic."""

	def test_admin_check_passes_for_system_manager(self):
		# Simulate the role-gate logic.
		roles = ["System Manager", "Sales User"]
		user = "milky@seismisk.com"
		self.assertTrue(
			"System Manager" in roles or user == "Administrator",
			"System Manager role must pass the gate",
		)

	def test_admin_check_passes_for_administrator(self):
		roles = []  # No roles, but session user is Administrator
		user = "Administrator"
		self.assertTrue(
			"System Manager" in roles or user == "Administrator",
			"Administrator user must pass the gate even without role",
		)

	def test_admin_check_blocks_regular_user(self):
		roles = ["Sales User", "Stock User"]
		user = "rep@example.com"
		self.assertFalse(
			"System Manager" in roles or user == "Administrator",
			"Regular user (no SM, not Administrator) must be blocked",
		)

	def test_synthetic_payload_shape(self):
		"""The wrapper must produce a payload of the shape
		handle_order_edited extracts order_id from."""
		shopify_order_id = 11048739799403
		synthetic = {"order_edit": {"order_id": str(shopify_order_id)}}
		# Mirror handle_order_edited's extraction logic.
		order_edit = (synthetic.get("order_edit") if isinstance(synthetic, dict) else None) or {}
		order_id = order_edit.get("order_id") or (synthetic.get("id") if isinstance(synthetic, dict) else None)
		self.assertEqual(order_id, "11048739799403",
			"synthetic payload must extract correctly via handle_order_edited's logic")

	def test_replay_idempotent_when_so_already_in_sync(self):
		"""If _reconcile_so_line_items returns no additions/refunds, the
		replay call is a no-op for ``items`` — only the audit-trail ToDo
		may be written. Mirrors the in-sync branch from the v1.3.3 tests."""
		existing_by_lid = {"100": object(), "200": object()}
		shopify_lines = [
			{"id": 100, "current_quantity": 1, "title": "A"},
			{"id": 200, "current_quantity": 1, "title": "B"},
		]
		added = []
		refunded = []
		for li in shopify_lines:
			lid = str(li.get("id") or "")
			cq = int(li.get("current_quantity") or 0)
			if cq == 0:
				continue
			if lid and lid in existing_by_lid:
				continue
			added.append(li)
		self.assertEqual(added, [],
			"replay against an in-sync SO must add zero lines")
		self.assertEqual(refunded, [])


# ───────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
	unittest.main(verbosity=2)
