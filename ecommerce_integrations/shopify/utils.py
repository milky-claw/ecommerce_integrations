# Copyright (c) 2021, Frappe and contributors
# For license information, please see LICENSE

import re

import frappe
from frappe import _, _dict
from frappe.utils import cstr, validate_phone_number

from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_integration_log.ecommerce_integration_log import (
	create_log,
)
from ecommerce_integrations.shopify.constants import (
	MODULE_NAME,
	OLD_SETTINGS_DOCTYPE,
	SETTING_DOCTYPE,
)


# yei-v1.4.5 phone-overflow helpers
# ---------------------------------
# Shopify customers sometimes type phone strings that Frappe's
# ``validate_phone_number`` regex rejects (extensions, concatenated
# alternates). Pre-v1.4.5 the connector silently dropped these phones —
# ``Address.phone`` stayed empty and the courier had no number to call.
# v1.4.5 splits the raw string into a clean validator-passing portion
# (Address.phone) and preserves the FULL raw string in
# ``Address.address_line2`` as ``"<addr2> | Ph: <raw>"`` so couriers
# see complete context on the printed label. User-approved 2026-05-15:
# the phone may appear twice on the label (once in phone field, once
# in line2) — by-design.

# Phone-shaped token regex. Captures contiguous digit-rich runs (with
# common separators) up to 18 chars + the leading optional ``+``.
# Greedy-then-trim: longest match found, then trimmed from the right
# until ``validate_phone_number`` accepts. This handles both:
#   ``+1 415-419-8616 ext. 67911`` → trim "ext. 67911" via letter break →
#       leading match ``+1 415-419-8616`` validates as-is.
#   ``+1 6026716610 99999 8031791701`` → first regex match is
#       ``+1 6026716610 99999`` (19 chars, validator-passing); trim the
#       trailing space-separated token → ``+1 6026716610`` (13 chars).
_PHONE_VALID_SUBSTR = re.compile(r"(\+?\d[\d\s\-\(\)\.,#\*]{8,18})")


_MIN_PHONE_DIGITS = 7  # local-format US phone is 7 digits; everything ≥7 acceptable


def _digit_count(s):
	return sum(1 for c in s if c.isdigit())


def split_phone_overflow(raw_phone):
	"""Return ``(clean_phone, has_overflow)``.

	``has_overflow == True`` means raw failed Frappe's validator and we
	extracted a sub-portion (or nothing extractable but raw is non-empty).
	``has_overflow == False`` means raw passed as-is, or was empty/None.

	When the raw string contains multiple space-separated phone numbers
	(e.g. ``"+1 6026716610 99999 8031791701"``), the leading number is
	returned. After the greedy regex match, the trailing whitespace-
	separated token is dropped while: (a) the dropped token is itself
	all-digits (i.e. looks like a second phone, not part of an
	international-format leader like ``+1 415-...``), (b) the remaining
	prefix still validates, and (c) the remaining prefix still carries
	at least ``_MIN_PHONE_DIGITS`` (7) digits.
	"""
	if not raw_phone:
		return ("", False)
	s = cstr(raw_phone).strip()
	if not s:
		return ("", False)
	if validate_phone_number(s, throw=False):
		return (s, False)
	for m in _PHONE_VALID_SUBSTR.finditer(s):
		candidate = m.group(1).strip()
		if not validate_phone_number(candidate, throw=False):
			continue
		# Trim trailing whitespace-separated digit-only tokens (second-phone
		# overflow inside the greedy match). Stops at international-format
		# leaders like "+1 415-..." where the token after the first space
		# contains hyphens (digit_count > 0 but not pure digits).
		while " " in candidate:
			stripped = candidate.rstrip()
			last_space = stripped.rfind(" ")
			if last_space <= 0:
				break
			trailing = stripped[last_space + 1 :]
			prefix = stripped[:last_space].rstrip()
			if not trailing.isdigit():
				break  # mixed token (e.g. "415-419-8616") — keep it
			if not validate_phone_number(prefix, throw=False):
				break
			if _digit_count(prefix) < _MIN_PHONE_DIGITS:
				break
			candidate = prefix
		return (candidate, True)
	return ("", True)  # nothing extractable, but raw is non-empty


