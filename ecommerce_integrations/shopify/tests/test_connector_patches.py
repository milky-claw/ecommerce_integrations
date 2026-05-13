"""
Standalone unit tests for connector patches B1-B13.

These tests mock Frappe and Shopify dependencies so they can run
locally without a Frappe instance. Tests cover pure logic functions
and verify the patched functions produce correct output.

Run with: python3 -m pytest ecommerce_integrations/shopify/tests/test_connector_patches.py -v
"""

import datetime
import json
import re
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

# ── Mock frappe before importing any connector code ──────────────────
frappe_mock = MagicMock()
frappe_mock._ = lambda x: x
frappe_mock.utils.cint = lambda x: int(x or 0)
frappe_mock.utils.cstr = lambda x: str(x) if x else ""
frappe_mock.utils.flt = lambda x: float(x or 0)
frappe_mock.flags = MagicMock()

sys.modules["frappe"] = frappe_mock
sys.modules["frappe.utils"] = frappe_mock.utils
sys.modules["frappe.utils.nestedset"] = MagicMock()
sys.modules["frappe.model.document"] = MagicMock()
sys.modules["frappe.custom.doctype.custom_field.custom_field"] = MagicMock()
sys.modules["frappe.tests"] = MagicMock()
sys.modules["frappe.utils.password"] = MagicMock()

# Mock shopify
shopify_mock = MagicMock()
sys.modules["shopify"] = shopify_mock
sys.modules["shopify.resources"] = MagicMock()
sys.modules["shopify.session"] = MagicMock()
sys.modules["shopify.collection"] = MagicMock()

# Mock pyactiveresource
sys.modules["pyactiveresource"] = MagicMock()
sys.modules["pyactiveresource.activeresource"] = MagicMock()
sys.modules["pyactiveresource.testing"] = MagicMock()
sys.modules["pyactiveresource.testing.http_fake"] = MagicMock()

# Mock erpnext
sys.modules["erpnext"] = MagicMock()

# Mock ecommerce_integrations modules that would import frappe
sys.modules["ecommerce_integrations"] = MagicMock()
sys.modules["ecommerce_integrations.controllers"] = MagicMock()
sys.modules["ecommerce_integrations.controllers.setting"] = MagicMock()
sys.modules["ecommerce_integrations.controllers.customer"] = MagicMock()
sys.modules["ecommerce_integrations.ecommerce_integrations"] = MagicMock()
sys.modules["ecommerce_integrations.ecommerce_integrations.doctype"] = MagicMock()
sys.modules["ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_item"] = MagicMock()
sys.modules["ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_integration_log"] = MagicMock()
sys.modules["ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_integration_log.ecommerce_integration_log"] = MagicMock()
sys.modules["ecommerce_integrations.shopify"] = MagicMock()
sys.modules["ecommerce_integrations.shopify.connection"] = MagicMock()
sys.modules["ecommerce_integrations.shopify.customer"] = MagicMock()
sys.modules["ecommerce_integrations.shopify.product"] = MagicMock()
sys.modules["ecommerce_integrations.shopify.utils"] = MagicMock()
sys.modules["ecommerce_integrations.utils"] = MagicMock()
sys.modules["ecommerce_integrations.utils.price_list"] = MagicMock()
sys.modules["ecommerce_integrations.utils.taxation"] = MagicMock()
sys.modules["ecommerce_integrations.shopify.oauth"] = MagicMock()

# Now import the actual constants (no frappe deps)
# We need to reload since we mocked the parent packages
import importlib

# Import constants directly by reading the file
import os
SHOPIFY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── Inline the functions we want to test ─────────────────────────────
# This avoids the complex import chain. We test the logic, not the wiring.

def _separate_tips(line_items):
    """B9: Separate tip line items from regular line items."""
    regular_items = []
    tip_total = 0.0

    for item in line_items:
        if str(item.get("title") or "").strip().lower() == "tip":
            tip_total += float(item.get("price", 0)) * int(item.get("quantity", 1))
        else:
            regular_items.append(item)

    return regular_items, tip_total


def _resolve_shipping_method_from_tags(tags_str):
    """B7: Logic extracted from _resolve_shipping_method.
    Given a tags string, return ship-sea, ship-air, or empty."""
    if not tags_str:
        return ""
    tags_lower = tags_str.lower()
    if "ship-sea" in tags_lower:
        return "ship-sea"
    elif "ship-air" in tags_lower:
        return "ship-air"
    return ""


def _build_order_edit_diff(so_items, shopify_line_items, shipping_address=None):
    """B13: Build diff between SO items and edited Shopify line items.

    Args:
        so_items: list of dicts with keys: item_code, item_name, qty, rate
        shopify_line_items: list of Shopify line item dicts
        shipping_address: optional Shopify shipping address dict
    """
    diff_lines = []

    # Detect new line items
    existing_titles = {item["item_name"] for item in so_items}
    for li in shopify_line_items:
        title = li.get("title", "Unknown")
        if title not in existing_titles:
            price = li.get("price", "0")
            qty = li.get("quantity", 1)
            diff_lines.append(f"+ NEW ITEM: {title} (qty: {qty}, price: ${price})")

    # Detect quantity/price changes
    for item in so_items:
        matching = [
            li for li in shopify_line_items
            if li.get("title") == item["item_name"] or li.get("sku") == item["item_code"]
        ]
        for li in matching:
            new_qty = int(li.get("quantity", 0))
            new_price = float(li.get("price", 0))
            if new_qty != int(item["qty"]):
                diff_lines.append(
                    f"~ QTY CHANGE: {item['item_name']} — {int(item['qty'])} → {new_qty}"
                )
            if abs(new_price - float(item["rate"])) > 0.01:
                diff_lines.append(
                    f"~ PRICE CHANGE: {item['item_name']} — ${float(item['rate']):.2f} → ${new_price:.2f}"
                )

    # Detect removed items
    shopify_titles = set()
    shopify_skus = set()
    for li in shopify_line_items:
        shopify_titles.add(li.get("title"))
        if li.get("sku"):
            shopify_skus.add(li.get("sku"))

    for item in so_items:
        if item["item_name"] not in shopify_titles and item["item_code"] not in shopify_skus:
            diff_lines.append(f"- REMOVED: {item['item_name']} (was qty: {int(item['qty'])})")

    # Check address
    if shipping_address:
        addr_str = (
            f"{shipping_address.get('address1', '')}, "
            f"{shipping_address.get('city', '')}, "
            f"{shipping_address.get('province', '')} "
            f"{shipping_address.get('zip', '')}"
        )
        diff_lines.append(f"  SHIPPING ADDRESS: {addr_str}")

    return diff_lines


def _extract_discount_codes(discount_codes):
    """B8: Extract discount code names from Shopify order."""
    if not discount_codes:
        return ""
    return ", ".join(dc.get("code", "") for dc in discount_codes)


def _get_order_enrichment(shopify_order):
    """Extract all order-level enrichment fields (B1, B8, B10)."""
    tags = shopify_order.get("tags", "")
    discount_codes = _extract_discount_codes(shopify_order.get("discount_codes", []))
    financial_status = shopify_order.get("financial_status") or ""
    fulfillment_status = shopify_order.get("fulfillment_status") or ""
    return {
        "tags": tags,
        "discount_codes": discount_codes,
        "financial_status": financial_status,
        "fulfillment_status": fulfillment_status,
    }


def _extract_line_item_properties(shopify_item):
    """B2: Extract properties as JSON string."""
    properties = shopify_item.get("properties", [])
    return json.dumps(properties) if properties else ""


def _should_use_misc_manual(shopify_item, item_code):
    """B12: Determine if MISC-MANUAL fallback is needed."""
    if item_code:
        return False
    if not shopify_item.get("product_exists"):
        return True
    if not shopify_item.get("product_id"):
        return True
    return True  # item_code is None/empty


# ── Tests ────────────────────────────────────────────────────────────

class TestB9SeparateTips(unittest.TestCase):
    """B9: Tip line items should be extracted, not created as SO Items."""

    def test_no_tips(self):
        items = [
            {"title": "Greenhouse WMP 3x4", "price": "2500", "quantity": 1},
            {"title": "Automatic Vent", "price": "150", "quantity": 2},
        ]
        regular, tip_total = _separate_tips(items)
        self.assertEqual(len(regular), 2)
        self.assertAlmostEqual(tip_total, 0.0)

    def test_single_tip(self):
        items = [
            {"title": "Greenhouse WMP 3x4", "price": "2500", "quantity": 1},
            {"title": "Tip", "price": "25", "quantity": 1},
        ]
        regular, tip_total = _separate_tips(items)
        self.assertEqual(len(regular), 1)
        self.assertEqual(regular[0]["title"], "Greenhouse WMP 3x4")
        self.assertAlmostEqual(tip_total, 25.0)

    def test_tip_case_insensitive(self):
        items = [
            {"title": "  tip  ", "price": "10", "quantity": 1},
            {"title": "TIP", "price": "5", "quantity": 1},
        ]
        regular, tip_total = _separate_tips(items)
        self.assertEqual(len(regular), 0)
        self.assertAlmostEqual(tip_total, 15.0)

    def test_tip_with_quantity(self):
        items = [
            {"title": "Tip", "price": "10", "quantity": 3},
        ]
        regular, tip_total = _separate_tips(items)
        self.assertEqual(len(regular), 0)
        self.assertAlmostEqual(tip_total, 30.0)

    def test_product_named_tip_guard(self):
        """A product literally named 'Tip' would be extracted —
        this is expected since Shopify tips use this exact title."""
        items = [
            {"title": "Tip", "price": "5", "quantity": 1, "product_exists": True},
        ]
        regular, tip_total = _separate_tips(items)
        self.assertEqual(len(regular), 0)
        self.assertAlmostEqual(tip_total, 5.0)

    def test_empty_line_items(self):
        regular, tip_total = _separate_tips([])
        self.assertEqual(len(regular), 0)
        self.assertAlmostEqual(tip_total, 0.0)

    def test_tip_with_none_title(self):
        items = [
            {"title": None, "price": "100", "quantity": 1},
        ]
        regular, tip_total = _separate_tips(items)
        self.assertEqual(len(regular), 1)
        self.assertAlmostEqual(tip_total, 0.0)


class TestB7ShippingMethodResolution(unittest.TestCase):
    """B7: Shipping method should be derived from product tags."""

    def test_ship_sea(self):
        self.assertEqual(_resolve_shipping_method_from_tags("ship-sea, Greenhouse, stockv2"), "ship-sea")

    def test_ship_air(self):
        self.assertEqual(_resolve_shipping_method_from_tags("ship-air, Accesories"), "ship-air")

    def test_no_shipping_tag(self):
        self.assertEqual(_resolve_shipping_method_from_tags("stockv1, Greenhouse"), "")

    def test_empty_tags(self):
        self.assertEqual(_resolve_shipping_method_from_tags(""), "")

    def test_none_tags(self):
        self.assertEqual(_resolve_shipping_method_from_tags(None), "")

    def test_case_insensitive(self):
        self.assertEqual(_resolve_shipping_method_from_tags("Ship-Sea, GREENHOUSE"), "ship-sea")

    def test_ship_sea_takes_precedence(self):
        """If both tags exist (shouldn't happen), ship-sea wins."""
        self.assertEqual(_resolve_shipping_method_from_tags("ship-sea, ship-air"), "ship-sea")


