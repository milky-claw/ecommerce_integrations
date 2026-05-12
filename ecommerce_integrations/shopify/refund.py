"""B24a: Shopify refund webhook handler — no-amend, flag-only model.

The webhook subscription is wired in ``constants.WEBHOOK_EVENTS`` +
``EVENT_MAPPER``. Refund events are dispatched here.

Design contract: the submitted DN is the immutable production record.
A refund webhook FLAGS the SO Item (+ mirrors to DN Item when a DN
exists) and the ygf supplier-sheet writer marks the supplier row in
place on its next cron run. There is no SO amend, no DN amend, no new
sales-return DN. See:
  stages/04c-data-sync/references/b24-impl-removed-items.md
  stages/04c-data-sync/references/b24-refunds-and-current-quantity.md
  stages/04d-exports/references/supplier-sheet-cancel-mark.md
"""
from __future__ import annotations

import frappe
from frappe.utils import flt

import shopify

from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import (
	CURRENT_SUBTOTAL_PRICE_FIELD,
	CURRENT_TOTAL_DISCOUNTS_FIELD,
	CURRENT_TOTAL_PRICE_FIELD,
	ITEM_REFUNDED_AT_FIELD,
	ITEM_REFUNDED_FIELD,
	ORDER_ID_FIELD,
	SETTING_DOCTYPE,
)


# ── Pure helpers (no Frappe deps — covered by standalone tests) ────


_TODO_TITLES = {
	"no_restock": "Refund credit note (customer keeps item, no stock movement)",
	"return": "Credit note + log inbound stock movement on physical return",
	"cancel": "Refund credit note; if DN submitted, reverse stock entry",
	# legacy_restock is documented as deprecated by Shopify; treat as return.
	"legacy_restock": "Credit note + log inbound stock movement on physical return",
}


def _restock_type_to_todo_title(restock_type: str, has_refund_line_items: bool) -> str:
	"""B24a: map (restock_type, has_refund_line_items) → ToDo title.

	When ``has_refund_line_items`` is False the refund is shipping-only
	(no goods movement) and the title says so regardless of restock_type.
	"""
	if not has_refund_line_items:
		return "Shipping-only refund — credit note"
	return _TODO_TITLES.get(restock_type, _TODO_TITLES["return"])


def _todo_priority(refund_total) -> str:
	"""B24a: ``High`` priority for refund_total ≥ $1,000, else ``Medium``."""
	try:
		amount = float(refund_total or 0)
	except (TypeError, ValueError):
		amount = 0.0
	return "High" if amount >= 1000 else "Medium"


def _sum_refund_amount(refund) -> float:
	"""Sum ``refund.transactions[].amount`` (the cash side of the refund).

	The line-item subtotal in ``refund.refund_line_items[].subtotal`` is
	a separate field; the cash transactions are authoritative for ToDo
	priority since they reflect what actually moved out of the merchant
	account.
	"""
	total = 0.0
	for tx in (refund or {}).get("transactions", []) or []:
		total += flt(tx.get("amount"))
	return total


# ── Frappe-coupled handlers ────────────────────────────────────────


def handle_refund_created(payload, request_id=None):
	"""B24a: webhook entry point for ``refunds/create``.

	Resolves the parent SO via the Shopify ``order_id`` on the refund
	payload, then dispatches to :func:`apply_refund`. Idempotent on
	``refund_id`` via ToDo-lookup: if a ToDo on this SO already mentions
	this refund_id in its description, no further action is taken.

	``payload`` is the JSON body Shopify sends (a Refund resource).
	"""
	refund = payload if isinstance(payload, dict) else (payload or {})
	order_id = refund.get("order_id")
	refund_id = str(refund.get("id") or "")

	if not order_id or not refund_id:
		return

	so_name = frappe.db.get_value(
		"Sales Order", {ORDER_ID_FIELD: str(order_id)}, "name"
	)
	if not so_name:
		# Unknown order — Shopify Log will record the event via the
		# regular webhook plumbing. Nothing else to do here.
		return

	# Idempotency: short-circuit if a ToDo on this SO already mentions
	# this refund_id (ToDo.reference_name is indexed; substring scan
	# over the per-SO result set is cheap).
	existing = frappe.get_all(
		"ToDo",
		filters={
			"reference_type": "Sales Order",
			"reference_name": so_name,
			"description": ["like", f"%{refund_id}%"],
		},
		limit=1,
	)
	if existing:
		return

	sales_order = frappe.get_doc("Sales Order", so_name)
	apply_refund(refund, sales_order)