def compute_line2_with_overflow(shopify_address2, raw_phone):
	"""Compute final ``Address.address_line2`` from shopify.address2 + raw phone.

	If raw_phone passes the validator: returns ``shopify_address2`` unchanged.
	Otherwise: appends ``" | Ph: <FULL raw phone>"`` — preserves ALL phone
	content so the courier sees the extension/alternate numbers on the
	printed label. The phone may appear twice on the label (once in the
	phone field, once embedded in line2) — by-design (user-approved
	2026-05-15: "courier might not understand otherwise").

	Idempotent overwrite: always derives from current Shopify state.
	Never append-merges with existing ``Address.address_line2``.
	"""
	_clean, has_overflow = split_phone_overflow(raw_phone)
	base = cstr(shopify_address2 or "").strip()
	if not has_overflow:
		return base
	raw_full = cstr(raw_phone or "").strip()
	if not raw_full:
		return base
	if base:
		return f"{base} | Ph: {raw_full}"
	return f"Ph: {raw_full}"


def create_shopify_log(**kwargs):
	return create_log(module_def=MODULE_NAME, **kwargs)


def migrate_from_old_connector(payload=None, request_id=None):
	"""This function is called to migrate data from old connector to new connector."""

	if request_id:
		log = frappe.get_doc("Ecommerce Integration Log", request_id)
	else:
		log = create_shopify_log(
			status="Queued",
			method="ecommerce_integrations.shopify.utils.migrate_from_old_connector",
		)

	frappe.enqueue(
		method=_migrate_items_to_ecommerce_item,
		queue="long",
		is_async=True,
		log=log,
	)


def ensure_old_connector_is_disabled():
	try:
		old_setting = frappe.get_doc(OLD_SETTINGS_DOCTYPE)
	except Exception:
		frappe.clear_last_message()
		return

	if old_setting.enable_shopify:
		link = frappe.utils.get_link_to_form(OLD_SETTINGS_DOCTYPE, OLD_SETTINGS_DOCTYPE)
		msg = _("Please disable old Shopify integration from {0} to proceed.").format(link)
		frappe.throw(msg)


def _migrate_items_to_ecommerce_item(log):
	shopify_fields = ["shopify_product_id", "shopify_variant_id"]

	for field in shopify_fields:
		if not frappe.db.exists({"doctype": "Custom Field", "fieldname": field}):
			return

	items = _get_items_to_migrate()

	try:
		_create_ecommerce_items(items)
	except Exception:
		log.status = "Error"
		log.traceback = frappe.get_traceback()
		log.save()
		return

	frappe.db.set_value(SETTING_DOCTYPE, SETTING_DOCTYPE, "is_old_data_migrated", 1)
	log.status = "Success"
	log.save()


def _get_items_to_migrate() -> list[_dict]:
	"""get all list of items that have shopify fields but do not have associated ecommerce item."""

	old_data = frappe.db.sql(
		"""SELECT item.name as erpnext_item_code, shopify_product_id, shopify_variant_id, item.variant_of, item.has_variants
			FROM tabItem item
			LEFT JOIN `tabEcommerce Item` ei on ei.erpnext_item_code = item.name
			WHERE ei.erpnext_item_code IS NULL AND shopify_product_id IS NOT NULL""",
		as_dict=True,
	)

	return old_data or []


def _create_ecommerce_items(items: list[_dict]) -> None:
	for item in items:
		if not all((item.erpnext_item_code, item.shopify_product_id, item.shopify_variant_id)):
			continue

		ecommerce_item = frappe.get_doc(
			{
				"doctype": "Ecommerce Item",
				"integration": MODULE_NAME,
				"erpnext_item_code": item.erpnext_item_code,
				"integration_item_code": item.shopify_product_id,
				"variant_id": item.shopify_variant_id,
				"variant_of": item.variant_of,
				"has_variants": item.has_variants,
			}
		)
		ecommerce_item.save()
