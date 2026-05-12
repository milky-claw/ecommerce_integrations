# Copyright (c) 2021, Frappe and contributors
# For license information, please see LICENSE

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import get_datetime
from shopify.collection import PaginatedIterator
from shopify.resources import Location

from ecommerce_integrations.controllers.setting import (
	ERPNextWarehouse,
	IntegrationWarehouse,
	SettingController,
)
from ecommerce_integrations.shopify import connection
from ecommerce_integrations.shopify.constants import (
	ADDRESS_ID_FIELD,
	CURRENT_SUBTOTAL_PRICE_FIELD,
	CURRENT_TOTAL_DISCOUNTS_FIELD,
	CURRENT_TOTAL_PRICE_FIELD,
	CUSTOMER_ID_FIELD,
	FREIGHT_CLASS_FIELD,
	FULLFILLMENT_ID_FIELD,
	ITEM_METAFIELDS_FIELD,
	ITEM_REFUNDED_AT_FIELD,
	ITEM_REFUNDED_FIELD,
	ITEM_TAGS_FIELD,
	ORDER_DISCOUNT_CODES_FIELD,
	ORDER_FINANCIAL_STATUS_FIELD,
	ORDER_FULFILLMENT_STATUS_FIELD,
	ORDER_ID_FIELD,
	ORDER_ITEM_PROPERTIES_FIELD,
	ORDER_ITEM_SHIPPING_METHOD_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	ORDER_TIP_AMOUNT_FIELD,
	SUPPLIER_ID_FIELD,
)
from ecommerce_integrations.shopify.oauth import validate_oauth_credentials
from ecommerce_integrations.shopify.utils import (
	ensure_old_connector_is_disabled,
	migrate_from_old_connector,
)