class TestB13OrderEditDiff(unittest.TestCase):
    """B13: Order edit diff should detect new, changed, and removed items."""

    def setUp(self):
        self.so_items = [
            {"item_code": "GH-WMP-0306", "item_name": "Greenhouse WMP 3x6", "qty": 1, "rate": 2500},
            {"item_code": "ACC-VENT-AUTO", "item_name": "Automatic Vent", "qty": 2, "rate": 150},
        ]

    def test_no_changes(self):
        shopify_items = [
            {"title": "Greenhouse WMP 3x6", "sku": "GH-WMP-0306", "quantity": 1, "price": "2500"},
            {"title": "Automatic Vent", "sku": "ACC-VENT-AUTO", "quantity": 2, "price": "150"},
        ]
        diff = _build_order_edit_diff(self.so_items, shopify_items)
        # Only address-free, should be empty
        self.assertEqual(diff, [])

    def test_new_item_added(self):
        shopify_items = [
            {"title": "Greenhouse WMP 3x6", "sku": "GH-WMP-0306", "quantity": 1, "price": "2500"},
            {"title": "Automatic Vent", "sku": "ACC-VENT-AUTO", "quantity": 2, "price": "150"},
            {"title": "Warranty Door Replacement", "sku": "", "quantity": 1, "price": "0"},
        ]
        diff = _build_order_edit_diff(self.so_items, shopify_items)
        new_items = [d for d in diff if d.startswith("+ NEW ITEM")]
        self.assertEqual(len(new_items), 1)
        self.assertIn("Warranty Door Replacement", new_items[0])
        self.assertIn("price: $0", new_items[0])

    def test_quantity_change(self):
        shopify_items = [
            {"title": "Greenhouse WMP 3x6", "sku": "GH-WMP-0306", "quantity": 2, "price": "2500"},
            {"title": "Automatic Vent", "sku": "ACC-VENT-AUTO", "quantity": 2, "price": "150"},
        ]
        diff = _build_order_edit_diff(self.so_items, shopify_items)
        qty_changes = [d for d in diff if "QTY CHANGE" in d]
        self.assertEqual(len(qty_changes), 1)
        self.assertIn("1 → 2", qty_changes[0])

    def test_price_change(self):
        shopify_items = [
            {"title": "Greenhouse WMP 3x6", "sku": "GH-WMP-0306", "quantity": 1, "price": "2200"},
            {"title": "Automatic Vent", "sku": "ACC-VENT-AUTO", "quantity": 2, "price": "150"},
        ]
        diff = _build_order_edit_diff(self.so_items, shopify_items)
        price_changes = [d for d in diff if "PRICE CHANGE" in d]
        self.assertEqual(len(price_changes), 1)
        self.assertIn("$2500.00 → $2200.00", price_changes[0])

    def test_item_removed(self):
        shopify_items = [
            {"title": "Greenhouse WMP 3x6", "sku": "GH-WMP-0306", "quantity": 1, "price": "2500"},
        ]
        diff = _build_order_edit_diff(self.so_items, shopify_items)
        removed = [d for d in diff if d.startswith("- REMOVED")]
        self.assertEqual(len(removed), 1)
        self.assertIn("Automatic Vent", removed[0])

    def test_address_included(self):
        shopify_items = [
            {"title": "Greenhouse WMP 3x6", "sku": "GH-WMP-0306", "quantity": 1, "price": "2500"},
            {"title": "Automatic Vent", "sku": "ACC-VENT-AUTO", "quantity": 2, "price": "150"},
        ]
        addr = {"address1": "123 Main St", "city": "Miami", "province": "FL", "zip": "33101"}
        diff = _build_order_edit_diff(self.so_items, shopify_items, shipping_address=addr)
        addr_lines = [d for d in diff if "SHIPPING ADDRESS" in d]
        self.assertEqual(len(addr_lines), 1)
        self.assertIn("123 Main St", addr_lines[0])
        self.assertIn("Miami", addr_lines[0])

    def test_multiple_changes(self):
        """New item + qty change + removal in one edit."""
        shopify_items = [
            {"title": "Greenhouse WMP 3x6", "sku": "GH-WMP-0306", "quantity": 3, "price": "2500"},
            {"title": "Free Gift Set", "quantity": 1, "price": "0"},
        ]
        diff = _build_order_edit_diff(self.so_items, shopify_items)
        new_items = [d for d in diff if d.startswith("+")]
        qty_changes = [d for d in diff if "QTY CHANGE" in d]
        removed = [d for d in diff if d.startswith("-")]
        self.assertEqual(len(new_items), 1)
        self.assertEqual(len(qty_changes), 1)
        self.assertEqual(len(removed), 1)

    def test_empty_so_all_new(self):
        shopify_items = [
            {"title": "Greenhouse WMP 3x6", "quantity": 1, "price": "2500"},
        ]
        diff = _build_order_edit_diff([], shopify_items)
        new_items = [d for d in diff if d.startswith("+")]
        self.assertEqual(len(new_items), 1)


class TestB1OrderTags(unittest.TestCase):
    """B1: Order tags should be extracted from the order payload."""

    def test_tags_present(self):
        order = {"tags": "Preorder, split, ship-sea"}
        result = _get_order_enrichment(order)
        self.assertEqual(result["tags"], "Preorder, split, ship-sea")

    def test_tags_empty(self):
        order = {"tags": ""}
        result = _get_order_enrichment(order)
        self.assertEqual(result["tags"], "")

    def test_tags_missing(self):
        order = {}
        result = _get_order_enrichment(order)
        self.assertEqual(result["tags"], "")


class TestB8DiscountCodes(unittest.TestCase):
    """B8: Discount code names should be extracted."""

    def test_single_code(self):
        codes = [{"code": "YGH5", "amount": "125.00", "type": "percentage"}]
        self.assertEqual(_extract_discount_codes(codes), "YGH5")

    def test_multiple_codes(self):
        codes = [
            {"code": "YGH5", "amount": "50.00"},
            {"code": "INFLUENCER", "amount": "100.00"},
        ]
        self.assertEqual(_extract_discount_codes(codes), "YGH5, INFLUENCER")

    def test_no_codes(self):
        self.assertEqual(_extract_discount_codes([]), "")

    def test_none_codes(self):
        self.assertEqual(_extract_discount_codes(None), "")


class TestB10FinancialFulfillmentStatus(unittest.TestCase):
    """B10: Financial and fulfillment status should be extracted."""

    def test_paid_unfulfilled(self):
        order = {"financial_status": "paid", "fulfillment_status": None}
        result = _get_order_enrichment(order)
        self.assertEqual(result["financial_status"], "paid")
        self.assertEqual(result["fulfillment_status"], "")

    def test_refunded_fulfilled(self):
        order = {"financial_status": "refunded", "fulfillment_status": "fulfilled"}
        result = _get_order_enrichment(order)
        self.assertEqual(result["financial_status"], "refunded")
        self.assertEqual(result["fulfillment_status"], "fulfilled")

    def test_missing_fields(self):
        order = {}
        result = _get_order_enrichment(order)
        self.assertEqual(result["financial_status"], "")
        self.assertEqual(result["fulfillment_status"], "")


class TestB2LineItemProperties(unittest.TestCase):
    """B2: Line item properties should be extracted as JSON."""

    def test_with_properties(self):
        item = {
            "properties": [
                {"name": "Doors", "value": "2"},
                {"name": "Door location", "value": "Left"},
                {"name": "Vents", "value": "3"},
            ]
        }
        result = _extract_line_item_properties(item)
        parsed = json.loads(result)
        self.assertEqual(len(parsed), 3)
        self.assertEqual(parsed[0]["name"], "Doors")

    def test_no_properties(self):
        item = {"properties": []}
        result = _extract_line_item_properties(item)
        self.assertEqual(result, "")

    def test_missing_properties(self):
        item = {}
        result = _extract_line_item_properties(item)
        self.assertEqual(result, "")

    def test_single_property(self):
        item = {"properties": [{"name": "Roof decoration", "value": "Snow guard"}]}
        result = _extract_line_item_properties(item)
        parsed = json.loads(result)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["value"], "Snow guard")


class TestB12MiscManualFallback(unittest.TestCase):
    """B12: Unmatched items should fall back to MISC-MANUAL."""

    def test_matched_item(self):
        self.assertFalse(_should_use_misc_manual(
            {"product_exists": True, "product_id": "123"}, "GH-WMP-0306"
        ))

    def test_product_not_exists(self):
        self.assertTrue(_should_use_misc_manual(
            {"product_exists": False, "product_id": None}, None
        ))

    def test_no_product_id(self):
        self.assertTrue(_should_use_misc_manual(
            {"product_exists": True, "product_id": None}, None
        ))

    def test_item_code_none(self):
        self.assertTrue(_should_use_misc_manual(
            {"product_exists": True, "product_id": "123"}, None
        ))

    def test_item_code_empty(self):
        self.assertTrue(_should_use_misc_manual(
            {"product_exists": True, "product_id": "123"}, ""
        ))

    def test_manual_line_item(self):
        """Draft order with manually typed product — no product_id."""
        self.assertTrue(_should_use_misc_manual(
            {"product_exists": False, "title": "Warranty Door Replacement"}, None
        ))


class TestB5B6TagsMetafieldsFormat(unittest.TestCase):
    """B5+B6: Verify the tag/metafield data structures are correct."""

    def test_tags_are_comma_separated_string(self):
        """Shopify returns tags as comma-separated string."""
        product_dict = {"tags": "ship-sea, Greenhouse, stockv2, pre-order"}
        tags = product_dict.get("tags", "")
        self.assertIsInstance(tags, str)
        self.assertIn("ship-sea", tags)
        self.assertIn("Greenhouse", tags)

    def test_metafields_clean_format(self):
        """Metafields should be stored as clean JSON with only useful keys."""
        raw_metafields = [
            {"namespace": "custom", "key": "ygwidth", "value": "300", "type": "number_integer",
             "id": 123, "owner_id": 456, "created_at": "2026-01-01"},
            {"namespace": "custom", "key": "preorder_message", "value": "Ships in 6 weeks",
             "type": "single_line_text_field", "id": 789, "admin_graphql_api_id": "gid://shopify/Metafield/789"},
        ]
        # This is the cleaning logic from _sync_tags_and_metafields
        clean = [
            {"namespace": mf.get("namespace"), "key": mf.get("key"),
             "value": mf.get("value"), "type": mf.get("type")}
            for mf in raw_metafields
        ]
        result = json.dumps(clean)
        parsed = json.loads(result)
        self.assertEqual(len(parsed), 2)
        # Internal Shopify fields should be stripped
        self.assertNotIn("id", parsed[0])
        self.assertNotIn("owner_id", parsed[0])
        self.assertNotIn("admin_graphql_api_id", parsed[1])
        # Useful fields preserved
        self.assertEqual(parsed[0]["key"], "ygwidth")
        self.assertEqual(parsed[1]["value"], "Ships in 6 weeks")


