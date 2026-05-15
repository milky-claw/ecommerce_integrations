"""yei-v1.4.2: unit tests for backfill_mirror admin-only method.

Run with: python3 ecommerce_integrations/shopify/tests/test_backfill_mirror.py

Bench-independent: mocks Frappe before importing, same pattern as
test_order_updated.py + test_v1310_release.py. Two categories:

  * Source-invariant tests (AST/text inspection) — proof that:
      - backfill_mirror exists in order.py
      - decorated with @frappe.whitelist()
      - admin-role gate present (System Manager OR Administrator)
      - uses frappe.db.set_value with update_modified=False
      - calls frappe.db.commit()
      - normalizes empty values

  * Behavioral tests via mocked frappe:
      - non-admin caller → frappe.throw invoked
      - admin caller → set_value invoked with expected args
      - missing SO → frappe.throw invoked
      - None / empty value → normalized to ""
      - idempotent: same-value re-call still completes
"""

import ast
import os
import sys
import unittest
from unittest.mock import MagicMock, patch


# ── Mock frappe BEFORE any connector import ────────────────────────────
def _build_frappe_mock():
	"""Fresh frappe mock per test method when needed."""
	m = MagicMock()
	m._ = lambda x: x
	m.utils.cint = lambda x: int(x or 0)
	m.utils.cstr = lambda x: str(x) if x else ""
	m.utils.flt = lambda x: float(x or 0)
	m.flags = MagicMock()
	m.whitelist = lambda *a, **kw: (lambda fn: fn)
	return m


frappe_mock = _build_frappe_mock()
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


def _source_of(fn_name: str, source: str) -> str:
	"""Return the raw source of a top-level function from parsed Python."""
	tree = ast.parse(source)
	for node in tree.body:
		if isinstance(node, ast.FunctionDef) and node.name == fn_name:
			return ast.get_source_segment(source, node) or ""
	raise AssertionError(f"function {fn_name!r} not found in source")


def _decorators_of(fn_name: str, source: str):
	tree = ast.parse(source)
	for node in tree.body:
		if isinstance(node, ast.FunctionDef) and node.name == fn_name:
			return [ast.unparse(d) for d in node.decorator_list]
	raise AssertionError(f"function {fn_name!r} not found in source")


# ───────────────────────────────────────────────────────────────────────
# Source-invariant tests
# ───────────────────────────────────────────────────────────────────────


