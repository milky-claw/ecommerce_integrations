"""B23: Shopify product-tag → freight class resolver.

Module is pure-importable in test environments (no Frappe). The
``@frappe.whitelist`` decorator on ``recompute_for_so`` falls back to
a no-op when ``frappe`` isn't on sys.path so unit tests can still
import the module.

Maps line items to a freight class derived from the Shopify product's
tags. Used at order sync time (real-time webhook + manual paths) to
stamp ``Sales Order Item.shopify_freight_class`` per line and
``Sales Order.shopify_freight_class`` as the order rollup.

Distinct from the older B7 ``_resolve_shipping_method``: that helper
reads ``Item.shopify_tags`` (stale on Items not linked via ``Ecommerce
Item``), this resolver reads tags **live from Shopify per product** so
it works regardless of yei sync linkage state.

Tag → class mapping (first match wins per line):

    ship-air      → "air"
    ship-sea      → "sea"
    ship-dropship → "dropship"
    (none)        → ""

Rollup semantics — see ``rollup_so``.
"""
from __future__ import annotations

try:
	import frappe as _frappe
	_whitelist = _frappe.whitelist
except ImportError:
	# Test environment — frappe not available. Decorator becomes no-op.
	def _whitelist(*args, **kwargs):
		def _wrap(fn):
			return fn
		# Support both `@_whitelist` and `@_whitelist()` forms.
		if args and callable(args[0]):
			return args[0]
		return _wrap


SHIP_TAG_TO_CLASS = {
	"ship-air": "air",
	"ship-sea": "sea",
	"ship-dropship": "dropship",
}


def classify_product_tags(tags) -> str:
	"""Map a Shopify product's tags to one of air/sea/dropship/"".

	First matching ``ship-*`` tag wins (caller controls priority via
	tag ordering on the product). Unknown tags are ignored.

	Accepts ``tags`` as either a list of strings or a comma-separated
	string. Empty / None → "".
	"""
	if not tags:
		return ""
	if isinstance(tags, str):
		tag_iter = (t.strip() for t in tags.split(","))
	else:
		tag_iter = (str(t).strip() for t in tags)
	for t in tag_iter:
		cls = SHIP_TAG_TO_CLASS.get(t.lower())
		if cls:
			return cls
	return ""


def rollup_so(line_classes) -> str:
	"""Compute the SO-level rollup from per-line classes.

	Returns one of: ``air`` | ``sea`` | ``dropship`` | ``split`` | ``""``.

	Rules:
	  * Both ``air`` and ``sea`` present → ``split`` (regardless of dropship)
	  * Single non-empty class → that class
	  * Mixed with dropship + exactly one other class → the non-dropship
	    class (dropship lines skip DN materialization, so for SO-level
	    intent the operational class is what's left)
	  * Only dropship → ``dropship``
	  * All blank → ``""``
	"""
	distinct = {c for c in (line_classes or []) if c}
	if {"air", "sea"}.issubset(distinct):
		return "split"
	non_drop = distinct - {"dropship"}
	if len(non_drop) == 1:
		return non_drop.pop()
	if non_drop:  # defensive — shouldn't reach (covered by split branch)
		return "split"
	if "dropship" in distinct:
		return "dropship"
	return ""


def resolve_for_order(shopify_order, fetcher):
	"""Classify each line in a Shopify order payload.

	Args:
		shopify_order: Shopify order dict (must have ``line_items``).
		fetcher: callable ``(product_id) -> tags`` returning a list or
			comma-string of tags. Caller supplies this so the resolver
			stays pure (testable without Shopify API). In production
			this wraps a per-call cache to dedup multi-line orders that
			repeat the same product.

	Returns:
		(per_line_classes, so_rollup) — list aligned to
		``shopify_order["line_items"]`` and the SO-level rollup string.
	"""
	per_line_classes = []
	for line in shopify_order.get("line_items") or []:
		pid = line.get("product_id")
		if not pid:
			per_line_classes.append("")
			continue
		try:
			tags = fetcher(pid)
		except Exception as e:  # noqa: BLE001 — never crash sync over a tag lookup
			# Wave A observability: previously a silent swallow; bare
			# fetcher failures during Shopify-API hiccups now show up in
			# Error Log so DN-empty-freight-class anomalies are traceable.
			try:
				import frappe as _f
				_f.log_error(
					title="B23 resolve_for_order: tag fetcher failed",
					message=f"product_id={pid}: {type(e).__name__}: {e}",
				)
			except Exception:  # noqa: BLE001 — log path must never crash sync
				pass
			tags = []
		per_line_classes.append(classify_product_tags(tags))
	return per_line_classes, rollup_so(per_line_classes)