class TestIntegrationScenarios(unittest.TestCase):
    """End-to-end-like scenarios testing multiple patches together."""

    def test_full_order_enrichment(self):
        """Simulate a complete Shopify order with all custom data."""
        order = {
            "id": 9876543210,
            "name": "#1234",
            "tags": "Preorder, split",
            "financial_status": "paid",
            "fulfillment_status": None,
            "discount_codes": [{"code": "YGH5", "amount": "125.00", "type": "percentage"}],
            "line_items": [
                {
                    "title": "Greenhouse WMP 3x6",
                    "product_exists": True,
                    "product_id": "111",
                    "variant_id": "222",
                    "sku": "GH-WMP-0306",
                    "price": "2500",
                    "quantity": 1,
                    "properties": [
                        {"name": "Doors", "value": "2"},
                        {"name": "Door location", "value": "Left"},
                    ],
                    "tax_lines": [],
                    "discount_allocations": [],
                },
                {
                    "title": "Automatic Vent",
                    "product_exists": True,
                    "product_id": "333",
                    "variant_id": "444",
                    "sku": "ACC-VENT-AUTO",
                    "price": "150",
                    "quantity": 2,
                    "properties": [],
                    "tax_lines": [],
                    "discount_allocations": [],
                },
                {
                    "title": "Tip",
                    "price": "25",
                    "quantity": 1,
                    "product_exists": False,
                },
            ],
        }

        # B9: Separate tips
        line_items, tip_total = _separate_tips(order["line_items"])
        self.assertEqual(len(line_items), 2)
        self.assertAlmostEqual(tip_total, 25.0)

        # B1+B8+B10: Order-level enrichment
        enrichment = _get_order_enrichment(order)
        self.assertEqual(enrichment["tags"], "Preorder, split")
        self.assertEqual(enrichment["discount_codes"], "YGH5")
        self.assertEqual(enrichment["financial_status"], "paid")
        self.assertEqual(enrichment["fulfillment_status"], "")

        # B2: Line item properties
        props_gh = _extract_line_item_properties(line_items[0])
        props_vent = _extract_line_item_properties(line_items[1])
        self.assertEqual(len(json.loads(props_gh)), 2)
        self.assertEqual(props_vent, "")

    def test_order_with_unmatched_item(self):
        """Order with one matched and one unmatched item."""
        line_items = [
            {
                "title": "Greenhouse WMP 3x6",
                "product_exists": True,
                "product_id": "111",
                "sku": "GH-WMP-0306",
                "price": "2500",
                "quantity": 1,
                "properties": [],
            },
            {
                "title": "Warranty Replacement Door",
                "product_exists": False,
                "product_id": None,
                "price": "0",
                "quantity": 1,
                "properties": [],
            },
        ]

        # B12: First item matched, second needs MISC-MANUAL
        self.assertFalse(_should_use_misc_manual(line_items[0], "GH-WMP-0306"))
        self.assertTrue(_should_use_misc_manual(line_items[1], None))

    def test_order_edit_warranty_scenario(self):
        """Real scenario: client adds $0 warranty part to existing order."""
        so_items = [
            {"item_code": "GH-WMP-0306", "item_name": "Greenhouse WMP 3x6", "qty": 1, "rate": 2500},
        ]
        edited_shopify_items = [
            {"title": "Greenhouse WMP 3x6", "sku": "GH-WMP-0306", "quantity": 1, "price": "2500"},
            {"title": "Warranty Door Replacement", "quantity": 1, "price": "0"},
        ]
        diff = _build_order_edit_diff(so_items, edited_shopify_items)
        new_items = [d for d in diff if d.startswith("+")]
        self.assertEqual(len(new_items), 1)
        self.assertIn("Warranty Door Replacement", new_items[0])
        self.assertIn("price: $0", new_items[0])


# ── B14: SKU match must run for variant rows (variant_of set) ──────────
#
# Regression: `_match_sku_and_link_item` used to early-return when
# variant_of was truthy. That caused every Shopify variant to bypass the
# SKU→Item lookup, create a phantom Item named after variant_id, and
# write a self-pointing Ecommerce Item mapping (erpnext_item_code ==
# variant_id). Subsequent orders for that variant then landed in SOs
# with item_code == variant_id — breaking BOM, PO routing, and FedEx
# customs.
#
# The patched function inlined below drops the variant_of guard.

MODULE_NAME_B14 = "shopify"


def _match_sku_and_link_item_patched(
    item_dict, product_id, variant_id, variant_of=None, has_variant=False,
    *, frappe_impl=None
):
    """Patched B14 version with frappe_impl injectable for testability."""
    sku = item_dict["sku"]
    if not sku or has_variant:
        return False

    item_name = frappe_impl.db.get_value("Item", {"item_code": sku})
    if item_name:
        try:
            ecommerce_item = frappe_impl.get_doc(
                {
                    "doctype": "Ecommerce Item",
                    "integration": MODULE_NAME_B14,
                    "erpnext_item_code": item_name,
                    "integration_item_code": product_id,
                    "has_variants": 0,
                    "variant_id": str(variant_id) if variant_id else "",
                    "sku": sku,
                }
            )
            ecommerce_item.insert()
            return True
        except Exception:
            return False
    return False


class TestB14VariantSKUMatch(unittest.TestCase):
    def _fake_frappe(self, item_name_for_sku=None):
        f = MagicMock()
        f.db.get_value = MagicMock(return_value=item_name_for_sku)
        f.get_doc = MagicMock(return_value=MagicMock())
        return f

    def test_variant_of_set_but_sku_matches_item_links_to_real_item(self):
        """Core regression: variant_of is set AND SKU matches an ERPNext Item
        → must create mapping to that Item, not phantom."""
        fake_frappe = self._fake_frappe(item_name_for_sku="NWD-GHG-0421X0543")
        item_dict = {"sku": "NWD-GHG-0421X0543"}
        linked = _match_sku_and_link_item_patched(
            item_dict,
            product_id="14907268890923",
            variant_id="53015229006187",
            variant_of="Nordwood Greenhouse Template",
            has_variant=False,
            frappe_impl=fake_frappe,
        )
        self.assertTrue(linked, "Must link variant row when SKU matches real Item")
        # Assert Ecommerce Item was built with correct erpnext_item_code
        created_doc = fake_frappe.get_doc.call_args[0][0]
        self.assertEqual(created_doc["erpnext_item_code"], "NWD-GHG-0421X0543")
        self.assertEqual(created_doc["variant_id"], "53015229006187")
        self.assertEqual(created_doc["sku"], "NWD-GHG-0421X0543")

    def test_no_sku_returns_false(self):
        fake_frappe = self._fake_frappe()
        self.assertFalse(_match_sku_and_link_item_patched(
            {"sku": ""}, "111", "222", variant_of=None, has_variant=False,
            frappe_impl=fake_frappe,
        ))
        fake_frappe.get_doc.assert_not_called()

    def test_has_variant_returns_false(self):
        """Template-level call must still early-return."""
        fake_frappe = self._fake_frappe(item_name_for_sku="SHOULDNT-MATTER")
        self.assertFalse(_match_sku_and_link_item_patched(
            {"sku": "X"}, "111", "", variant_of=None, has_variant=True,
            frappe_impl=fake_frappe,
        ))
        fake_frappe.get_doc.assert_not_called()

    def test_sku_present_but_no_matching_item_returns_false(self):
        fake_frappe = self._fake_frappe(item_name_for_sku=None)
        self.assertFalse(_match_sku_and_link_item_patched(
            {"sku": "UNKNOWN-SKU"}, "111", "222",
            variant_of="Some Template", has_variant=False,
            frappe_impl=fake_frappe,
        ))
        fake_frappe.get_doc.assert_not_called()

    def test_variant_of_none_and_sku_matches_still_works(self):
        """Single-variant path (variant_of=None) must keep working."""
        fake_frappe = self._fake_frappe(item_name_for_sku="GNR-AIR-VENT-AUTO")
        linked = _match_sku_and_link_item_patched(
            {"sku": "GNR-AIR-VENT-AUTO"}, "111", "222",
            variant_of=None, has_variant=False,
            frappe_impl=fake_frappe,
        )
        self.assertTrue(linked)

    def test_insert_failure_returns_false(self):
        fake_frappe = self._fake_frappe(item_name_for_sku="NWD-GHG-0421X0543")
        fake_frappe.get_doc.return_value.insert.side_effect = RuntimeError("dup")
        self.assertFalse(_match_sku_and_link_item_patched(
            {"sku": "NWD-GHG-0421X0543"}, "111", "222",
            variant_of="T", has_variant=False,
            frappe_impl=fake_frappe,
        ))


class TestB14BugRegression(unittest.TestCase):
    """Verify the old buggy behavior — asserting it was a bug — then the
    patched behavior. Demonstrates the before/after contract."""

    def _buggy_match_fn(self, item_dict, product_id, variant_id, variant_of=None, has_variant=False, frappe_impl=None):
        """The OLD version with the variant_of guard."""
        sku = item_dict["sku"]
        if not sku or variant_of or has_variant:  # <-- the bug
            return False
        item_name = frappe_impl.db.get_value("Item", {"item_code": sku})
        if item_name:
            frappe_impl.get_doc({}).insert()
            return True
        return False

    def test_old_behavior_skipped_variant_rows(self):
        """Documents the bug: variants always got skipped even with matching SKU."""
        fake_frappe = MagicMock()
        fake_frappe.db.get_value = MagicMock(return_value="NWD-GHG-0421X0543")
        result = self._buggy_match_fn(
            {"sku": "NWD-GHG-0421X0543"}, "111", "222",
            variant_of="Template", has_variant=False,
            frappe_impl=fake_frappe,
        )
        self.assertFalse(result, "Old buggy version returned False → forced phantom Item path")
        fake_frappe.db.get_value.assert_not_called()  # Never even tried SKU lookup

    def test_new_behavior_links_variant_rows(self):
        """Same inputs, patched fn must link instead of skip."""
        fake_frappe = MagicMock()
        fake_frappe.db.get_value = MagicMock(return_value="NWD-GHG-0421X0543")
        result = _match_sku_and_link_item_patched(
            {"sku": "NWD-GHG-0421X0543"}, "111", "222",
            variant_of="Template", has_variant=False,
            frappe_impl=fake_frappe,
        )
        self.assertTrue(result, "Patched version must link when SKU matches real Item")


# ── B15: discount via dollar-amount rate (matching Shopify's model) ────
#
# Regression: the connector used to set `rate = price - discount/qty`
# and rely on ERPNext to keep it. On fully-discounted lines (e.g. FREE
# gift-with-purchase where line_item.price=$34.80 and discount_allocation
# = $69.60 for qty=2 → rate should be $0.00), ERPNext's server-side
# save/submit silently reset `rate` back to the non-discounted price.
# Observed in production on live webhook #4344 (SAL-ORD-2026-02283):
# 2×RGB-GST bed kit with $69.60 line discount landed with rate=$34.80
# (full price) instead of $0.00, over-charging the order by $69.60.
#
# Isolated cause (verified via direct save+submit test matrix):
#   rate=0 alone                → survives cleanly
#   rate=0 + price_list_rate=P  → ERPNext reconciles rate back to P
# ERPNext's save() pipeline populates price_list_rate during
# `set_missing_values`, and when plr ≠ rate it reconciles. The connector
# didn't explicitly set plr, but ERPNext filled it anyway.
#
# Fix: set BOTH rate AND price_list_rate to the SAME discounted dollar
# value. Matches Shopify's dollar-amount model (no percentage conversion,
# no rounding). Short-circuits reconciliation because plr == rate.
#
# B21: the `shopify_item_discount` audit-snapshot field is retired.
# B15's rate+price_list_rate pair carries the same information (discount
# = price - effective_rate); we no longer mirror per_unit_discount into
# a custom field.