class ShopifySetting(SettingController):
	def is_enabled(self) -> bool:
		return bool(self.enable_shopify)

	def _get_password_safe(self, fieldname: str) -> str:
		"""
		Safely get password field value without raising exceptions.
		Returns empty string if password doesn't exist or document is new.
		"""
		try:
			# Check if document is saved
			if not self.name or self.is_new():
				return ""

			password = self.get_password(fieldname, raise_exception=False)
			return password if password else ""
		except Exception:
			return ""

	def validate(self):
		ensure_old_connector_is_disabled()

		if self.shopify_url:
			self.shopify_url = self.shopify_url.replace("https://", "").replace("http://", "")

		self._set_default_authentication_method()
		self._validate_authentication_fields()
		self._validate_oauth_credentials_if_needed()
		self._handle_webhooks()
		self._validate_warehouse_links()
		self._initalize_default_values()

		if self.is_enabled():
			setup_custom_fields()

	def on_update(self):
		if self.is_enabled() and not self.is_old_data_migrated:
			migrate_from_old_connector()

	def _set_default_authentication_method(self):
		"""Set default authentication method for existing documents."""
		if not self.authentication_method:
			self.authentication_method = "Static Token"

	def _validate_authentication_fields(self):
		"""Validate that required fields are present based on authentication method."""
		if not self.is_enabled():
			return

		if self.authentication_method == "Static Token":
			# Check password field exists and has value
			password = self._get_password_safe("password")
			if not password:
				frappe.throw(_("Password / Access Token is required for Static Token authentication"))

			if not self.shared_secret:
				frappe.throw(_("Shared secret / API Secret is required for Static Token authentication"))

		elif self.authentication_method == "OAuth 2.0 Client Credentials":
			if not self.client_id:
				frappe.throw(_("Client ID is required for OAuth 2.0 authentication"))

			# Check client_secret field exists and has value
			client_secret = self._get_password_safe("client_secret")
			if not client_secret:
				frappe.throw(_("Client Secret is required for OAuth 2.0 authentication"))

	def _validate_oauth_credentials_if_needed(self):
		"""Validate OAuth credentials by generating a test token if credentials changed."""
		if not self.is_enabled():
			return

		if self.authentication_method != "OAuth 2.0 Client Credentials":
			return

		# Check if OAuth credentials have changed
		if self.has_value_changed("client_id") or self.has_value_changed("client_secret"):
			client_secret = self._get_password_safe("client_secret")
			if not client_secret:
				return  # Will be caught by _validate_authentication_fields

			try:
				# Validate credentials by attempting to generate a token
				validate_oauth_credentials(
					self.shopify_url,
					self.client_id,
					client_secret,
				)
				frappe.msgprint(
					_("OAuth credentials validated successfully. Token will be auto-generated on save."),
					indicator="green",
					alert=True,
				)
			except Exception:
				# Error is already logged by validate_oauth_credentials
				raise

	def before_save(self):
		"""Optional: Pre-generate OAuth token for better UX."""
		if not self.is_enabled():
			return

		if self.authentication_method == "OAuth 2.0 Client Credentials":
			# Pre-generate token if credentials are new or changed
			# This is optional - token will be generated on-demand if not done here
			current_token = self._get_password_safe("oauth_access_token")

			if (
				self.has_value_changed("client_id")
				or self.has_value_changed("client_secret")
				or not current_token
			):
				try:
					self._get_or_generate_oauth_token()
				except Exception:
					# Don't block save, token will be generated on-demand when needed
					pass

	def _handle_webhooks(self):
		"""Handle webhook registration/unregistration. Uses appropriate token based on auth method."""
		# Also check DB for webhooks in case child table wasn't loaded (e.g. set_value API)
		if self.is_enabled() and not self.webhooks:
			db_webhooks = frappe.get_all(
				"Shopify Webhooks", filters={"parent": self.name}, fields=["name"], limit=1
			)
			if db_webhooks:
				self.reload()
				return
		if self.is_enabled() and not self.webhooks:
			# Get the appropriate password/token for webhook registration
			if self.authentication_method == "OAuth 2.0 Client Credentials":
				# For OAuth, get or generate a valid token
				password = self._get_or_generate_oauth_token()
			else:
				# For Static Token, use the password field
				password = self.get_password("password")

			new_webhooks = connection.register_webhooks(self.shopify_url, password)

			if not new_webhooks:
				msg = _("Failed to register webhooks with Shopify.") + "<br>"
				msg += _("Please check credentials and retry.") + " "
				msg += _("Disabling and re-enabling the integration might also help.")
				frappe.throw(msg)

			for webhook in new_webhooks:
				self.append("webhooks", {"webhook_id": webhook.id, "method": webhook.topic})

		elif not self.is_enabled():
			# Get the appropriate password/token for webhook unregistration
			if self.authentication_method == "OAuth 2.0 Client Credentials":
				password = self._get_password_safe("oauth_access_token")
			else:
				password = self._get_password_safe("password")

			if password:  # Only unregister if we have a password
				connection.unregister_webhooks(self.shopify_url, password)

			self.webhooks = list()  # remove all webhooks

	def _get_or_generate_oauth_token(self) -> str:
		"""
		Get OAuth token if valid, or generate a new one if expired/missing.
		This ensures we always have a valid token when needed.
		"""
		from ecommerce_integrations.shopify.oauth import is_token_valid, refresh_oauth_token

		# Check if we have a valid token
		current_token = self._get_password_safe("oauth_access_token")
		token_expiry = self.token_expires_at

		# If token exists and is valid, return it
		if current_token and is_token_valid(token_expiry):
			return current_token

		# Token is missing, expired, or expiring soon - generate new one
		try:
			# Generate new token and save it
			new_token = refresh_oauth_token(self)
			return new_token
		except Exception as e:
			frappe.throw(
				_("Failed to generate OAuth token: {0}").format(str(e)),
				title=_("OAuth Authentication Error"),
			)

	def _validate_warehouse_links(self):
		for wh_map in self.shopify_warehouse_mapping:
			if not wh_map.erpnext_warehouse:
				frappe.throw(_("ERPNext warehouse required in warehouse map table."))

	def _initalize_default_values(self):
		if not self.last_inventory_sync:
			self.last_inventory_sync = get_datetime("1970-01-01")

	@frappe.whitelist()
	@connection.temp_shopify_session
	def update_location_table(self):
		"""Fetch locations from shopify and add it to child table so user can
		map it with correct ERPNext warehouse."""

		self.shopify_warehouse_mapping = []
		for locations in PaginatedIterator(Location.find()):
			for location in locations:
				self.append(
					"shopify_warehouse_mapping",
					{"shopify_location_id": location.id, "shopify_location_name": location.name},
				)

	def get_erpnext_warehouses(self) -> list[ERPNextWarehouse]:
		return [wh_map.erpnext_warehouse for wh_map in self.shopify_warehouse_mapping]

	def get_erpnext_to_integration_wh_mapping(self) -> dict[ERPNextWarehouse, IntegrationWarehouse]:
		return {
			wh_map.erpnext_warehouse: wh_map.shopify_location_id for wh_map in self.shopify_warehouse_mapping
		}

	def get_integration_to_erpnext_wh_mapping(self) -> dict[IntegrationWarehouse, ERPNextWarehouse]:
		return {
			wh_map.shopify_location_id: wh_map.erpnext_warehouse for wh_map in self.shopify_warehouse_mapping
		}