@_whitelist()
def recompute_for_so(so_name, fetcher=None):
	"""Recompute & write shopify_freight_class for an existing Sales Order.

	Used for manual recovery (sync went wrong, tags retroactively
	changed) and the workspace backfill script. Re-fetches the Shopify
	order via its stored ``shopify_order_id`` so per-line product_ids
	come from the original payload — independent of Ecommerce Item
	linkage state (which is incomplete for many legacy Items).

	Args:
		so_name: Sales Order name (e.g. ``SH-2026-04837``).
		fetcher: optional product-tags fetcher override (testing).
			Defaults to a fresh ``make_live_fetcher()`` per call.

	Returns:
		dict with new per-line classes + rollup, or ``None`` if SO
		doesn't exist or has no shopify_order_id.
	"""
	import frappe  # local — keep this module pure-importable

	if not frappe.db.exists("Sales Order", so_name):
		return None
	so = frappe.get_doc("Sales Order", so_name)
	shopify_order_id = so.get("shopify_order_id")
	if not shopify_order_id:
		return None
	if fetcher is None:
		fetcher = make_live_fetcher()

	# Pull the original Shopify order to get line_items[].product_id —
	# the only authoritative source. SO Items don't store product_id.
	from shopify.resources import Order

	try:
		shopify_order = Order.find(shopify_order_id)
		line_items = shopify_order.attributes.get("line_items") or []
		# Convert to plain dicts (PaginatedIterator returns Resource objects)
		line_payload = [
			{"product_id": getattr(li, "product_id", None) or
				(li.attributes.get("product_id") if hasattr(li, "attributes") else None)}
			for li in line_items
		]
	except Exception as e:  # noqa: BLE001
		frappe.log_error(
			title="B23 freight_class recompute: Shopify order fetch failed",
			message=f"SO {so_name} (shopify_order_id={shopify_order_id}): {e}",
		)
		return None

	# Filter out tip lines (mirrors B9 behavior in create_sales_order
	# so per-line alignment with SO.items is preserved).
	regular_lines = [
		li for li, raw in zip(line_payload, line_items)
		if str(getattr(raw, "title", "") or "").strip().lower() != "tip"
	]

	per_line, rollup = resolve_for_order({"line_items": regular_lines}, fetcher)
	for idx, row in enumerate(so.items):
		if idx < len(per_line):
			cls = per_line[idx]
			frappe.db.set_value(
				"Sales Order Item", row.name, "shopify_freight_class",
				cls, update_modified=False,
			)
			# Wave A dual-write: new item_ship_method carries the raw
			# Shopify-tag form (air → ship-air, sea → ship-sea, etc.).
			frappe.db.set_value(
				"Sales Order Item", row.name, "item_ship_method",
				f"ship-{cls}" if cls else "", update_modified=False,
			)
	frappe.db.set_value(
		"Sales Order", so_name, "shopify_freight_class",
		rollup, update_modified=False,
	)
	# Wave A dual-write — identity copy at SO header.
	frappe.db.set_value(
		"Sales Order", so_name, "so_ship_class",
		rollup, update_modified=False,
	)
	# Wave A B25 cascade: also propagate to any draft Delivery Notes
	# linked to this SO. Eliminates the historical 7-anomaly bug class
	# where a Shopify retag/recompute updated the SO but left the DN
	# carrying stale freight class. Buckets at the DN level (no "split"
	# or "dropship"); we only push bare air/sea down so the DN's
	# `shopify_freight_class` enum (`""|"air"|"sea"`) stays valid.
	#
	# Source of truth for the DN's class is the bucket assigned at
	# materialization time, which lives on the DN itself. The cascade
	# only touches DRAFT DNs (docstatus=0) for SOs where the bucket
	# matches a bare class on the rollup (air or sea); split-SOs would
	# have multiple DNs (one air, one sea) that already carry the right
	# value from split.py and don't need cascade.
	if rollup in ("air", "sea"):
		draft_dns = frappe.db.sql(
			"""
			SELECT DISTINCT dn.name
			FROM `tabDelivery Note` dn
			JOIN `tabDelivery Note Item` dni ON dni.parent = dn.name
			WHERE dni.against_sales_order = %(so)s AND dn.docstatus = 0
			""",
			{"so": so_name},
			as_dict=True,
		)
		for dn in draft_dns:
			frappe.db.set_value(
				"Delivery Note", dn["name"], "shopify_freight_class",
				rollup, update_modified=False,
			)
			frappe.db.set_value(
				"Delivery Note", dn["name"], "dn_ship_method",
				rollup, update_modified=False,
			)
	return {"per_line": per_line, "rollup": rollup}


def make_live_fetcher():
	"""Return a fetcher ``(product_id) -> list[str]`` that hits Shopify.

	Caches per-call to avoid duplicate API hits when one order has
	multiple lines from the same product. Caller should construct a
	fresh fetcher per order (or per sync run) so the cache lifetime
	matches the request boundary.

	Tags returned as a list of stripped strings (empty list on
	missing/error).
	"""
	cache = {}

	def fetch(product_id):
		key = str(product_id)
		if key in cache:
			return cache[key]
		# Local import — shopify SDK isn't always available at module
		# import time (test environments mock it). Defer.
		from shopify.resources import Product

		try:
			product = Product.find(product_id)
			raw = (getattr(product, "tags", None) or "")
			tags = [t.strip() for t in raw.split(",") if t.strip()]
		except Exception as e:  # noqa: BLE001
			# Wave A observability: previously a silent swallow; bare
			# Product.find failures during Shopify-API hiccups now show up
			# in Error Log so DN-empty-freight-class anomalies are traceable.
			try:
				import frappe as _f
				_f.log_error(
					title="B23 make_live_fetcher: Product.find failed",
					message=f"product_id={product_id}: {type(e).__name__}: {e}",
				)
			except Exception:  # noqa: BLE001 — log path must never crash sync
				pass
			tags = []
		cache[key] = tags
		return tags

	return fetch