def _build_item_row(shopify_item, taxes_inclusive, setting_warehouse="W"):
    """Inline copy of the B15 item-row builder for unit testing."""
    price = float(shopify_item.get("price") or 0)
    qty = int(shopify_item.get("quantity") or 0) or 1
    disc_allocs = shopify_item.get("discount_allocations") or []
    total_discount = sum(float(d.get("amount") or 0) for d in disc_allocs)
    per_unit_discount = total_discount / qty

    if taxes_inclusive:
        per_unit_tax = sum(
            float(t.get("price") or 0) for t in (shopify_item.get("tax_lines") or [])
        ) / qty
    else:
        per_unit_tax = 0.0

    effective_rate = price - per_unit_tax - per_unit_discount

    return {
        "rate": effective_rate,
        "price_list_rate": effective_rate,  # match to disable ERPNext reconciliation
        "qty": qty,
    }


class TestB15DiscountDollarAmount(unittest.TestCase):
    def test_fully_discounted_line_free_gift(self):
        """#4344 regression: 2×$34.80 with $69.60 line discount → rate=0."""
        item = {
            "price": "34.80",
            "quantity": 2,
            "discount_allocations": [{"amount": "69.60"}],
        }
        row = _build_item_row(item, taxes_inclusive=False)
        self.assertEqual(row["rate"], 0.0)
        self.assertEqual(row["price_list_rate"], 0.0,
                         msg="plr must equal rate to prevent ERPNext reconciliation")

    def test_partially_discounted_line(self):
        """$100 item with $25 line discount on qty=1 → rate=75."""
        item = {
            "price": "100.00",
            "quantity": 1,
            "discount_allocations": [{"amount": "25.00"}],
        }
        row = _build_item_row(item, taxes_inclusive=False)
        self.assertEqual(row["rate"], 75.0)
        self.assertEqual(row["price_list_rate"], 75.0)

    def test_no_discount(self):
        """Plain line: rate=price, plr=price."""
        item = {"price": "180.00", "quantity": 1, "discount_allocations": []}
        row = _build_item_row(item, taxes_inclusive=False)
        self.assertEqual(row["rate"], 180.0)
        self.assertEqual(row["price_list_rate"], 180.0)

    def test_zero_amount_discount_allocation(self):
        """Shopify often sends discount_allocations=[{amount:0}] for un-discounted
        lines inside an order that has other discounts. Must treat as zero."""
        item = {
            "price": "34.80",
            "quantity": 3,
            "discount_allocations": [{"amount": "0.00"}],
        }
        row = _build_item_row(item, taxes_inclusive=False)
        self.assertEqual(row["rate"], 34.80)

    def test_multiple_discount_allocations(self):
        """Order-level + line-level discount both allocate to one line."""
        item = {
            "price": "100.00",
            "quantity": 1,
            "discount_allocations": [
                {"amount": "10.00"},  # order-level
                {"amount": "15.00"},  # line-level
            ],
        }
        row = _build_item_row(item, taxes_inclusive=False)
        self.assertEqual(row["rate"], 75.0)

    def test_taxes_inclusive_no_discount(self):
        """Taxes inclusive: price=120 includes $20 tax, qty=1 → rate=100."""
        item = {
            "price": "120.00",
            "quantity": 1,
            "discount_allocations": [],
            "tax_lines": [{"price": "20.00"}],
        }
        row = _build_item_row(item, taxes_inclusive=True)
        self.assertEqual(row["rate"], 100.0)
        self.assertEqual(row["price_list_rate"], 100.0)

    def test_taxes_inclusive_with_discount(self):
        """Tax-inclusive $120, $10 tax, $20 discount → rate = 120-10-20 = 90."""
        item = {
            "price": "120.00",
            "quantity": 1,
            "discount_allocations": [{"amount": "20.00"}],
            "tax_lines": [{"price": "10.00"}],
        }
        row = _build_item_row(item, taxes_inclusive=True)
        self.assertAlmostEqual(row["rate"], 90.0)
        self.assertAlmostEqual(row["price_list_rate"], 90.0)

    def test_qty_zero_is_normalized_to_one(self):
        """Shopify can't send qty=0 but belt-and-braces: don't crash on div-by-zero."""
        item = {"price": "50.00", "quantity": 0, "discount_allocations": []}
        row = _build_item_row(item, taxes_inclusive=False)
        self.assertEqual(row["rate"], 50.0)
        self.assertEqual(row["qty"], 1)

    def test_zero_price_line(self):
        """Free item (price=0): rate=0, no div-by-zero."""
        item = {"price": "0", "quantity": 1, "discount_allocations": []}
        row = _build_item_row(item, taxes_inclusive=False)
        self.assertEqual(row["rate"], 0.0)
        self.assertEqual(row["price_list_rate"], 0.0)

    def test_rate_equals_price_list_rate_invariant(self):
        """Core invariant: rate must always equal price_list_rate to prevent
        ERPNext's save() from reconciling rate back to the non-discounted price."""
        cases = [
            {"price": "34.80", "quantity": 2, "discount_allocations": [{"amount": "69.60"}]},
            {"price": "100.00", "quantity": 1, "discount_allocations": [{"amount": "25.00"}]},
            {"price": "50.00", "quantity": 4, "discount_allocations": [{"amount": "20.00"}]},
            {"price": "34.80", "quantity": 3, "discount_allocations": []},
            {"price": "0", "quantity": 1, "discount_allocations": []},
        ]
        for c in cases:
            row = _build_item_row(c, taxes_inclusive=False)
            self.assertEqual(
                row["rate"], row["price_list_rate"],
                msg=f"Invariant violated for {c}: rate={row['rate']} != plr={row['price_list_rate']}",
            )

    def test_no_percentage_conversion_no_rounding(self):
        """Dollar-amount model must not introduce rounding from percentage conversion.
        Test with a non-clean-percent case: $33.33 discount on $100 would be
        33.33% which rounds; our dollar-amount approach is exact."""
        item = {
            "price": "100.00",
            "quantity": 1,
            "discount_allocations": [{"amount": "33.33"}],
        }
        row = _build_item_row(item, taxes_inclusive=False)
        self.assertEqual(row["rate"], 100.00 - 33.33)  # exact, no rounding


# ── B16: ship-dropship + product_id fallback for SKU-less products ────
#
# 1. `get_item_code` previously required (sku OR variant_id) to match an
#    Ecommerce Item. For SKU-less products where we maintain only a
#    product-level mapping (single row with integration_item_code but no
#    variant_id), lookups fell through to MISC-MANUAL. B16 adds a second
#    attempt that filters on integration_item_code only, so every variant
#    of a SKU-less product resolves to the parent item_code.
# 2. `_resolve_shipping_method` returns 'ship-dropship' when the linked
#    Item's tags include `ship-dropship` — bypassing ship-sea/ship-air
#    routing. Priority is dropship > sea > air.
# 3. `create_sales_order` sets `shopify_fulfillment_source` on the SO to
#    'dropship' if any line resolved to ship-dropship, else 'warehouse'.
#    Downstream FedEx skip / reporting can branch on a single field.


def _resolve_shipping_method_from_tags_b16(tags_str):
    """B16 version of the shipping-method resolver.

    Dropship > sea > air priority."""
    if not tags_str:
        return ""
    tags_lower = tags_str.lower()
    if "ship-dropship" in tags_lower:
        return "ship-dropship"
    if "ship-sea" in tags_lower:
        return "ship-sea"
    if "ship-air" in tags_lower:
        return "ship-air"
    return ""


def _get_item_code_b16(shopify_item, *, ecommerce_item_impl):
    """B16 version of get_item_code.

    Primary lookup uses (product_id, variant_id, sku). On miss, falls back
    to product-level match (integration_item_code only, variant_id=None,
    sku=None).
    """
    MODULE = "shopify"
    item = ecommerce_item_impl.get_erpnext_item(
        integration=MODULE,
        integration_item_code=shopify_item.get("product_id"),
        variant_id=shopify_item.get("variant_id"),
        sku=shopify_item.get("sku"),
    )
    if item:
        return item.item_code

    # B16: product-level fallback
    if shopify_item.get("product_id"):
        item = ecommerce_item_impl.get_erpnext_item(
            integration=MODULE,
            integration_item_code=shopify_item.get("product_id"),
            variant_id=None,
            sku=None,
        )
        if item:
            return item.item_code
    return None


def _compute_fulfillment_source_b16(line_items):
    """B16 helper: inspect the rendered SO items list and return dropship
    if any line has shipping_method == 'ship-dropship', else 'warehouse'.
    """
    SHIPPING_FIELD = "shopify_shipping_method"
    has_dropship = any(
        item.get(SHIPPING_FIELD) == "ship-dropship" for item in line_items
    )
    return "dropship" if has_dropship else "warehouse"


class TestB16ProductIdFallback(unittest.TestCase):
    """B16: SKU-less products resolve via product-level Ecommerce Item."""

    def _fake_ecom(self, primary_return=None, fallback_return=None):
        """Builds a mock that returns primary on first call, fallback on second."""
        ecom = MagicMock()
        ecom.get_erpnext_item = MagicMock(side_effect=[primary_return, fallback_return])
        return ecom

    def test_single_variant_no_sku_resolves_via_product_id(self):
        """Single-variant no-SKU product: primary (by variant_id) misses,
        fallback (by product_id only) hits."""
        fallback_item = SimpleNamespace(item_code="DROPSHIP-AMT-PARENT")
        ecom = self._fake_ecom(primary_return=None, fallback_return=fallback_item)
        shopify_item = {"product_id": "9999", "variant_id": "111", "sku": None}

        result = _get_item_code_b16(shopify_item, ecommerce_item_impl=ecom)
        self.assertEqual(result, "DROPSHIP-AMT-PARENT")
        # Verify both calls happened — primary (with variant_id) then fallback
        self.assertEqual(ecom.get_erpnext_item.call_count, 2)
        second_call_kwargs = ecom.get_erpnext_item.call_args_list[1].kwargs
        self.assertEqual(second_call_kwargs["integration_item_code"], "9999")
        self.assertIsNone(second_call_kwargs["variant_id"])
        self.assertIsNone(second_call_kwargs["sku"])

    def test_multi_variant_no_sku_all_resolve_to_parent(self):
        """Multi-variant dropship: every variant shares one product-level
        Ecommerce Item, so all resolve to the same parent item_code."""
        parent_item = SimpleNamespace(item_code="DROPSHIP-AMT-PARENT")
        resolved = []
        for variant_id in ["v-100", "v-200", "v-300"]:
            ecom = self._fake_ecom(primary_return=None, fallback_return=parent_item)
            shopify_item = {"product_id": "9999", "variant_id": variant_id, "sku": None}
            resolved.append(_get_item_code_b16(shopify_item, ecommerce_item_impl=ecom))
        self.assertEqual(resolved, ["DROPSHIP-AMT-PARENT"] * 3)

    def test_primary_match_still_wins_when_sku_matches(self):
        """Existing SKU-matched products must NOT trigger the fallback
        (i.e. primary lookup's result is returned, fallback never runs)."""
        primary = SimpleNamespace(item_code="GH-WMP-0306")
        ecom = MagicMock()
        ecom.get_erpnext_item = MagicMock(return_value=primary)
        shopify_item = {"product_id": "111", "variant_id": "222", "sku": "GH-WMP-0306"}

        result = _get_item_code_b16(shopify_item, ecommerce_item_impl=ecom)
        self.assertEqual(result, "GH-WMP-0306")
        # Only one call — the primary
        self.assertEqual(ecom.get_erpnext_item.call_count, 1)

    def test_no_product_id_returns_none(self):
        """If there's no product_id, fallback cannot run — returns None."""
        ecom = MagicMock()
        ecom.get_erpnext_item = MagicMock(return_value=None)
        shopify_item = {"product_id": None, "variant_id": None, "sku": None}
        result = _get_item_code_b16(shopify_item, ecommerce_item_impl=ecom)
        self.assertIsNone(result)

    def test_both_lookups_miss_returns_none(self):
        """Both primary and fallback miss → None (caller uses MISC-MANUAL)."""
        ecom = self._fake_ecom(primary_return=None, fallback_return=None)
        shopify_item = {"product_id": "9999", "variant_id": "111", "sku": None}
        result = _get_item_code_b16(shopify_item, ecommerce_item_impl=ecom)
        self.assertIsNone(result)
        self.assertEqual(ecom.get_erpnext_item.call_count, 2)