def apply_refund(refund, sales_order):
	"""B24a: core dispatch — flag SO Item (+ DN Item) per refund_line_item.

	Steps:
	  1. For each refund_line_item, locate the matching SO Item line.
	  2. SO docstatus=0 (draft): reduce qty in-place; flag if qty→0.
	     SO docstatus=1 (submitted): flag SO Item + mirror to DN Item if a
	     DN row exists for that SO Item.
	  3. Enqueue ONE ToDo per refund event (description carries refund_id
	     for idempotency).
	  4. Append a Comment to the SO timeline noting the lines flagged.
	  5. Refetch the parent Shopify order and call
	     :func:`populate_current_totals` to update the 3 SO drift fields.

	No SO amend. No DN amend. No SI/CN creation.
	"""
	refund_id = str(refund.get("id") or "")
	refund_lines = refund.get("refund_line_items") or []
	refunded_at = refund.get("created_at")
	restock_type = _refund_restock_type(refund_lines)
	refund_total = _sum_refund_amount(refund)

	flagged_summary = []

	for rli in refund_lines:
		shopify_line = rli.get("line_item") or {}
		so_item = _match_so_item(sales_order, shopify_line)
		if so_item is None:
			# No matching line — record in summary so the Comment shows it.
			flagged_summary.append(
				f"(no match) {shopify_line.get('title') or shopify_line.get('sku') or '?'}"
			)
			continue

		if sales_order.docstatus == 0:
			# Draft SO: reduce qty in place; flag if qty drops to zero.
			refunded_qty = int(rli.get("quantity") or 0)
			new_qty = max(0, int(so_item.qty or 0) - refunded_qty)
			frappe.db.set_value(
				"Sales Order Item", so_item.name, "qty", new_qty
			)
			if new_qty == 0:
				flag_so_item_refunded(sales_order.name, so_item.name, refunded_at)
		else:
			# Submitted SO: flag only.
			flag_so_item_refunded(sales_order.name, so_item.name, refunded_at)
			# Mirror to DN Item if a DN line points at this SO Item.
			for dn_name, dn_item_name in _find_dn_items_for_so_item(so_item.name):
				flag_dn_item_refunded(dn_name, dn_item_name, refunded_at)

		flagged_summary.append(
			f"{so_item.item_code or so_item.item_name} x{rli.get('quantity', 1)}"
		)

	enqueue_refund_todo(
		sales_order=sales_order,
		refund=refund,
		restock_type=restock_type,
		refund_total=refund_total,
	)

	sales_order.add_comment(
		"Comment",
		text=(
			f"Refund {refund_id} ({restock_type or 'shipping-only'}): "
			+ ", ".join(flagged_summary or ["no line items"])
		),
	)

	# Drift totals (running current_* on SO). Refetch the parent order
	# because the webhook payload only has the refund resource.
	shopify_order = _fetch_shopify_order(sales_order.get(ORDER_ID_FIELD))
	if shopify_order is not None:
		populate_current_totals(sales_order, shopify_order)


def flag_so_item_refunded(so_name, so_item_name, refunded_at):
	"""B24a: set ``shopify_refunded=1`` + ``shopify_refunded_at`` on a SO Item
	row. Uses ``frappe.db.set_value`` so it works on submitted SOs (the
	Custom Fields are declared ``allow_on_submit=1``). Frappe Version
	captures the change automatically."""
	frappe.db.set_value(
		"Sales Order Item", so_item_name,
		{ITEM_REFUNDED_FIELD: 1, ITEM_REFUNDED_AT_FIELD: refunded_at},
		update_modified=False,
	)


def flag_dn_item_refunded(dn_name, dn_item_name, refunded_at):
	"""B24a: mirror of :func:`flag_so_item_refunded` for DN Item."""
	frappe.db.set_value(
		"Delivery Note Item", dn_item_name,
		{ITEM_REFUNDED_FIELD: 1, ITEM_REFUNDED_AT_FIELD: refunded_at},
		update_modified=False,
	)


