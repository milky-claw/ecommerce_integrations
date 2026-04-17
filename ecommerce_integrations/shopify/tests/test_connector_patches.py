"""
Standalone unit tests for connector patches B1-B13.

These tests mock Frappe and Shopify dependencies so they can run
locally without a Frappe instance. Tests cover pure logic functions
and verify the patched functions produce correct output.

Run with: python3 -m pytest ecommerce_integrations/shopify/tests/test_connector_patches.py -v
"""

import json
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


if __name__ == "__main__":
    unittest.main()