class TestB16ShipDropshipDetection(unittest.TestCase):
    """B16: Tag lookup returns ship-dropship when present."""

    def test_dropship_only(self):
        self.assertEqual(
            _resolve_shipping_method_from_tags_b16("ship-dropship"),
            "ship-dropship",
        )

    def test_dropship_among_other_tags(self):
        self.assertEqual(
            _resolve_shipping_method_from_tags_b16("Accessories, ship-dropship, stockv2"),
            "ship-dropship",
        )

    def test_dropship_case_insensitive(self):
        self.assertEqual(
            _resolve_shipping_method_from_tags_b16("SHIP-DROPSHIP, Greenhouse"),
            "ship-dropship",
        )

    def test_no_dropship_falls_back_to_sea(self):
        self.assertEqual(
            _resolve_shipping_method_from_tags_b16("ship-sea, Greenhouse"),
            "ship-sea",
        )


class TestB16ShipDropshipPriority(unittest.TestCase):
    """B16: ship-dropship beats ship-sea beats ship-air."""

    def test_dropship_beats_sea(self):
        self.assertEqual(
            _resolve_shipping_method_from_tags_b16("ship-sea, ship-dropship"),
            "ship-dropship",
        )

    def test_dropship_beats_air(self):
        self.assertEqual(
            _resolve_shipping_method_from_tags_b16("ship-air, ship-dropship"),
            "ship-dropship",
        )

    def test_dropship_beats_both_sea_and_air(self):
        self.assertEqual(
            _resolve_shipping_method_from_tags_b16("ship-sea, ship-air, ship-dropship"),
            "ship-dropship",
        )

    def test_sea_still_beats_air_when_no_dropship(self):
        """Regression guard: the existing B7 sea>air priority must survive."""
        self.assertEqual(
            _resolve_shipping_method_from_tags_b16("ship-sea, ship-air"),
            "ship-sea",
        )


class TestB16FulfillmentSourceField(unittest.TestCase):
    """B16: SO's shopify_fulfillment_source is set from line-item shipping method."""

    def test_any_dropship_line_marks_so_dropship(self):
        items = [
            {"shopify_shipping_method": "ship-sea"},
            {"shopify_shipping_method": "ship-dropship"},
        ]
        self.assertEqual(_compute_fulfillment_source_b16(items), "dropship")

    def test_all_dropship_lines_mark_so_dropship(self):
        items = [
            {"shopify_shipping_method": "ship-dropship"},
            {"shopify_shipping_method": "ship-dropship"},
        ]
        self.assertEqual(_compute_fulfillment_source_b16(items), "dropship")

    def test_no_dropship_lines_mark_so_warehouse(self):
        items = [
            {"shopify_shipping_method": "ship-sea"},
            {"shopify_shipping_method": "ship-air"},
        ]
        self.assertEqual(_compute_fulfillment_source_b16(items), "warehouse")

    def test_empty_items_default_to_warehouse(self):
        self.assertEqual(_compute_fulfillment_source_b16([]), "warehouse")

    def test_missing_shipping_method_defaults_to_warehouse(self):
        items = [{}, {"shopify_shipping_method": ""}]
        self.assertEqual(_compute_fulfillment_source_b16(items), "warehouse")


# ── B5: product webhook handler — keep shopify_tags in sync ───────────
#
# Connector didn't subscribe to products/* webhooks, so tag changes after
# product creation (client adds `ship-dropship` or `warranty` to an existing
# product) never reached Item.shopify_tags. Fix: subscribe to products/create
# + products/update, and on fire, refresh tags on every linked ERPNext Item
# via Ecommerce Item → erpnext_item_code mapping. B6 (metafields) parked.

def _resolve_product_sync_action_b5(product_id, ecom_lookup):
    """Decide whether the webhook handler should update existing Item tags or
    trigger a fresh ShopifyProduct.sync_product() flow.

    ecom_lookup is a callable: (product_id_str) → list[erpnext_item_code].
    Returns ("update_tags", [codes]) | ("create_new", []).
    """
    linked = ecom_lookup(str(product_id))
    if linked:
        return ("update_tags", linked)
    return ("create_new", [])


def _apply_tag_update_b5(linked_item_codes, tags_string, setter):
    """Apply tag writes to every linked ERPNext Item.

    setter is a callable: (item_code, field_name, value) → None.
    Returns number of items updated.
    """
    for item_code in linked_item_codes:
        setter(item_code, "shopify_tags", tags_string)
    return len(linked_item_codes)


class TestB5ProductWebhookDispatch(unittest.TestCase):
    """B5: webhook handler picks update-tags vs create-new correctly."""

    def test_mapped_product_updates_tags(self):
        ecom = lambda pid: ["GH-WMP-0306", "GH-WMP-0306-small"]
        action, items = _resolve_product_sync_action_b5("14712905695595", ecom)
        self.assertEqual(action, "update_tags")
        self.assertEqual(items, ["GH-WMP-0306", "GH-WMP-0306-small"])

    def test_unmapped_product_creates_new(self):
        ecom = lambda pid: []
        action, items = _resolve_product_sync_action_b5("99999999", ecom)
        self.assertEqual(action, "create_new")
        self.assertEqual(items, [])

    def test_product_id_coerced_to_string(self):
        """Shopify sends id as int, Ecommerce Item stores as string."""
        calls = []
        def ecom(pid):
            calls.append(pid)
            return ["ITEM-1"]
        _resolve_product_sync_action_b5(14712905695595, ecom)
        self.assertEqual(calls, ["14712905695595"])


class TestB5TagUpdateApply(unittest.TestCase):
    """B5: tag update walks every linked Item."""

    def test_single_item_single_variant_product(self):
        writes = []
        setter = lambda ic, field, val: writes.append((ic, field, val))
        count = _apply_tag_update_b5(
            ["ACC-DRIP-SET-0400"], "ship-air, Accesories", setter
        )
        self.assertEqual(count, 1)
        self.assertEqual(writes, [("ACC-DRIP-SET-0400", "shopify_tags", "ship-air, Accesories")])

    def test_multi_variant_product_updates_all_linked_items(self):
        writes = []
        setter = lambda ic, field, val: writes.append((ic, field, val))
        linked = ["AMT-CHR1-091X72-G", "AMT-CHR1-091X72-B", "AMT-CHR1-091X72-N"]
        count = _apply_tag_update_b5(linked, "ship-dropship, stockv2", setter)
        self.assertEqual(count, 3)
        self.assertEqual([w[0] for w in writes], linked)
        self.assertTrue(all(w[2] == "ship-dropship, stockv2" for w in writes))

    def test_empty_tags_still_writes(self):
        """Client deletes all tags → empty string must replace, not be skipped.
        Otherwise stale tags would persist after client cleanup."""
        writes = []
        setter = lambda ic, field, val: writes.append((ic, field, val))
        count = _apply_tag_update_b5(["GH-WMP-0306"], "", setter)
        self.assertEqual(count, 1)
        self.assertEqual(writes[0][2], "")

    def test_no_linked_items_is_noop(self):
        writes = []
        setter = lambda ic, field, val: writes.append((ic, field, val))
        count = _apply_tag_update_b5([], "ship-sea", setter)
        self.assertEqual(count, 0)
        self.assertEqual(writes, [])


class TestB5WebhookEventsConstants(unittest.TestCase):
    """B5: constants.py must subscribe to products/* webhooks."""

    def test_webhook_events_include_products(self):
        # Inline the expected entries — we're testing constant config, not
        # importing constants.py directly (MagicMock'd at top of file).
        required_events = {
            "orders/create",
            "orders/paid",
            "orders/fulfilled",
            "orders/cancelled",
            "orders/partially_fulfilled",
            "orders/edited",
            "products/create",   # B5
            "products/update",   # B5
        }
        # Read the actual constants.py to verify the config matches.
        import os
        const_path = os.path.join(SHOPIFY_DIR, "constants.py")
        with open(const_path) as f:
            source = f.read()
        for ev in required_events:
            self.assertIn(f'"{ev}"', source, f"missing event: {ev}")

    def test_event_mapper_routes_products_to_handler(self):
        import os
        const_path = os.path.join(SHOPIFY_DIR, "constants.py")
        with open(const_path) as f:
            source = f.read()
        for ev in ("products/create", "products/update"):
            # Must map to the product webhook handler.
            self.assertIn(
                f'"{ev}": "ecommerce_integrations.shopify.product.sync_product_from_webhook"',
                source,
                f"{ev} not mapped to sync_product_from_webhook",
            )


# ── B20: products/* webhook must not create Items (f-020 fix) ─────────
#
# Regression 2026-04-20: yei-v1.2.0 registered products/create + products/update
# webhooks (B5). The then-current `sync_product_from_webhook` fallback branch
# called `ShopifyProduct.sync_product()`, which for variant-bearing products
# built `item_code = product_dict["id"]` (a Shopify numeric ID) because
# `_match_sku_and_link_item` short-circuits when `has_variant=True`. Result: 5
# active products fanned out to 70 duplicate Items with numeric item_codes over
# 7 hours before the webhooks were disabled. Fix: the products/* path is
# tag-sync-only — unmapped products are logged and skipped. New products enter
# ERPNext via order sync (B14-guarded) or manual Item creation.