def enqueue_refund_todo(sales_order, refund, restock_type, refund_total):
	"""B24a: create one native ToDo per refund event. Description carries
	refund_id for idempotency on webhook re-fires."""
	refund_id = str(refund.get("id") or "")
	has_lines = bool(refund.get("refund_line_items"))
	title = _restock_type_to_todo_title(restock_type or "", has_lines)
	priority = _todo_priority(refund_total)

	line_summary = ", ".join(
		f"{(rli.get('line_item') or {}).get('sku') or (rli.get('line_item') or {}).get('title') or '?'}"
		f" x{rli.get('quantity', 1)}"
		for rli in (refund.get("refund_line_items") or [])
	) or "(shipping-only)"

	description = (
		f"refund_id={refund_id} • restock_type={restock_type or 'shipping_only'} "
		f"• lines: {line_summary}\n\n{title}"
	)

	frappe.get_doc({
		"doctype": "ToDo",
		"reference_type": "Sales Order",
		"reference_name": sales_order.name,
		"description": description,
		"priority": priority,
		"status": "Open",
	}).insert(ignore_permissions=True)


def populate_current_totals(sales_order_doc, shopify_order):
	"""B24c: write Shopify's running ``current_*`` totals onto the SO doc.

	Idempotent. Safe to call from sync_sales_order, handle_order_edited,
	handle_refund_created, and the Phase 1 backfill pass.
	"""
	# Accept either a Frappe doc or a name string; load if needed.
	if isinstance(sales_order_doc, str):
		sales_order_doc = frappe.get_doc("Sales Order", sales_order_doc)

	values = {
		CURRENT_SUBTOTAL_PRICE_FIELD: flt(_extract_current(shopify_order, "current_subtotal_price")),
		CURRENT_TOTAL_PRICE_FIELD: flt(_extract_current(shopify_order, "current_total_price")),
		CURRENT_TOTAL_DISCOUNTS_FIELD: flt(_extract_current(shopify_order, "current_total_discounts")),
	}
	frappe.db.set_value(
		"Sales Order", sales_order_doc.name, values, update_modified=False
	)


def reconcile_so_against_current_quantity(so_name, dry_run=False):
	"""B24b backfill helper. Compare ERPNext SO Items qty vs Shopify
	``current_quantity`` (refetched live). For each SO line where ERPNext
	qty > Shopify current_quantity, apply the same flag logic with a
	synthetic refund (refund_id = ``reconcile-<so>``)."""
	sales_order = frappe.get_doc("Sales Order", so_name)
	shopify_order = _fetch_shopify_order(sales_order.get(ORDER_ID_FIELD))
	if shopify_order is None:
		return {"planned": [], "applied": [], "failed": ["shopify_order_not_found"]}

	shopify_lines = _shopify_lines(shopify_order)
	planned, applied = [], []

	for so_item in sales_order.items:
		matching = _match_shopify_line_to_so_item(shopify_lines, so_item)
		if matching is None:
			continue
		current_qty = int(matching.get("current_quantity", matching.get("quantity", 0)) or 0)
		if current_qty < int(so_item.qty or 0):
			planned.append({
				"so_item": so_item.name,
				"erp_qty": int(so_item.qty or 0),
				"shopify_current_qty": current_qty,
			})
			if not dry_run:
				refunded_at = _first_refund_created_at_for_line(
					shopify_order, matching)
				flag_so_item_refunded(sales_order.name, so_item.name, refunded_at)
				for dn_name, dn_item_name in _find_dn_items_for_so_item(so_item.name):
					flag_dn_item_refunded(dn_name, dn_item_name, refunded_at)
				applied.append(so_item.name)

	if not dry_run and planned:
		# One synthetic ToDo per reconcile pass (not per line).
		synthetic_refund = {
			"id": f"reconcile-{so_name}",
			"refund_line_items": [{"line_item": {}}] * len(planned),
			"transactions": [],
		}
		enqueue_refund_todo(
			sales_order=sales_order,
			refund=synthetic_refund,
			restock_type="cancel",
			refund_total=0,
		)
		populate_current_totals(sales_order, shopify_order)

	return {"planned": planned, "applied": applied, "failed": []}


