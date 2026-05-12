"""B23: Tests for the Shopify freight-class resolver.

Pure-logic tests — ``freight_class.py`` has no Frappe imports, so we
import it directly without the heavy mocking the older
test_connector_patches.py file uses.

Run with:
  python3 -m pytest ecommerce_integrations/shopify/tests/test_freight_class.py -v
"""
from __future__ import annotations

import os
import sys
import unittest

# Make the package importable from the repo root regardless of cwd.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if ROOT not in sys.path:
	sys.path.insert(0, ROOT)

from ecommerce_integrations.shopify.freight_class import (  # noqa: E402
	classify_product_tags,
	resolve_for_order,
	rollup_so,
)


class TestClassifyProductTags(unittest.TestCase):
	def test_ship_air_string(self):
		self.assertEqual(classify_product_tags("ship-air, stockv2"), "air")

	def test_ship_sea_string(self):
		self.assertEqual(classify_product_tags("Greenhouse, ship-sea, stockv1"), "sea")

	def test_ship_dropship_string(self):
		self.assertEqual(classify_product_tags("ship-dropship, Accesories"), "dropship")

	def test_ship_air_list(self):
		self.assertEqual(classify_product_tags(["ship-air", "stockv2"]), "air")

	def test_ship_sea_list(self):
		self.assertEqual(classify_product_tags(["Greenhouse", "ship-sea"]), "sea")

	def test_no_ship_tag(self):
		self.assertEqual(classify_product_tags("Accesories, stockv2"), "")

	def test_empty_string(self):
		self.assertEqual(classify_product_tags(""), "")

	def test_empty_list(self):
		self.assertEqual(classify_product_tags([]), "")

	def test_none(self):
		self.assertEqual(classify_product_tags(None), "")

	def test_case_insensitive(self):
		self.assertEqual(classify_product_tags("SHIP-AIR, Greenhouse"), "air")
		self.assertEqual(classify_product_tags(["Ship-Sea"]), "sea")

	def test_whitespace_tolerated(self):
		self.assertEqual(classify_product_tags(" ship-air , stockv2 "), "air")

	def test_first_match_wins(self):
		# If a product somehow has both ship-air and ship-sea, first listed wins.
		self.assertEqual(classify_product_tags("ship-air, ship-sea"), "air")
		self.assertEqual(classify_product_tags("ship-sea, ship-air"), "sea")

	def test_unknown_tags_ignored(self):
		self.assertEqual(classify_product_tags("warranty, candyrack"), "")


class TestRollupSo(unittest.TestCase):
	def test_uniform_air(self):
		self.assertEqual(rollup_so(["air", "air", "air"]), "air")

	def test_uniform_sea(self):
		self.assertEqual(rollup_so(["sea", "sea"]), "sea")

	def test_uniform_dropship(self):
		self.assertEqual(rollup_so(["dropship"]), "dropship")

	def test_air_with_blank_accessories(self):
		# Greenhouse + accessories with no ship-* tag → still air at SO level.
		self.assertEqual(rollup_so(["air", "", ""]), "air")

	def test_sea_with_blank_accessories(self):
		self.assertEqual(rollup_so(["sea", "", ""]), "sea")

	def test_split_mixed_air_sea(self):
		self.assertEqual(rollup_so(["air", "sea"]), "split")

	def test_split_mixed_with_blanks(self):
		self.assertEqual(rollup_so(["air", "sea", "", ""]), "split")

	def test_air_with_dropship_collapses_to_air(self):
		# Dropship lines skip DN materialization, so the operational class
		# is what's left. Practical case: an Amata accessory mixed in.
		self.assertEqual(rollup_so(["air", "dropship"]), "air")

	def test_sea_with_dropship_collapses_to_sea(self):
		self.assertEqual(rollup_so(["sea", "dropship"]), "sea")

	def test_split_with_dropship_still_split(self):
		# Air + sea + dropship — the air/sea split dominates.
		self.assertEqual(rollup_so(["air", "sea", "dropship"]), "split")

	def test_all_blank(self):
		self.assertEqual(rollup_so(["", "", ""]), "")

	def test_empty_list(self):
		self.assertEqual(rollup_so([]), "")

	def test_none(self):
		self.assertEqual(rollup_so(None), "")