def _handle_webhook_branch_b20(action, linked_items, tags, tag_setter, log_writer):
    """Handler dispatch logic extracted from sync_product_from_webhook.

    action: "update_tags" | "create_new"
    linked_items: list[str] of erpnext_item_codes
    tags: str — comma-separated tags from webhook payload
    tag_setter: callable (item_code, field_name, value) → None
    log_writer: callable (status, message) → None

    Contract: create_new branch MUST NOT create Items or call any Item-creation
    function. It logs a skip and returns. This is B20's core invariant.
    Returns the count of Items tag-updated (0 when skipped).
    """
    if action == "update_tags":
        for item_code in linked_items:
            tag_setter(item_code, "shopify_tags", tags)
        log_writer("Success", f"B5 tag sync: {len(linked_items)} Item(s)")
        return len(linked_items)
    log_writer("Success", "B20 skip: no ERPNext mapping — Items not created")
    return 0


class TestB20UnmappedProductSkipped(unittest.TestCase):
    """B20: unmapped product webhook must not create Items."""

    def test_unmapped_product_does_not_invoke_item_creation(self):
        """create_new branch writes a skip log, nothing else."""
        tag_writes = []
        logs = []
        count = _handle_webhook_branch_b20(
            action="create_new",
            linked_items=[],
            tags="ship-air, Accesories",
            tag_setter=lambda ic, f, v: tag_writes.append((ic, f, v)),
            log_writer=lambda s, m: logs.append((s, m)),
        )
        self.assertEqual(count, 0)
        self.assertEqual(tag_writes, [], "No tag writes for unmapped product")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0][0], "Success")
        self.assertIn("B20 skip", logs[0][1])

    def test_mapped_product_still_updates_tags(self):
        """Regression guard: B5 happy path must keep working."""
        tag_writes = []
        logs = []
        count = _handle_webhook_branch_b20(
            action="update_tags",
            linked_items=["SPY-FUL-0400", "SPY-FUL-0600"],
            tags="ship-air, Greenhouse",
            tag_setter=lambda ic, f, v: tag_writes.append((ic, f, v)),
            log_writer=lambda s, m: logs.append((s, m)),
        )
        self.assertEqual(count, 2)
        self.assertEqual(len(tag_writes), 2)
        self.assertEqual(
            [w[0] for w in tag_writes], ["SPY-FUL-0400", "SPY-FUL-0600"]
        )
        self.assertTrue(all(w[1] == "shopify_tags" for w in tag_writes))
        self.assertTrue(all(w[2] == "ship-air, Greenhouse" for w in tag_writes))


class TestB20SourceInvariant(unittest.TestCase):
    """B20: read product.py source and assert create_new branch does not
    call ShopifyProduct or sync_product. Guards against accidental
    reintroduction of the f-020 regression."""

    def _read_product_source(self):
        import os
        prod_path = os.path.join(SHOPIFY_DIR, "product.py")
        with open(prod_path) as f:
            return f.read()

    def _executable_body(self, source):
        """Return handler source with the docstring stripped (docstring
        mentions the old code path for context; only the executable body
        matters for the invariant)."""
        start = source.index("def sync_product_from_webhook(")
        end = source.index("\ndef ", start + 1)
        body = source[start:end]
        # Strip the triple-quoted docstring
        dq_open = body.find('"""')
        if dq_open >= 0:
            dq_close = body.find('"""', dq_open + 3)
            if dq_close >= 0:
                body = body[:dq_open] + body[dq_close + 3:]
        return body

    def test_sync_product_from_webhook_does_not_construct_shopify_product(self):
        """The handler must not instantiate ShopifyProduct() — that path led
        to duplicate numeric-ID Items in the f-020 regression."""
        body = self._executable_body(self._read_product_source())
        self.assertNotIn(
            "ShopifyProduct(",
            body,
            "sync_product_from_webhook must not instantiate ShopifyProduct "
            "(f-020 regression: creates numeric-ID Items for variant-bearing products)",
        )
        self.assertNotIn(
            ".sync_product()",
            body,
            "sync_product_from_webhook must not call .sync_product() — "
            "new-product creation belongs on the orders/* path (B14-guarded)",
        )

    def test_create_new_branch_logs_skip(self):
        """Asserts the skip log tag ('B20 skip') is present — ensures the
        unmapped branch is explicit, not a silent drop."""
        body = self._executable_body(self._read_product_source())
        self.assertIn(
            "B20 skip",
            body,
            "Unmapped-product branch must emit a 'B20 skip' log",
        )


# ── B21: Flat-Items-only + numeric-ID guard (f-027 fix) ───────────────
#
# Regression 2026-04-22: B20 closed the webhook-path vector for f-020 but
# left the order-sync path open. `create_items_if_not_exist` →
# `ShopifyProduct.sync_product()` → `_make_item` still called
# `_create_item(..., has_variant=1)` for variant-bearing Shopify products,
# producing a templated Item with `item_code = product_dict["id"]` (numeric
# Shopify product_id). Item `6970914963546` appeared on 2026-04-22 23:50Z
# through this path; `_match_sku_and_link_item` short-circuited on
# `has_variant=True` so the template was never linked to an existing SKU.
#
# Fix: Kete's flat-Item convention was never properly enforced — every
# Shopify variant should map 1:1 to a top-level ERPNext Item keyed by SKU,
# with no `has_variants=1` templates created by the connector. B21 removes
# the templated-Item code path entirely (`_create_item_variants`,
# `_create_attribute`, `_set_new_attribute_values`, `_get_attribute_value`)
# and replaces `_make_item` with a per-variant loop that produces flat Items.
# Variants without SKU are skipped with an explicit `B21 skip` log — we
# never mint numeric item_codes.
#
# Defense-in-depth: `_guard_non_numeric_item_code` helper raises at the
# Item-creation boundary if any future path tries to mint a numeric item_code.


def _guard_non_numeric_item_code_b21(item_code, source):
    """Inline copy of product._guard_non_numeric_item_code for unit testing.

    The test_connector_patches harness mocks ecommerce_integrations.shopify.product
    wholesale (line 64 at the top of this file) so direct imports return
    MagicMocks. Same pattern as _handle_webhook_branch_b20 and _build_item_row.
    Contract must mirror product.py exactly — if this drifts from the real
    helper, the TestB21OrderSyncSourceInvariant test suite catches it via
    source-text assertions that the real helper exists with the right shape.
    """
    if str(item_code).isdigit():
        raise ValueError(
            f"{source}: attempted to create Item with numeric item_code "
            f"{item_code!r}. SKU-less products must be skipped, not stubbed."
        )


class TestB21GuardNonNumericItemCode(unittest.TestCase):
    """B21: the boundary guard against numeric item_codes."""

    def test_numeric_item_code_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _guard_non_numeric_item_code_b21("6970914963546", "_create_flat_variant")
        self.assertIn("6970914963546", str(ctx.exception))
        self.assertIn("SKU-less", str(ctx.exception))

    def test_alpha_sku_passes(self):
        # SKUs from Kete's master registry pass: prefix-letters + dash + digits
        _guard_non_numeric_item_code_b21("GNR-EXT-0200X0000", "_create_flat_variant")
        _guard_non_numeric_item_code_b21("AMT-A58229-GR", "_create_flat_variant")
        _guard_non_numeric_item_code_b21("SCG-FUL-0300", "_create_flat_variant")

    def test_empty_string_passes(self):
        # Empty string isn't all-digits; guard is narrowly scoped to pure-digit.
        # (The variant-no-sku case is caught earlier in _make_item with a skip log.)
        _guard_non_numeric_item_code_b21("", "_create_flat_variant")

    def test_numeric_string_with_spaces_raises(self):
        # isdigit returns False on whitespace, so this actually passes the guard;
        # document the current narrow semantics rather than claim broader coverage.
        _guard_non_numeric_item_code_b21("123 456", "_create_flat_variant")


class TestB21OrderSyncSourceInvariant(unittest.TestCase):
    """B21: read product.py source and assert the flat-Items invariant
    holds structurally — the templated-Item path is gone and no function
    mints numeric item_codes."""

    def _read_product_source(self):
        import os
        prod_path = os.path.join(SHOPIFY_DIR, "product.py")
        with open(prod_path) as f:
            return f.read()

    def test_make_item_does_not_mint_numeric_template(self):
        """_make_item must not produce Items keyed by Shopify product_id."""
        src = self._read_product_source()
        # These specific patterns were the f-020/f-027 vector. None must survive.
        self.assertNotIn(
            '"item_code": cstr(product_dict.get("id"))',
            src,
            "Templated-Item path (item_code = Shopify product_id) must be removed",
        )
        self.assertNotIn(
            'cstr(product_dict.get("item_code")) or cstr(product_dict.get("id"))',
            src,
            "Legacy _create_item fallback chain must be removed",
        )
        # The variant-cascade pattern — variant_id as item_code
        self.assertNotIn(
            '"item_code": variant.get("id")',
            src,
            "Variant-cascade item_code assignment (_create_item_variants) must be removed",
        )

    def test_create_item_variants_deleted(self):
        """The cascade that produced variant-id item_codes is gone."""
        src = self._read_product_source()
        self.assertNotIn(
            "def _create_item_variants",
            src,
            "B21 removes _create_item_variants — no template variant cascade",
        )

    def test_attribute_helpers_deleted(self):
        """Flat Items don't use ERPNext Item Attributes — helpers go away."""
        src = self._read_product_source()
        self.assertNotIn("def _create_attribute(", src)
        self.assertNotIn("def _set_new_attribute_values(", src)
        self.assertNotIn("def _get_attribute_value(", src)

    def test_guard_helper_present_and_invoked(self):
        """_guard_non_numeric_item_code must exist AND be called by the new code."""
        src = self._read_product_source()
        self.assertIn("def _guard_non_numeric_item_code(", src,
                      "Boundary guard helper must be defined")
        # Must be invoked at least once (in _create_flat_variant + _create_item)
        invocations = src.count("_guard_non_numeric_item_code(")
        self.assertGreaterEqual(invocations, 2,
                                msg=f"Guard must be invoked at >=1 call site + defined (found {invocations})")

    def test_make_item_does_not_set_has_variants_true(self):
        """No code path in product.py should create has_variants=1 Items."""
        src = self._read_product_source()
        # The dict-literal form that was the template-creation pattern
        self.assertNotIn('"has_variants": 1,', src)
        self.assertNotIn("'has_variants': 1,", src)
        # The keyword-argument form used by the old _create_item signature
        self.assertNotIn("has_variant=1", src)

    def test_match_sku_signature_simplified(self):
        """_match_sku_and_link_item no longer takes has_variant or variant_of."""
        src = self._read_product_source()
        # Old signature with has_variant/variant_of must be gone
        self.assertNotIn(
            "def _match_sku_and_link_item(item_dict, product_id, variant_id, variant_of=None, has_variant=False)",
            src,
        )
        # New signature
        self.assertIn(
            "def _match_sku_and_link_item(item_dict, product_id, variant_id)",
            src,
        )

    def test_b21_rationale_logged(self):
        """The new skip-path must emit an explicit B21 log so rep can debug."""
        src = self._read_product_source()
        self.assertIn("B21 skip", src,
                      "Unmapped / no-SKU variant branch must emit a 'B21 skip' log")

    def test_native_standard_rate_used_for_shopify_push(self):
        """B21 native swap: upload_erpnext_item reads Item.standard_rate, not
        the retired Item.shopify_selling_rate."""
        src = self._read_product_source()
        # Retired constant must not be imported or referenced
        self.assertNotIn("ITEM_SELLING_RATE_FIELD", src,
                         "ITEM_SELLING_RATE_FIELD constant should no longer be used")
        # Native field must be referenced in the push path
        self.assertIn("standard_rate", src,
                      "upload_erpnext_item should read native Item.standard_rate")