def setup_custom_fields():
	custom_fields = {
		# B21: shopify_selling_rate retired — use native Item.standard_rate.
		"Item": [
			dict(
				fieldname=ITEM_TAGS_FIELD,
				label="Shopify Tags",
				fieldtype="Small Text",
				insert_after="standard_rate",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ITEM_METAFIELDS_FIELD,
				label="Shopify Metafields",
				fieldtype="Long Text",
				insert_after=ITEM_TAGS_FIELD,
				read_only=1,
				print_hide=1,
			),
		],
		"Customer": [
			dict(
				fieldname=CUSTOMER_ID_FIELD,
				label="Shopify Customer Id",
				fieldtype="Data",
				insert_after="series",
				read_only=1,
				print_hide=1,
			)
		],
		"Supplier": [
			dict(
				fieldname=SUPPLIER_ID_FIELD,
				label="Shopify Supplier Id",
				fieldtype="Data",
				insert_after="supplier_name",
				read_only=1,
				print_hide=1,
			)
		],
		"Address": [
			dict(
				fieldname=ADDRESS_ID_FIELD,
				label="Shopify Address Id",
				fieldtype="Data",
				insert_after="fax",
				read_only=1,
				print_hide=1,
			)
		],
		"Sales Order": [
			dict(
				fieldname=ORDER_ID_FIELD,
				label="Shopify Order Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_NUMBER_FIELD,
				label="Shopify Order Number",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_STATUS_FIELD,
				label="Shopify Order Status",
				fieldtype="Small Text",
				insert_after=ORDER_NUMBER_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_FINANCIAL_STATUS_FIELD,
				label="Shopify Financial Status",
				fieldtype="Data",
				insert_after=ORDER_STATUS_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_FULFILLMENT_STATUS_FIELD,
				label="Shopify Fulfillment Status",
				fieldtype="Data",
				insert_after=ORDER_FINANCIAL_STATUS_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_DISCOUNT_CODES_FIELD,
				label="Shopify Discount Codes",
				fieldtype="Small Text",
				insert_after=ORDER_FULFILLMENT_STATUS_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_TIP_AMOUNT_FIELD,
				label="Shopify Tip Amount",
				fieldtype="Currency",
				insert_after=ORDER_DISCOUNT_CODES_FIELD,
				read_only=1,
				print_hide=1,
			),
			# B23: SO-level rollup of per-line freight classes.
			# `split` indicates the SO has both ship-air and ship-sea
			# lines; the DN-split grouper materializes one DN per
			# (manufacturer, freight_class) tuple in that case.
			# allow_on_submit=1 — backfill + recompute_for_so write
			# this on already-submitted SOs, and value can change if
			# Shopify product gets retagged.
			dict(
				fieldname=FREIGHT_CLASS_FIELD,
				label="Shopify Freight Class",
				fieldtype="Select",
				options="\nair\nsea\ndropship\nsplit",
				insert_after=ORDER_TIP_AMOUNT_FIELD,
				read_only=1,
				print_hide=1,
				allow_on_submit=1,
			),
			# B24c: Shopify's current_* running totals — drift indicators
			# visible on the SO form once refunds land. No native ERPNext
			# equivalent (grand_total is the original total, not the
			# post-refund total). Read-only; populated by sync_sales_order,
			# handle_order_edited, handle_refund_created, backfill.
			dict(
				fieldname=CURRENT_SUBTOTAL_PRICE_FIELD,
				label="Shopify Current Subtotal",
				fieldtype="Currency",
				options="currency",
				insert_after=ORDER_FINANCIAL_STATUS_FIELD,
				read_only=1,
				print_hide=1,
				allow_on_submit=1,
			),
			dict(
				fieldname=CURRENT_TOTAL_PRICE_FIELD,
				label="Shopify Current Total",
				fieldtype="Currency",
				options="currency",
				insert_after=CURRENT_SUBTOTAL_PRICE_FIELD,
				read_only=1,
				print_hide=1,
				allow_on_submit=1,
			),
			dict(
				fieldname=CURRENT_TOTAL_DISCOUNTS_FIELD,
				label="Shopify Current Discounts",
				fieldtype="Currency",
				options="currency",
				insert_after=CURRENT_TOTAL_PRICE_FIELD,
				read_only=1,
				print_hide=1,
				allow_on_submit=1,
			),
		],
		# B21: shopify_item_discount retired — B15's native price_list_rate
		# + rate = effective_rate (dollar-amount model) is the discount
		# source of truth; the snapshot field was redundant.
		"Sales Order Item": [
			dict(
				fieldname=ORDER_ITEM_PROPERTIES_FIELD,
				label="Shopify Line Item Properties",
				fieldtype="Long Text",
				insert_after="discount_and_margin",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_ITEM_SHIPPING_METHOD_FIELD,
				label="Shopify Shipping Method",
				fieldtype="Data",
				insert_after=ORDER_ITEM_PROPERTIES_FIELD,
				read_only=1,
				print_hide=1,
			),
			# B23: per-line freight class derived live from Shopify product
			# tags at sync time. Read by ygf split.py grouper to materialize
			# DNs per (manufacturer, freight_class). allow_on_submit=1 —
			# same rationale as the SO-level field; mass backfill writes
			# on already-submitted SOs.
			dict(
				fieldname=FREIGHT_CLASS_FIELD,
				label="Shopify Freight Class",
				fieldtype="Select",
				options="\nair\nsea\ndropship",
				insert_after=ORDER_ITEM_SHIPPING_METHOD_FIELD,
				read_only=1,
				print_hide=1,
				allow_on_submit=1,
			),
			# B24a: per-line refund flag — drives Stage 04d supplier-sheet
			# CANCELLED prefix and is the canonical "this line was refunded"
			# signal. refunded_at carries the Shopify refunds[].created_at
			# moment (used by ygf for the CANCELLED-prefix date). Both
			# allow_on_submit=1 because refunds arrive after SO submit.
			dict(
				fieldname=ITEM_REFUNDED_FIELD,
				label="Refunded",
				fieldtype="Check",
				default=0,
				insert_after=FREIGHT_CLASS_FIELD,
				read_only=1,
				print_hide=1,
				allow_on_submit=1,
			),
			dict(
				fieldname=ITEM_REFUNDED_AT_FIELD,
				label="Refunded At",
				fieldtype="Datetime",
				insert_after=ITEM_REFUNDED_FIELD,
				read_only=1,
				print_hide=1,
				allow_on_submit=1,
				description=(
					"Set from refunds[].created_at on webhook; used by "
					"Stage 04d (ygf) for the CANCELLED-prefix date."
				),
			),
		],
		# B24a: yei now installs Custom Fields on DN Item (previously only
		# DN itself was touched). Mirror of SO Item refund flag — apply_refund
		# propagates SO Item flag → DN Item flag when a DN exists. ygf reads
		# this flag on every supplier-sheet cron run.
		"Delivery Note Item": [
			dict(
				fieldname=ITEM_REFUNDED_FIELD,
				label="Refunded",
				fieldtype="Check",
				default=0,
				insert_after="against_sales_order",
				read_only=1,
				print_hide=1,
				allow_on_submit=1,
			),
			dict(
				fieldname=ITEM_REFUNDED_AT_FIELD,
				label="Refunded At",
				fieldtype="Datetime",
				insert_after=ITEM_REFUNDED_FIELD,
				read_only=1,
				print_hide=1,
				allow_on_submit=1,
			),
		],
		"Delivery Note": [
			dict(
				fieldname=ORDER_ID_FIELD,
				label="Shopify Order Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_NUMBER_FIELD,
				label="Shopify Order Number",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_STATUS_FIELD,
				label="Shopify Order Status",
				fieldtype="Small Text",
				insert_after=ORDER_NUMBER_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=FULLFILLMENT_ID_FIELD,
				label="Shopify Fulfillment Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
		],
		"Sales Invoice": [
			dict(
				fieldname=ORDER_ID_FIELD,
				label="Shopify Order Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_NUMBER_FIELD,
				label="Shopify Order Number",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_STATUS_FIELD,
				label="Shopify Order Status",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
		],
	}

	create_custom_fields(custom_fields)