class TestResolveForOrder(unittest.TestCase):
	def _fake_fetcher(self, mapping):
		"""Build a deterministic fetcher from a {product_id: tags} dict."""
		return lambda pid: mapping.get(pid, [])

	def test_air_only_order(self):
		order = {"line_items": [
			{"product_id": 100, "name": "WMP greenhouse"},
			{"product_id": 200, "name": "Accessory"},
		]}
		fetcher = self._fake_fetcher({
			100: ["ship-air", "stockv2"],
			200: ["Accesories"],
		})
		per_line, rollup = resolve_for_order(order, fetcher)
		self.assertEqual(per_line, ["air", ""])
		self.assertEqual(rollup, "air")

	def test_sea_only_order(self):
		order = {"line_items": [
			{"product_id": 1, "name": "Nordwood greenhouse"},
		]}
		fetcher = self._fake_fetcher({1: ["Greenhouse", "ship-sea", "stockv1"]})
		per_line, rollup = resolve_for_order(order, fetcher)
		self.assertEqual(per_line, ["sea"])
		self.assertEqual(rollup, "sea")

	def test_split_order_air_and_sea(self):
		order = {"line_items": [
			{"product_id": 100, "name": "Air greenhouse"},
			{"product_id": 200, "name": "Sea greenhouse"},
			{"product_id": 300, "name": "Untagged accessory"},
		]}
		fetcher = self._fake_fetcher({
			100: ["ship-air"],
			200: ["ship-sea"],
			300: [],
		})
		per_line, rollup = resolve_for_order(order, fetcher)
		self.assertEqual(per_line, ["air", "sea", ""])
		self.assertEqual(rollup, "split")

	def test_dropship_only_order(self):
		order = {"line_items": [
			{"product_id": 1, "name": "Amata chair"},
		]}
		fetcher = self._fake_fetcher({1: ["ship-dropship", "furniture"]})
		per_line, rollup = resolve_for_order(order, fetcher)
		self.assertEqual(per_line, ["dropship"])
		self.assertEqual(rollup, "dropship")

	def test_missing_product_id(self):
		# B12-style unmatched line items have no product_id — should
		# classify as "" without crashing.
		order = {"line_items": [
			{"product_id": 100, "name": "Real product"},
			{"name": "Phantom line, no product_id"},
		]}
		fetcher = self._fake_fetcher({100: ["ship-air"]})
		per_line, rollup = resolve_for_order(order, fetcher)
		self.assertEqual(per_line, ["air", ""])
		self.assertEqual(rollup, "air")

	def test_fetcher_exception_swallowed(self):
		# Fetcher raises (network blip, deleted product, etc.) — line
		# falls back to "" so SO sync doesn't abort.
		def angry_fetcher(pid):
			raise RuntimeError("Shopify API blew up")

		order = {"line_items": [
			{"product_id": 100, "name": "Item"},
		]}
		per_line, rollup = resolve_for_order(order, angry_fetcher)
		self.assertEqual(per_line, [""])
		self.assertEqual(rollup, "")

	def test_empty_line_items(self):
		per_line, rollup = resolve_for_order({"line_items": []}, lambda pid: [])
		self.assertEqual(per_line, [])
		self.assertEqual(rollup, "")

	def test_missing_line_items_key(self):
		per_line, rollup = resolve_for_order({}, lambda pid: [])
		self.assertEqual(per_line, [])
		self.assertEqual(rollup, "")


class TestLiveFetcherCaching(unittest.TestCase):
	"""make_live_fetcher should cache per call to avoid duplicate API hits."""

	def test_cache_hit_within_call(self):
		# Inject mocked shopify modules into sys.modules so the lazy
		# import inside make_live_fetcher resolves to our fakes.
		from unittest.mock import MagicMock

		fake_product_cls = MagicMock()
		mock_obj = MagicMock(tags="ship-air, stockv2")
		fake_product_cls.find.return_value = mock_obj

		fake_resources = MagicMock(Product=fake_product_cls)
		fake_shopify = MagicMock(resources=fake_resources)

		original = {
			"shopify": sys.modules.get("shopify"),
			"shopify.resources": sys.modules.get("shopify.resources"),
		}
		sys.modules["shopify"] = fake_shopify
		sys.modules["shopify.resources"] = fake_resources

		try:
			from ecommerce_integrations.shopify.freight_class import make_live_fetcher
			fetcher = make_live_fetcher()

			# First call hits API
			t1 = fetcher(100)
			self.assertEqual(t1, ["ship-air", "stockv2"])
			# Second call same pid → cache hit, no extra API call
			t2 = fetcher(100)
			self.assertEqual(t2, ["ship-air", "stockv2"])
			self.assertEqual(fake_product_cls.find.call_count, 1)

			# Different pid → API hit again
			fetcher(200)
			self.assertEqual(fake_product_cls.find.call_count, 2)
		finally:
			# Restore module state so other tests aren't affected.
			for k, v in original.items():
				if v is None:
					sys.modules.pop(k, None)
				else:
					sys.modules[k] = v


