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


if __name__ == "__main__":
	unittest.main()