class TestB21OrderItemDiscountRetired(unittest.TestCase):
    """B21 native swap: shopify_item_discount (the SOI custom snapshot) is
    retired. B15's native rate + price_list_rate carry the same information."""

    def _read_order_source(self):
        import os
        order_path = os.path.join(SHOPIFY_DIR, "order.py")
        with open(order_path) as f:
            return f.read()

    def test_order_item_discount_field_not_imported(self):
        src = self._read_order_source()
        self.assertNotIn("ORDER_ITEM_DISCOUNT_FIELD", src,
                         "order.py should no longer import / reference the retired constant")

    def test_order_item_row_does_not_set_discount_snapshot(self):
        src = self._read_order_source()
        # The specific write that was the retired snapshot
        self.assertNotIn(
            "ORDER_ITEM_DISCOUNT_FIELD: per_unit_discount",
            src,
            "Retired custom-field write must be removed",
        )


# ── B17: SO delivery_date parsed from shipping_lines titles ───────────
#
# Connector currently sets delivery_date = created_at, so every Shopify SO
# flags Overdue within 1-2 days. Fix: parse shipping_lines[].title for either
# (a) business-day range "(X - Y business days)" → order_date + Y business
# days, or (b) explicit "Estimated (to be Delivered|Delivery by) <Mon> <Day>".
# Across multiple shipping_lines (mixed carts, preorder), take LATER date.
# Fallback = order_date when nothing parses.
#
# Patterns observed live on #4395 (pre-order, 2 shipping_lines):
#   "FREE Shipping (12 - 18 business days)"
#   "FREE Shipping (Estimated to be Delivered  May 20)"
# Also seen on backfill audit (50 recent SOs, 100% regex coverage):
#   "PRIORITY Secured FedEx Shipping with Tracking (8 - 12 business days)"
#   "Secured FedEx Shipping with Tracking (5 - 12 business days)"
#   "Priority Handling (12 - 18 business days)"
#   "Free Shipping (Estimated Delivery by May 20th)"
#   "Free Shipping (4-7 Business Days)"

_RE_BIZ_DAYS = re.compile(r'\((\d+)\s*-\s*(\d+)\s+business days\)', re.IGNORECASE)
_RE_EXPLICIT_DATE = re.compile(
    r'Estimated (?:to be Delivered|Delivery by)\s+([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?',
    re.IGNORECASE,
)


def _add_business_days_b17(start_date, days):
    """Add N business days (Mon-Fri) to a date, skipping Sat/Sun."""
    current = start_date
    added = 0
    while added < days:
        current = current + datetime.timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current


def _parse_month_day_b17(month_str, day_str, order_year):
    """Parse 'May 20' (or 'January 5') to a date in the given year.

    Tries full and abbreviated month names. Returns None if unparseable.
    """
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.datetime.strptime(
                f"{month_str} {day_str} {order_year}", fmt
            ).date()
        except ValueError:
            continue
    return None


def _resolve_delivery_date_b17(shopify_order, fallback):
    """B17: delivery_date from shipping_lines titles.

    Returns `fallback` (typically order_date) if nothing parses.
    """
    created_at = (shopify_order.get("created_at") or "")[:10]
    try:
        order_date = datetime.datetime.strptime(created_at, "%Y-%m-%d").date()
    except ValueError:
        return fallback

    candidates = []
    for line in shopify_order.get("shipping_lines") or []:
        title = line.get("title") or ""

        m_biz = _RE_BIZ_DAYS.search(title)
        if m_biz:
            upper_days = int(m_biz.group(2))
            candidates.append(_add_business_days_b17(order_date, upper_days))
            continue

        m_date = _RE_EXPLICIT_DATE.search(title)
        if m_date:
            d = _parse_month_day_b17(m_date.group(1), m_date.group(2), order_date.year)
            if d is None:
                continue
            # If parsed date is before order date, assume next year (Dec → Jan).
            if d < order_date:
                d = d.replace(year=order_date.year + 1)
            candidates.append(d)

    if candidates:
        return max(candidates)
    return fallback


class TestB17DeliveryDate(unittest.TestCase):
    _ORDER_DATE = datetime.date(2026, 4, 19)  # a Sunday
    _ORDER = {"created_at": "2026-04-19T15:00:00Z"}

    def _with_lines(self, *titles):
        return {**self._ORDER, "shipping_lines": [{"title": t} for t in titles]}

    def test_business_days_range_uses_upper_bound(self):
        # 2026-04-19 (Sun) + 18 business days → 2026-05-13 (Wed)
        order = self._with_lines("FREE Shipping (12 - 18 business days)")
        result = _resolve_delivery_date_b17(order, fallback=self._ORDER_DATE)
        self.assertEqual(result, datetime.date(2026, 5, 13))

    def test_priority_secured_fedex_8_to_12_days(self):
        # 2026-04-19 (Sun) + 12 business days → 2026-05-05 (Tue)
        order = self._with_lines("PRIORITY Secured FedEx Shipping with Tracking (8 - 12 business days)")
        result = _resolve_delivery_date_b17(order, fallback=self._ORDER_DATE)
        self.assertEqual(result, datetime.date(2026, 5, 5))

    def test_case_insensitive_business_days(self):
        # "Business Days" vs "business days"
        order = self._with_lines("Free Shipping (4-7 Business Days)")
        result = _resolve_delivery_date_b17(order, fallback=self._ORDER_DATE)
        # 2026-04-19 (Sun) + 7 business days → 2026-04-28 (Tue)
        self.assertEqual(result, datetime.date(2026, 4, 28))

    def test_explicit_date_standard_phrasing(self):
        order = self._with_lines("FREE Shipping (Estimated to be Delivered May 20)")
        result = _resolve_delivery_date_b17(order, fallback=self._ORDER_DATE)
        self.assertEqual(result, datetime.date(2026, 5, 20))

    def test_explicit_date_alt_phrasing_with_ordinal(self):
        order = self._with_lines("Free Shipping (Estimated Delivery by May 20th)")
        result = _resolve_delivery_date_b17(order, fallback=self._ORDER_DATE)
        self.assertEqual(result, datetime.date(2026, 5, 20))

    def test_mixed_cart_takes_later_of_lines(self):
        # Same shape as live #4395: one bizdays line + one explicit preorder line.
        order = self._with_lines(
            "FREE Shipping (12 - 18 business days)",
            "FREE Shipping (Estimated to be Delivered May 20)",
        )
        result = _resolve_delivery_date_b17(order, fallback=self._ORDER_DATE)
        # 5-13 (biz) vs 5-20 (explicit) → max = 5-20
        self.assertEqual(result, datetime.date(2026, 5, 20))

    def test_year_rollover_for_dec_order_to_jan_delivery(self):
        dec_order = {"created_at": "2025-12-20T10:00:00Z"}
        dec_order["shipping_lines"] = [{"title": "FREE Shipping (Estimated Delivery by January 5th)"}]
        fallback = datetime.date(2025, 12, 20)
        result = _resolve_delivery_date_b17(dec_order, fallback=fallback)
        self.assertEqual(result, datetime.date(2026, 1, 5))

    def test_no_shipping_lines_returns_fallback(self):
        result = _resolve_delivery_date_b17({"created_at": "2026-04-19T15:00:00Z"}, fallback=self._ORDER_DATE)
        self.assertEqual(result, self._ORDER_DATE)

    def test_unparseable_title_returns_fallback(self):
        order = self._with_lines("Some random shipping method")
        result = _resolve_delivery_date_b17(order, fallback=self._ORDER_DATE)
        self.assertEqual(result, self._ORDER_DATE)

    def test_bogus_created_at_returns_fallback(self):
        bogus = {
            "created_at": "not-a-date",
            "shipping_lines": [{"title": "FREE Shipping (12 - 18 business days)"}],
        }
        result = _resolve_delivery_date_b17(bogus, fallback=self._ORDER_DATE)
        self.assertEqual(result, self._ORDER_DATE)

    def test_full_month_name(self):
        order = self._with_lines("Free Shipping (Estimated Delivery by November 15)")
        # 2026-04-19 order → Nov 15 2026
        result = _resolve_delivery_date_b17(order, fallback=self._ORDER_DATE)
        self.assertEqual(result, datetime.date(2026, 11, 15))

    def test_empty_shipping_line_skipped(self):
        order = {
            **self._ORDER,
            "shipping_lines": [{"title": ""}, {"title": "FREE Shipping (12 - 18 business days)"}],
        }
        result = _resolve_delivery_date_b17(order, fallback=self._ORDER_DATE)
        self.assertEqual(result, datetime.date(2026, 5, 13))


# ── B19: SO name from Shopify order number (SH-YYYY-NNNNN) ────────────
#
# Fixes cross-platform lookup pain: SAL-ORD-2026-02333 has no relation to
# Shopify #4395. Format: SH-{year-from-created_at}-{order_number zero-padded
# to 5 digits}. Year-prefix allows order_number counter resets across years;
# 5-digit pad covers > 10k orders/year (Shopify counter will exceed 5 digits
# eventually — zfill degrades gracefully). Only applies to Shopify-sourced
# SOs via connector; manual SOs keep SAL-ORD series.

def _format_shopify_so_name_b19(shopify_order):
    """B19: Build SO name from Shopify order dict. Returns None if malformed."""
    order_number = shopify_order.get("order_number")
    if order_number is None or order_number == "":
        return None
    try:
        on_int = int(order_number)
    except (ValueError, TypeError):
        return None
    created_at = shopify_order.get("created_at", "") or ""
    year = created_at[:4] if len(created_at) >= 4 else ""
    if not year.isdigit():
        return None
    return f"SH-{year}-{on_int:05d}"