class TestBackfillMirrorSourceInvariant(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		with open(ORDER_PY, "r", encoding="utf-8") as f:
			cls.source = f.read()

	def test_function_exists(self):
		body = _source_of("backfill_mirror", self.source)
		self.assertTrue(len(body) > 100, "backfill_mirror body must be non-trivial")

	def test_is_whitelisted(self):
		decs = _decorators_of("backfill_mirror", self.source)
		self.assertTrue(
			any("frappe.whitelist" in d for d in decs),
			"v1.4.2: backfill_mirror must be @frappe.whitelist()'d",
		)

	def test_admin_role_gate(self):
		body = _source_of("backfill_mirror", self.source)
		self.assertIn("System Manager", body, "v1.4.2: admin gate must check System Manager role")
		self.assertIn("Administrator", body, "v1.4.2: admin gate must check Administrator session user")
		self.assertIn("frappe.get_roles()", body, "v1.4.2: must call frappe.get_roles()")
		self.assertIn("frappe.session.user", body, "v1.4.2: must check frappe.session.user")

	def test_uses_set_value_with_update_modified_false(self):
		body = _source_of("backfill_mirror", self.source)
		self.assertIn("frappe.db.set_value", body, "v1.4.2: must use frappe.db.set_value")
		self.assertIn("update_modified=False", body, "v1.4.2: must pass update_modified=False")

	def test_writes_shopify_fulfillment_status(self):
		body = _source_of("backfill_mirror", self.source)
		self.assertIn(
			'"shopify_fulfillment_status"',
			body,
			"v1.4.2: must write to shopify_fulfillment_status field",
		)

	def test_commits(self):
		body = _source_of("backfill_mirror", self.source)
		self.assertIn("frappe.db.commit", body, "v1.4.2: must call frappe.db.commit()")

	def test_validates_so_exists(self):
		body = _source_of("backfill_mirror", self.source)
		self.assertIn("frappe.db.exists", body, "v1.4.2: must validate SO exists")

	def test_normalizes_empty(self):
		body = _source_of("backfill_mirror", self.source)
		# Either `value or ""` or explicit None check — accept either shape
		self.assertTrue(
			'value or ""' in body or 'if value is None' in body or "if not value" in body,
			"v1.4.2: must normalize None/empty to empty string",
		)


# ───────────────────────────────────────────────────────────────────────
# Behavioral tests via mocked frappe
# ───────────────────────────────────────────────────────────────────────


def _load_backfill_mirror_into_namespace(frappe_obj):
	"""Extract backfill_mirror source from order.py and exec it in an
	isolated namespace bound to ``frappe_obj``. Avoids importing order.py
	(which would drag in sibling modules we'd have to mock individually).
	"""
	with open(ORDER_PY, "r", encoding="utf-8") as f:
		full = f.read()
	src = _source_of("backfill_mirror", full)
	ns = {
		"frappe": frappe_obj,
		"_": lambda x: x,  # the function uses _("...") for i18n
	}
	exec(compile(src, ORDER_PY, "exec"), ns)
	return ns["backfill_mirror"]


class _ThrowError(Exception):
	"""Sentinel raised by mocked frappe.throw to simulate Frappe's behaviour."""


class TestBackfillMirrorBehavior(unittest.TestCase):
	def setUp(self):
		# Reset frappe mock per test
		frappe_mock.reset_mock(return_value=True, side_effect=True)
		frappe_mock._ = lambda x: x
		frappe_mock.throw = MagicMock(side_effect=_ThrowError)
		frappe_mock.get_roles = MagicMock(return_value=[])
		frappe_mock.session = MagicMock()
		frappe_mock.session.user = "guest@example.com"
		frappe_mock.db = MagicMock()
		frappe_mock.db.exists = MagicMock(return_value=True)
		frappe_mock.db.set_value = MagicMock()
		frappe_mock.db.commit = MagicMock()
		frappe_mock.whitelist = lambda *a, **kw: (lambda fn: fn)

	def test_backfill_mirror_admin_only(self):
		"""Non-admin caller must trip frappe.throw."""
		backfill_mirror = _load_backfill_mirror_into_namespace(frappe_mock)
		with self.assertRaises(_ThrowError):
			backfill_mirror("SAL-ORD-2026-00894", "fulfilled")
		frappe_mock.throw.assert_called_once()
		# set_value must NOT have been called
		frappe_mock.db.set_value.assert_not_called()

	def test_backfill_mirror_writes_value(self):
		"""Admin caller → set_value called with expected args."""
		frappe_mock.get_roles = MagicMock(return_value=["System Manager"])
		backfill_mirror = _load_backfill_mirror_into_namespace(frappe_mock)
		result = backfill_mirror("SAL-ORD-2026-00894", "fulfilled")
		frappe_mock.db.set_value.assert_called_once_with(
			"Sales Order",
			"SAL-ORD-2026-00894",
			"shopify_fulfillment_status",
			"fulfilled",
			update_modified=False,
		)
		frappe_mock.db.commit.assert_called_once()
		self.assertEqual(result, {"sales_order": "SAL-ORD-2026-00894", "value": "fulfilled"})

	def test_backfill_mirror_administrator_session(self):
		"""Administrator session_user even without role list must pass gate."""
		frappe_mock.get_roles = MagicMock(return_value=[])
		frappe_mock.session.user = "Administrator"
		backfill_mirror = _load_backfill_mirror_into_namespace(frappe_mock)
		backfill_mirror("SH-2026-04724", "partial")
		frappe_mock.db.set_value.assert_called_once()

	def test_backfill_mirror_idempotent(self):
		"""Same-value re-call is still a no-op-equivalent (no error)."""
		frappe_mock.get_roles = MagicMock(return_value=["System Manager"])
		backfill_mirror = _load_backfill_mirror_into_namespace(frappe_mock)
		r1 = backfill_mirror("SH-2026-04927", "fulfilled")
		r2 = backfill_mirror("SH-2026-04927", "fulfilled")
		self.assertEqual(r1, r2)
		self.assertEqual(frappe_mock.db.set_value.call_count, 2)

	def test_backfill_mirror_normalizes_empty(self):
		"""None → empty string."""
		frappe_mock.get_roles = MagicMock(return_value=["System Manager"])
		backfill_mirror = _load_backfill_mirror_into_namespace(frappe_mock)
		result = backfill_mirror("SH-2026-04900", None)
		# Inspect the args set_value received
		_args, kwargs = frappe_mock.db.set_value.call_args
		args_passed = frappe_mock.db.set_value.call_args[0]
		self.assertEqual(args_passed[3], "", "None value must be normalized to empty string")
		self.assertEqual(result["value"], "")

	def test_backfill_mirror_rejects_missing_so(self):
		"""Non-existent SO → frappe.throw."""
		frappe_mock.get_roles = MagicMock(return_value=["System Manager"])
		frappe_mock.db.exists = MagicMock(return_value=False)
		backfill_mirror = _load_backfill_mirror_into_namespace(frappe_mock)
		with self.assertRaises(_ThrowError):
			backfill_mirror("DOES-NOT-EXIST", "fulfilled")
		frappe_mock.db.set_value.assert_not_called()


if __name__ == "__main__":
	unittest.main(verbosity=2)