# ── Internal helpers ──────────────────────────────────────────────


def _refund_restock_type(refund_lines):
	"""Return the dominant ``restock_type`` across refund_line_items.

	If empty: returns ``""`` (caller surfaces shipping-only title).
	If all the same: returns that value.
	If mixed: returns ``"cancel"`` (most conservative — assumes stock
	reversal is required).
	"""
	if not refund_lines:
		return ""
	types = {rli.get("restock_type") for rli in refund_lines if rli.get("restock_type")}
	if not types:
		return ""
	if len(types) == 1:
		return next(iter(types))
	return "cancel"


def _match_so_item(sales_order, shopify_line):
	"""Locate the SO Item row for a Shopify refund_line_item.

	Match priority: item_code + rate → item_code → item_name. Conservative
	when ambiguous — returns the first hit; the Comment narrative records
	when multiple lines could match."""
	sku = (shopify_line.get("sku") or "").strip()
	title = (shopify_line.get("name") or shopify_line.get("title") or "").strip()
	price = flt(shopify_line.get("price"))

	by_code = [it for it in sales_order.items if (it.item_code or "") == sku]
	if not by_code and sku:
		# fallback: try item_name match
		by_code = [it for it in sales_order.items if (it.item_name or "") == title]
	if not by_code:
		return None
	if len(by_code) == 1:
		return by_code[0]
	# Multiple SO Items with same item_code — disambiguate by rate.
	by_rate = [it for it in by_code if abs(flt(it.rate) - price) < 0.01]
	return by_rate[0] if by_rate else by_code[0]


def _find_dn_items_for_so_item(so_item_name):
	"""Return list of ``(dn_name, dn_item_name)`` tuples for every DN Item
	row that traces back to this SO Item via ``so_detail``."""
	rows = frappe.get_all(
		"Delivery Note Item",
		filters={"so_detail": so_item_name},
		fields=["name", "parent"],
	)
	return [(row["parent"], row["name"]) for row in rows]


@temp_shopify_session
def _fetch_shopify_order(order_id):
	"""Fetch a Shopify Order resource by id. Returns the resource ``.attributes``
	(dict-like). Returns None on miss/error."""
	if not order_id:
		return None
	try:
		order = shopify.Order.find(order_id)
	except Exception:
		return None
	if order is None:
		return None
	return getattr(order, "attributes", None) or {}


def _extract_current(shopify_order, key):
	"""Helper: dict-or-attribute access on a Shopify order resource."""
	if isinstance(shopify_order, dict):
		return shopify_order.get(key)
	return getattr(shopify_order, key, None)


def _shopify_lines(shopify_order):
	if isinstance(shopify_order, dict):
		return shopify_order.get("line_items") or []
	return getattr(shopify_order, "line_items", None) or []


def _match_shopify_line_to_so_item(shopify_lines, so_item):
	"""Reverse of :func:`_match_so_item` — find the Shopify line that
	corresponds to an ERPNext SO Item row."""
	for li in shopify_lines:
		line = li if isinstance(li, dict) else getattr(li, "attributes", {}) or {}
		if (line.get("sku") or "") == (so_item.item_code or ""):
			return line
	return None


def _first_refund_created_at_for_line(shopify_order, shopify_line):
	"""Find the earliest refunds[].created_at that touched this line.

	Used by reconcile to set ``shopify_refunded_at`` to the actual
	historical refund moment, not the reconcile-run timestamp.
	"""
	if isinstance(shopify_order, dict):
		refunds = shopify_order.get("refunds") or []
	else:
		refunds = getattr(shopify_order, "refunds", None) or []
	line_id = shopify_line.get("id") if isinstance(shopify_line, dict) else None

	candidates = []
	for refund in refunds:
		refund_dict = refund if isinstance(refund, dict) else (
			getattr(refund, "attributes", {}) or {}
		)
		for rli in refund_dict.get("refund_line_items") or []:
			inner = rli.get("line_item") or {}
			if inner.get("id") == line_id or (
				inner.get("sku") and shopify_line.get("sku")
				and inner["sku"] == shopify_line["sku"]
			):
				candidates.append(refund_dict.get("created_at"))
	candidates = [c for c in candidates if c]
	return min(candidates) if candidates else None