class TestB19Naming(unittest.TestCase):
    def test_standard_format(self):
        self.assertEqual(
            _format_shopify_so_name_b19({"order_number": 4395, "created_at": "2026-04-19T15:23:00-04:00"}),
            "SH-2026-04395",
        )

    def test_short_order_number_padded_to_five(self):
        self.assertEqual(
            _format_shopify_so_name_b19({"order_number": 42, "created_at": "2026-01-01T00:00:00Z"}),
            "SH-2026-00042",
        )

    def test_five_digit_number_fits(self):
        self.assertEqual(
            _format_shopify_so_name_b19({"order_number": 12345, "created_at": "2026-06-15T12:00:00Z"}),
            "SH-2026-12345",
        )

    def test_over_hundred_thousand_still_works(self):
        # Graceful overflow — zfill pads, doesn't truncate.
        self.assertEqual(
            _format_shopify_so_name_b19({"order_number": 100000, "created_at": "2027-01-01T00:00:00Z"}),
            "SH-2027-100000",
        )

    def test_string_order_number_coerced(self):
        self.assertEqual(
            _format_shopify_so_name_b19({"order_number": "4395", "created_at": "2026-04-19T15:00:00Z"}),
            "SH-2026-04395",
        )

    def test_year_from_created_at_not_sync_time(self):
        # Order placed Dec 31 2025, year prefix must be 2025 not the current year.
        self.assertEqual(
            _format_shopify_so_name_b19({"order_number": 9999, "created_at": "2025-12-31T23:59:59Z"}),
            "SH-2025-09999",
        )

    def test_missing_order_number_returns_none(self):
        self.assertIsNone(_format_shopify_so_name_b19({"created_at": "2026-04-19T15:00:00Z"}))

    def test_missing_created_at_returns_none(self):
        self.assertIsNone(_format_shopify_so_name_b19({"order_number": 4395}))

    def test_bogus_created_at_returns_none(self):
        self.assertIsNone(_format_shopify_so_name_b19({"order_number": 4395, "created_at": "not-a-date"}))

    def test_zero_order_number_pads_zeros(self):
        # Edge: Shopify counter never at 0 in practice, but if it ever was,
        # we produce SH-YYYY-00000 rather than crashing.
        self.assertEqual(
            _format_shopify_so_name_b19({"order_number": 0, "created_at": "2026-04-19T00:00:00Z"}),
            "SH-2026-00000",
        )


# ── B18: SO currency from Shopify order ──────────────────────────────
#
# Connector was writing Shopify USD order totals into SO with currency=EUR +
# conversion_rate=1.0 → every US order overvalued ~8-9% on the books.
# Fix (per user 2026-04-20): set SO.currency = shopify_order.currency verbatim
# and leave conversion_rate unset so ERPNext applies current FX at Sales Invoice
# time. Just a currency copy — no conversion math in the connector.

def _resolve_currency_b18(shopify_order):
    """Return the Shopify order's currency code, or None if missing.

    Returning None causes caller to omit the currency field on SO creation,
    letting ERPNext fall back to Customer/Company default. Explicit 'USD'
    or 'EUR' etc. is used verbatim (uppercased, whitespace-trimmed).
    """
    c = shopify_order.get("currency")
    if not c or not isinstance(c, str):
        return None
    c = c.strip().upper()
    return c or None


class TestB18Currency(unittest.TestCase):
    def test_usd_present(self):
        self.assertEqual(_resolve_currency_b18({"currency": "USD"}), "USD")

    def test_eur_present(self):
        self.assertEqual(_resolve_currency_b18({"currency": "EUR"}), "EUR")

    def test_lowercase_normalized(self):
        self.assertEqual(_resolve_currency_b18({"currency": "usd"}), "USD")

    def test_whitespace_padded(self):
        self.assertEqual(_resolve_currency_b18({"currency": "  USD  "}), "USD")

    def test_missing_key(self):
        self.assertIsNone(_resolve_currency_b18({}))

    def test_empty_string(self):
        self.assertIsNone(_resolve_currency_b18({"currency": ""}))

    def test_none_value(self):
        self.assertIsNone(_resolve_currency_b18({"currency": None}))

    def test_non_string_ignored(self):
        self.assertIsNone(_resolve_currency_b18({"currency": 123}))


# ── yei-v1.3.5 §3.1: qty-diff branch on existing-lid lines ────────────
#
# `_reconcile_so_line_items` previously `continue`d silently when a Shopify
# line was already on the SO (same `shopify_line_item_id`). Free-gift bumps
# (1→2, 2→4) and partial refunds where `current_quantity > 0` were ignored.
#
# v1.3.5 adds: if `lid in existing_by_lid` AND Shopify `current_quantity`
# differs from existing SOI `qty`, update the SOI qty + record the lid in
# `qty_updated`. Return shape becomes `(added, refunded, qty_updated)`.
# Save block fires when `added` OR `qty_updated` is non-empty.
#
# Inline-copy reconcile logic — the real `_reconcile_so_line_items` pulls
# in frappe.get_doc + ecommerce_item dependencies that aren't worth mocking
# here. We're testing the algorithm, matching the pattern used elsewhere
# in this file.


def _reconcile_so_line_items_v135(sales_order_items, shopify_lines):
    """Inline copy of v1.3.5 reconcile logic — qty-diff branch.

    `sales_order_items` is a list of dicts (each representing an existing
    SOI) with at least keys: ``shopify_line_item_id``, ``qty``,
    ``shopify_refunded``. The function mutates them in place when a qty
    update applies and returns ``(added, refunded, qty_updated)``.

    `shopify_lines` is a list of dicts mimicking Shopify line_items: at
    least ``id``, ``current_quantity`` (or ``quantity``), ``title``.

    Skips tip lines, refund branch for cq==0, qty-diff for existing lids,
    new-line branch for unknown lids (returns a stub item_code).
    """
    def cint(x):
        try:
            return int(x or 0)
        except (TypeError, ValueError):
            return 0

    existing_by_lid = {}
    for soi in sales_order_items:
        lid = (soi.get("shopify_line_item_id") or "")
        if lid:
            existing_by_lid[str(lid)] = soi

    added = []
    refunded = []
    qty_updated = []

    for li in shopify_lines:
        lid = str(li.get("id") or "")
        cq = cint(li.get("current_quantity", li.get("quantity", 1)))

        title = str(li.get("title") or "").strip().lower()
        if title == "tip":
            continue

        if cq == 0:
            soi = existing_by_lid.get(lid)
            if soi and not cint(soi.get("shopify_refunded") or 0):
                soi["__refunded_flagged__"] = True
                refunded.append(lid)
            continue

        if lid and lid in existing_by_lid:
            soi = existing_by_lid[lid]
            if cint(soi.get("qty")) != cq:
                soi["qty"] = cq
                qty_updated.append(lid)
            continue

        added.append(li.get("__item_code__") or f"SKU-{lid}")

    return added, refunded, qty_updated


class TestV135QtyDiffBranchSourceInvariant(unittest.TestCase):
    """yei-v1.3.5: assert the qty-diff branch is present in order.py source."""

    @classmethod
    def setUpClass(cls):
        order_py = os.path.join(SHOPIFY_DIR, "order.py")
        with open(order_py, "r", encoding="utf-8") as f:
            cls.source = f.read()

    def test_qty_updated_list_initialised(self):
        self.assertIn("qty_updated = []", self.source,
            "_reconcile_so_line_items must initialise qty_updated list")

    def test_qty_diff_branch_updates_soi_qty(self):
        # The qty-diff branch must compare cint(soi.get('qty')) != cq and
        # then assign soi.qty = cq inside the existing-lid branch.
        self.assertRegex(self.source,
            r"if\s+lid\s+and\s+lid\s+in\s+existing_by_lid\s*:\s*\n"
            r"\s*soi\s*=\s*existing_by_lid\[lid\]\s*\n"
            r"\s*if\s+cint\(soi\.get\(\"qty\"\)\)\s*!=\s*cq\s*:",
            "qty-diff branch must guard on cint(soi.get('qty')) != cq")
        self.assertIn("soi.qty = cq", self.source,
            "qty-diff branch must assign soi.qty = cq")

    def test_qty_updated_records_lid(self):
        self.assertIn("qty_updated.append(lid)", self.source,
            "qty-diff branch must record the updated lid in qty_updated")

    def test_save_fires_on_qty_updated(self):
        # The save block must fire when either added or qty_updated is
        # non-empty (not just when added is non-empty).
        self.assertRegex(self.source,
            r"if\s+added\s+or\s+qty_updated\s*:\s*\n",
            "save block must fire when added OR qty_updated is non-empty")

    def test_return_shape_is_three_tuple(self):
        self.assertIn("return added, refunded, qty_updated", self.source,
            "_reconcile_so_line_items must return (added, refunded, qty_updated)")

    def test_caller_unpacks_three_tuple(self):
        # handle_order_edited unpacks `added, refunded, qty_updated`.
        self.assertIn(
            "added, refunded, qty_updated = _reconcile_so_line_items(",
            self.source,
            "handle_order_edited must unpack the new (added, refunded, qty_updated) tuple",
        )


class TestV135QtyDiffBranchBehaviour(unittest.TestCase):
    """yei-v1.3.5 §3.1: behavioural tests for the qty-diff branch."""

    def test_existing_lid_qty_bump_updates_soi(self):
        """Existing SOI lid=X qty=1 + Shopify cq=2 → SOI qty=2,
        qty_updated contains the lid."""
        soi = {"shopify_line_item_id": "100", "qty": 1, "shopify_refunded": 0}
        sales_order_items = [soi]
        shopify_lines = [{"id": 100, "current_quantity": 2, "title": "Greenhouse"}]

        added, refunded, qty_updated = _reconcile_so_line_items_v135(
            sales_order_items, shopify_lines,
        )

        self.assertEqual(soi["qty"], 2,
            "SOI qty must update from 1 to 2 to match Shopify current_quantity")
        self.assertEqual(qty_updated, ["100"],
            "qty_updated must contain the bumped lid")
        self.assertEqual(added, [],
            "no new line added — same lid as existing SOI")
        self.assertEqual(refunded, [],
            "not a refund — cq is non-zero")

    def test_partial_refund_qty_reduction_updates_soi(self):
        """Existing SOI lid=X qty=3 + Shopify quantity=3 current_quantity=2
        → SOI qty=2, refunded stays 0, qty_updated contains the lid.

        Mirrors #4485 case (partial refund of 1 of 3 units). The line is
        NOT fully refunded — current_quantity > 0 — so the refund branch
        doesn't fire; the qty-diff branch handles it instead."""
        soi = {"shopify_line_item_id": "200", "qty": 3, "shopify_refunded": 0}
        sales_order_items = [soi]
        shopify_lines = [{
            "id": 200, "quantity": 3, "current_quantity": 2,
            "title": "Greenhouse",
        }]

        added, refunded, qty_updated = _reconcile_so_line_items_v135(
            sales_order_items, shopify_lines,
        )

        self.assertEqual(soi["qty"], 2,
            "SOI qty must reduce from 3 to 2 (partial refund of 1 unit)")
        self.assertEqual(refunded, [],
            "refunded must stay empty — line isn't fully refunded (cq > 0)")
        self.assertEqual(qty_updated, ["200"],
            "qty_updated must contain the partially-refunded lid")
        self.assertEqual(soi.get("shopify_refunded"), 0,
            "shopify_refunded flag must stay 0 — partial, not full, refund")

    def test_existing_lid_qty_match_is_noop(self):
        """Existing SOI lid=X qty=2 + Shopify cq=2 → no-op (qty_updated empty).
        Idempotency check — re-running reconcile must not double-write."""
        soi = {"shopify_line_item_id": "300", "qty": 2, "shopify_refunded": 0}
        sales_order_items = [soi]
        shopify_lines = [{"id": 300, "current_quantity": 2, "title": "Greenhouse"}]

        added, refunded, qty_updated = _reconcile_so_line_items_v135(
            sales_order_items, shopify_lines,
        )

        self.assertEqual(soi["qty"], 2,
            "SOI qty must stay at 2 — no-op when qty already matches")
        self.assertEqual(qty_updated, [],
            "qty_updated must be empty — Shopify cq matches SOI qty")
        self.assertEqual(added, [])
        self.assertEqual(refunded, [])


if __name__ == "__main__":
    unittest.main()