class TestWaveBSingleWriteRecomputeForSO(unittest.TestCase):
	"""Wave B (2026-05-12 reader cutover) — `recompute_for_so` writes ONLY
	the new field set; legacy fields are no longer written.

	Stubs frappe.db.{exists,get_doc,set_value,sql} + shopify.resources.Order so
	the whitelisted function body runs end-to-end against in-memory fakes.
	Asserts that ONLY `so_ship_class` (SO) / `item_ship_method` (SOI) /
	`dn_ship_method` (DN cascade) are written; the legacy
	`shopify_freight_class` field is NOT written on any of the three doctypes.
	"""

	def _build_stubs(self, so_items, lines, draft_dns=()):
		"""Install frappe + shopify stubs.

		Args:
			so_items: list of (name, item_code) — SO Items to iterate.
			lines: list of dicts with {product_id, title}.
			draft_dns: list of DN names that should appear in the cascade
				SELECT (already gated by docstatus=0 + against_sales_order).

		Returns:
			(set_value_calls, dn_set_value_calls) — list of (doctype, name,
			field, value) tuples captured per call.
		"""
		from unittest.mock import MagicMock

		set_value_calls = []

		def fake_set_value(doctype, name, field, value, update_modified=False):
			set_value_calls.append((doctype, name, field, value))

		fake_frappe = MagicMock()
		fake_frappe.db.exists.return_value = True
		fake_frappe.db.set_value.side_effect = fake_set_value

		# fake SO doc — only `.items` + `.get("shopify_order_id")` are touched.
		class _Item:
			def __init__(self, n, c):
				self.name = n
				self.item_code = c

		fake_so = MagicMock()
		fake_so.items = [_Item(n, c) for n, c in so_items]
		fake_so.get.return_value = "999"  # any non-empty shopify_order_id

		fake_frappe.get_doc.return_value = fake_so

		# fake DN cascade SQL result
		fake_frappe.db.sql.return_value = [{"name": n} for n in draft_dns]

		# Inject fake frappe so the local `import frappe` inside
		# recompute_for_so finds our stub.
		original = sys.modules.get("frappe")
		sys.modules["frappe"] = fake_frappe

		# Shopify.resources.Order stub — Order.find returns a payload whose
		# attributes.get("line_items") yields our lines.
		fake_order = MagicMock()
		def _li_from_dict(d):
			obj = MagicMock()
			obj.product_id = d.get("product_id")
			obj.title = d.get("title", "")
			obj.attributes = {"product_id": d.get("product_id")}
			return obj
		fake_order.attributes.get.return_value = [_li_from_dict(li) for li in lines]
		fake_order_cls = MagicMock(find=MagicMock(return_value=fake_order))

		fake_resources = MagicMock(Order=fake_order_cls)
		fake_shopify = MagicMock(resources=fake_resources)
		original_shopify = sys.modules.get("shopify")
		original_shopify_res = sys.modules.get("shopify.resources")
		sys.modules["shopify"] = fake_shopify
		sys.modules["shopify.resources"] = fake_resources

		def restore():
			if original is None:
				sys.modules.pop("frappe", None)
			else:
				sys.modules["frappe"] = original
			if original_shopify is None:
				sys.modules.pop("shopify", None)
			else:
				sys.modules["shopify"] = original_shopify
			if original_shopify_res is None:
				sys.modules.pop("shopify.resources", None)
			else:
				sys.modules["shopify.resources"] = original_shopify_res

		return set_value_calls, restore

	def test_so_header_writes_new_field_only(self):
		"""SO.so_ship_class is written. Legacy SO.shopify_freight_class is NOT."""
		set_calls, restore = self._build_stubs(
			so_items=[("SOI-1", "ITEM-A")],
			lines=[{"product_id": 100, "title": "Greenhouse"}],
			draft_dns=[],
		)
		try:
			from ecommerce_integrations.shopify import freight_class
			# Inject a deterministic fetcher so we don't hit make_live_fetcher.
			fetcher = lambda pid: ["ship-air"]
			result = freight_class.recompute_for_so("SO-1", fetcher=fetcher)
			self.assertEqual(result["rollup"], "air")

			so_writes = [c for c in set_calls if c[0] == "Sales Order"]
			fields_written = {c[2] for c in so_writes}
			# Wave B: only new field on SO header.
			self.assertIn("so_ship_class", fields_written)
			self.assertNotIn("shopify_freight_class", fields_written)
			# Value carries the bare rollup.
			new_value = next(c[3] for c in so_writes if c[2] == "so_ship_class")
			self.assertEqual(new_value, "air")
		finally:
			restore()

	def test_soi_writes_item_ship_method_with_prefix(self):
		"""SOI.item_ship_method written with `ship-` prefix. Legacy field NOT written."""
		set_calls, restore = self._build_stubs(
			so_items=[("SOI-1", "ITEM-A"), ("SOI-2", "ITEM-B")],
			lines=[
				{"product_id": 100, "title": "Air greenhouse"},
				{"product_id": 200, "title": "Sea greenhouse"},
			],
			draft_dns=[],
		)
		try:
			from ecommerce_integrations.shopify import freight_class
			tag_map = {100: ["ship-air"], 200: ["ship-sea"]}
			fetcher = lambda pid: tag_map.get(pid, [])
			freight_class.recompute_for_so("SO-1", fetcher=fetcher)

			soi_writes = [c for c in set_calls if c[0] == "Sales Order Item"]
			# Map of (soi_name, field) → value
			writes = {(c[1], c[2]): c[3] for c in soi_writes}
			fields_written = {c[2] for c in soi_writes}
			# Wave B: only new field, with `ship-` prefix.
			self.assertIn("item_ship_method", fields_written)
			self.assertNotIn("shopify_freight_class", fields_written)
			self.assertEqual(writes[("SOI-1", "item_ship_method")], "ship-air")
			self.assertEqual(writes[("SOI-2", "item_ship_method")], "ship-sea")
		finally:
			restore()

	def test_cascade_writes_dn_ship_method_only(self):
		"""Draft DN linked to SO gets dn_ship_method. Legacy shopify_freight_class NOT written."""
		set_calls, restore = self._build_stubs(
			so_items=[("SOI-1", "ITEM-A")],
			lines=[{"product_id": 100, "title": "Greenhouse"}],
			draft_dns=["DN-DRAFT-1"],
		)
		try:
			from ecommerce_integrations.shopify import freight_class
			fetcher = lambda pid: ["ship-air"]
			freight_class.recompute_for_so("SO-1", fetcher=fetcher)

			dn_writes = [c for c in set_calls if c[0] == "Delivery Note"]
			fields_written = {c[2] for c in dn_writes}
			# Wave B: only new field on DN cascade.
			self.assertIn("dn_ship_method", fields_written)
			self.assertNotIn("shopify_freight_class", fields_written)
			# Identity cascade — value should be 'air'.
			for dt, name, field, value in dn_writes:
				self.assertEqual(value, "air")
				self.assertEqual(name, "DN-DRAFT-1")
		finally:
			restore()

	def test_no_cascade_when_rollup_is_split_or_dropship(self):
		"""Split/dropship rollups don't cascade — DN-level value comes from split.py bucket."""
		set_calls, restore = self._build_stubs(
			so_items=[("SOI-1", "ITEM-A"), ("SOI-2", "ITEM-B")],
			lines=[
				{"product_id": 100, "title": "Air"},
				{"product_id": 200, "title": "Sea"},
			],
			draft_dns=["DN-DRAFT-1"],
		)
		try:
			from ecommerce_integrations.shopify import freight_class
			tag_map = {100: ["ship-air"], 200: ["ship-sea"]}
			fetcher = lambda pid: tag_map.get(pid, [])
			result = freight_class.recompute_for_so("SO-1", fetcher=fetcher)
			self.assertEqual(result["rollup"], "split")

			# DN cascade should NOT have fired for split rollup.
			dn_writes = [c for c in set_calls if c[0] == "Delivery Note"]
			self.assertEqual(dn_writes, [])
		finally:
			restore()


if __name__ == "__main__":
	unittest.main()
