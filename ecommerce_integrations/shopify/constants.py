# Copyright (c) 2021, Frappe and contributors
# For license information, please see LICENSE


MODULE_NAME = "shopify"
SETTING_DOCTYPE = "Shopify Setting"
OLD_SETTINGS_DOCTYPE = "Shopify Settings"

API_VERSION = "2024-01"

WEBHOOK_EVENTS = [
	"orders/create",
	"orders/paid",
	"orders/fulfilled",
	"orders/cancelled",
	"orders/partially_fulfilled",
	"orders/edited",
	# B5: product webhooks so tag changes (ship-sea/ship-air/ship-dropship,
	# warranty, stockv1/v2, etc.) flow to Item.shopify_tags in real time.
	"products/create",
	"products/update",
]

EVENT_MAPPER = {
	"orders/create": "ecommerce_integrations.shopify.order.sync_sales_order",
	"orders/paid": "ecommerce_integrations.shopify.invoice.prepare_sales_invoice",
	"orders/fulfilled": "ecommerce_integrations.shopify.fulfillment.prepare_delivery_note",
	"orders/cancelled": "ecommerce_integrations.shopify.order.cancel_order",
	"orders/partially_fulfilled": "ecommerce_integrations.shopify.fulfillment.prepare_delivery_note",
	"orders/edited": "ecommerce_integrations.shopify.order.handle_order_edited",
	"products/create": "ecommerce_integrations.shopify.product.sync_product_from_webhook",  # B5
	"products/update": "ecommerce_integrations.shopify.product.sync_product_from_webhook",  # B5
}

SHOPIFY_VARIANTS_ATTR_LIST = ["option1", "option2", "option3"]

# custom fields

CUSTOMER_ID_FIELD = "shopify_customer_id"
ORDER_ID_FIELD = "shopify_order_id"
ORDER_NUMBER_FIELD = "shopify_order_number"
ORDER_STATUS_FIELD = "shopify_order_status"
FULLFILLMENT_ID_FIELD = "shopify_fulfillment_id"
SUPPLIER_ID_FIELD = "shopify_supplier_id"
ADDRESS_ID_FIELD = "shopify_address_id"
ORDER_ITEM_DISCOUNT_FIELD = "shopify_item_discount"
ITEM_SELLING_RATE_FIELD = "shopify_selling_rate"

# Custom fields added for YourGreenhouses connector patches
ORDER_DISCOUNT_CODES_FIELD = "shopify_discount_codes"
ORDER_FINANCIAL_STATUS_FIELD = "shopify_financial_status"
ORDER_FULFILLMENT_STATUS_FIELD = "shopify_fulfillment_status"
ORDER_TIP_AMOUNT_FIELD = "shopify_tip_amount"
ORDER_ITEM_PROPERTIES_FIELD = "shopify_line_item_properties"
ORDER_ITEM_SHIPPING_METHOD_FIELD = "shopify_shipping_method"
ORDER_FULFILLMENT_SOURCE_FIELD = "shopify_fulfillment_source"
ITEM_TAGS_FIELD = "shopify_tags"
ITEM_METAFIELDS_FIELD = "shopify_metafields"

UNMATCHED_ITEM_CODE = "MISC-MANUAL"

# ERPNext already defines the default UOMs from Shopify but names are different
WEIGHT_TO_ERPNEXT_UOM_MAP = {"kg": "Kg", "g": "Gram", "oz": "Ounce", "lb": "Pound"}
