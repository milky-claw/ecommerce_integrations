# Changelog — `milky-claw/ecommerce_integrations` fork

Fork-specific changes on top of upstream `frappe/ecommerce_integrations` (branch `version-16`). Upstream tags (`v1.x.y`, `v16.0.0`) remain as-is; our fork's additions use the **`yei-v*`** prefix (YGH Ecommerce Integrations) to avoid namespace collision with upstream and with sibling apps like `ygf-*` (the YGH FedEx app).

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning SemVer-for-our-fork starting at `ygh-v1.0.0`.

**Scope of this fork:** runtime fixes + connector patches deployed to `yourgreenhouses.frappe.cloud` to support YourGreenhouses' Shopify → ERPNext order + product sync. Upstream PRs are the right destination for most of these; we fork for speed while stabilizing, and merge upstream when the patches are broadly useful.

**Target site:** `yourgreenhouses.frappe.cloud` (bench-37067, f2-virginia.frappe.cloud).

---

## [yei-v1.4.3] — 2026-05-15

### Added
- `_reconcile_so_line_items` (orders/edited handler) now mirrors the
  SOI refund flag onto every linked DNI via
  `_find_dn_items_for_so_item` + `flag_dn_item_refunded`, called
  immediately after `flag_so_item_refunded`.

### Why
Paired with **ygf-v0.5.9** (materializer-side copy). Refund flagging
has four paths into the SOI; pre-v1.4.3 only `refunds/create` →
`apply_refund` mirrored to DNI. The orders/edited path (qty drop to
zero is interpreted as a refund) flagged SOI but stopped there,
leaving DNI stale. Audit 2026-05-15 surfaced 2 sheet-visible cases
(#3843 WMP-FUL-0600X0300, #4291 RGB-GST-095X075H40); site-wide drift
was 18 DNIs across 12 Draft DNs (workspace one-shot Server-Script
backfill repaired pre-fix data; ygf-v0.5.9 closes the
materialization-time half of the gap).

### Tests
`tests/test_b24_refunds.py::TestReconcileMirrorsRefundToDNI` (2
source-invariant AST tests, matches B24's existing test style):
1. `test_reconciler_imports_dn_mirror_helpers` — both
   `flag_dn_item_refunded` and `_find_dn_items_for_so_item` appear
   in the function body.
2. `test_reconciler_calls_flag_dn_item_refunded_in_refund_branch` —
   call ordering: SOI flagged first, DNI mirror after.

Both pass; full `test_b24_refunds.py` 59/59.

### Mechanism

Drop-in copy of `apply_refund`'s mirror pattern at refund.py:174-176,
sequenced after `flag_so_item_refunded` in the `cq == 0` refund
branch. No new behaviour when no DNs exist for the SOI (loop is
empty). Idempotent: `flag_dn_item_refunded` uses
`frappe.db.set_value`; re-runs with the same value are no-ops.

---

## [yei-v1.4.2] — 2026-05-15

### Added
- `backfill_mirror` admin-only whitelisted method in
  `shopify/order.py`. Uses `frappe.db.set_value(..., update_modified=False)`
  to write `Sales Order.shopify_fulfillment_status` on submitted SOs,
  bypassing both the `allow_on_submit=0` gate and the ygh_fedex
  `cascade_post_submit_changes` hook.

### Why
- Mirror backfill 2026-05-14 (688 candidate SOs) left 4 with real-drift
  payloads unwritten because the REST PUT path is doubly blocked.
  v1.3.10's `prepare_delivery_note` mirror write only fires on
  `fulfillments/create` webhook; terminal/un-fulfilled SOs never emit
  more events. This method is the bridge.

### Tests
- `test_backfill_mirror_admin_only` — non-System-Manager → throw.
- `test_backfill_mirror_writes_value` — set_value path called with
  expected args.
- `test_backfill_mirror_idempotent` — same-value re-call is no-op.
- `test_backfill_mirror_normalizes_empty` — None → "".
- `test_backfill_mirror_rejects_missing_so` — non-existent SO → throw.

### Mechanism

Mirrors `replay_handle_order_edited` (yei-v1.3.4) admin-gate pattern.
Same module placement, same docstring style, same `frappe.session.user
== "Administrator"` OR `"System Manager" in roles` gate.

---

## [yei-v1.4.1] — 2026-05-15

**Hotfix: `SETTING_DOCTYPE` import missing in `shopify_setting.py`** — `force_reregister_webhooks()` (introduced in v1.4.0) raised `NameError: name 'SETTING_DOCTYPE' is not defined` on first call post-deploy. Added the constant to the existing `from ecommerce_integrations.shopify.constants import (...)` block.

No behavioural change to v1.4.0's primary work (webhook handler, helpers, tests). Only the post-deploy admin helper was affected.

---

## [yei-v1.4.0] — 2026-05-15

**Subscribe to `orders/updated` for fulfillment + shipping-address mirror catch-up.** Adds the seventh Shopify webhook subscription with deliberately narrow scope: defense-in-depth on `fulfillment_status` and first-event coverage on `shipping_address` edits with 4-tier DN-state guard. Pairs with cohort B revert (33 SOs flipped from "On Hold" → "To Deliver and Bill" in same session).

### Why

Two gaps in webhook coverage:

1. **Defense-in-depth on fulfillment.** `orders/fulfilled` already writes `shopify_fulfillment_status="fulfilled"` (since yei-v1.3.10 closed the architectural gap there). But if `orders/fulfilled` is missed (Shopify retry exhaustion, queue stall, transient handler exception), the mirror stays empty until a chance `orders/edited` triggers a full refresh. `orders/updated` fires on every order-object mutation, so it's the natural catch-up channel.

2. **No event covers shipping-address edits.** When a Shopify rep edits ship-to (typo correction, customer call-in), neither `orders/create` nor `orders/edited` nor `orders/paid` fires. The ERPNext mirror stays stale; supplier sheets show the OLD address. Critical pre-AWB; intervention-worthy post-AWB.

### What changes

**Subscribed events:** `orders/updated` added to `WEBHOOK_EVENTS` + `EVENT_MAPPER` in `shopify/constants.py`.

**Handler (`shopify/order.py`):** new `handle_order_updated(payload, request_id=None)` reacts to exactly two conditions:

| Condition | Action |
|---|---|
| Payload `fulfillment_status == "fulfilled"` AND SO mirror != "fulfilled" | Write SO mirror via `frappe.db.set_value(update_modified=False)` |
| Payload `shipping_address` differs from SO's linked Address (field-by-field on labelled subset) | Invoke 4-tier subhandler |

The labelled subset (fields that go on a FedEx label): `address1`, `address2`, `city`, `province` (→ Address.state), `zip` (→ pincode), `country`, `phone`, plus `first_name`+`last_name` (→ Address.address_title). Excluded: `company`, `email`, `latitude`, `longitude`.

**4-tier DN-state guard (`_compute_tier`):**

| Tier | DN state | Action |
|---|---|---|
| 1 | No alive DN | Update SO + linked Address. Silent. |
| 2 | Alive DN(s) draft, no AWB | Update SO + linked Address (cascades to DN.shipping_address rendering). Silent. |
| 3 | AWB minted, pre-pickup | Update SO + linked Address. Log alert via `frappe.log_error` (Tier 3) for ops review. |
| 4 | AWB + pickup (PICKED_UP / IN_TRANSIT / DELIVERED) | Update SO + linked Address. Log URGENT alert. |

**Safety gate:** in-place Address update only runs if the Address record carries `shopify_address_id` (i.e., is a per-order Address from `_create_per_order_shipping_address`). Shared Addresses (e.g. Customer-primary) are refused — manual review path. Prevents cross-doc surprises.

**Explicitly out of scope:**

- `financial_status` mirror. The "BNPL pending→paid unhold" use case is genuine but the derivation `financial_status in ("pending", "partially_paid") → On Hold` was flagged as flawed (ygf-v0.5.8 CHANGELOG). Re-attempt requires a real Shopify hold signal (e.g., `fulfillment_orders[].request_status == "ON_HOLD"`). Out of scope for v1.4.0.
- `tags`, `note`, `customer.*`, `email` — no downstream ERPNext consumer.
- `fulfillment_status` values OTHER than `"fulfilled"` (un-fulfill, partial, restocked, null) — `orders/fulfilled` + `orders/partially_fulfilled` already cover those transitions.
- Telegram POST for Tier 3/4 alerts. Handler logs via `frappe.log_error`; a separate workspace-side commit will route those error_log entries to Kete's chat. Out of band, not blocking.

**Idempotency:** handler reads SO + Address state, compares to payload, writes only on delta. Equal state → no DB write → no further events → no loop. This pattern survives the future ygf → Shopify fulfillment push (currently out of scope): handler will read `shopify_fulfillment_status == "fulfilled"`, payload will agree, no-op.

**Companion helper:** `force_reregister_webhooks()` added to `shopify_setting.py` (whitelisted, System-Manager-only). Clears the `Shopify Webhooks` child table and saves Setting → triggers `_handle_webhooks` → full unregister+re-register from current `WEBHOOK_EVENTS`. Used once post-deploy to add `orders/updated` to existing Shopify-side registrations (the install hook only fires on empty child table).

### Files

| File | Change |
|---|---|
| `ecommerce_integrations/shopify/constants.py` | Add `"orders/updated"` to `WEBHOOK_EVENTS` + `EVENT_MAPPER` |
| `ecommerce_integrations/shopify/order.py` | New `handle_order_updated` + 3 helpers (`_shipping_address_differs`, `_handle_shipping_address_change`, `_compute_tier`) + `_SHIPPING_ADDR_FIELD_MAP` constant |
| `ecommerce_integrations/shopify/doctype/shopify_setting/shopify_setting.py` | New `force_reregister_webhooks` whitelisted helper |
| `ecommerce_integrations/shopify/tests/test_order_updated.py` | NEW — 33 bench-independent tests |
| `docs/orders-updated-design.md` | NEW — design doc |

### Tests

33/33 PASS on `python3 ecommerce_integrations/shopify/tests/test_order_updated.py`:

- 2 TestConstantsRegistration (WEBHOOK_EVENTS + EVENT_MAPPER)
- 11 TestHandleOrderUpdatedSourceInvariant (handler shape, narrow-scope guards, helper existence, idempotency pattern, safety gate, SO-not-mirrored skip)
- 3 TestForceReregisterHelper (whitelist decorator, child-table clear, role gate)
- 9 TestShippingAddressDiffersBehavioral (identical/changed/whitespace/null cases)
- 8 TestComputeTierBehavioral (tier 1/2/3/4 paths, multiple DN aggregation, case-insensitive pickup status)

### Pairs with

Workspace cohort B revert run 2026-05-15 (33 SOs flipped from "On Hold" → "To Deliver and Bill" via `erpnext.selling.doctype.sales_order.sales_order.update_status`). Same root-cause family: financial_status derivation flagged as flawed; the revert cleans historical residue; this patch ensures future shipping-address edits + fulfilled defense-in-depth flow correctly.

---

## [yei-v1.3.10] — 2026-05-14

**`prepare_delivery_note` mirrors `Sales Order.shopify_fulfillment_status`.** Closes the architectural gap where Shopify's `orders/fulfilled` + `orders/partially_fulfilled` webhooks created a DN but never updated the SO-side mirror field, causing ~240+ SOs to drift in any 30-day window.

### Why

`Sales Order.shopify_fulfillment_status` (the `ORDER_FULFILLMENT_STATUS_FIELD` Custom Field) had exactly two writers before this patch:
- `order.sync_sales_order` at line 173 (set at SO insert from Shopify's initial value).
- `order.handle_order_edited` at line 949 (refreshed on `orders/edited`).

`fulfillment.prepare_delivery_note` — the handler for `orders/fulfilled` and `orders/partially_fulfilled` — created a DN and stamped DN-level Shopify fields, but never wrote back the SO-level mirror. Population estimate: 29 of 30 most-recently-fulfilled Shopify orders had stale ERPNext mirror at investigation time on 2026-05-14. Downstream consumers (notably `ygh_fedex.supplier_sync.writer` per ygf-v0.5.7) rely on the mirror for scope gating + status-label stamping; without this patch they operate on stale data.

### What changes

Inside `prepare_delivery_note`, after `create_delivery_note(...)` returns successfully and before `create_shopify_log(status="Success")`, write the mirror via `frappe.db.set_value(..., update_modified=False)`. Same pattern as `handle_order_edited` at order.py:944. `update_modified=False` skips the `allow_on_submit=0` check on the Custom Field and avoids bumping `modified`.

### Dependencies

- Paired with ygf-v0.5.7 (writer policy reversal) — ship together in a single Press deploy so the mirror-write activates simultaneously with the writer's new "keep fulfilled rows" behavior. No version-skew window.
- Cohort A backfill (~500 SOs) follows the deploy to clean historical drift.

### Risk

- Race with `handle_order_edited` writing the same field simultaneously: harmless (last writer wins; values converge to Shopify's current state via either handler).
- Idempotent on webhook redelivery: `db.set_value` overwrites unconditionally.
- Restock/cancel path NOT addressed in this release (`orders/cancelled` handler doesn't update mirror). Defer; rare event.

### Tests

- Post-deploy: trigger one Shopify fulfillment on a test SO via `orders/fulfilled` webhook. Verify `Sales Order.shopify_fulfillment_status` flips to `"fulfilled"` (not via `handle_order_edited`). Verify EIL `prepare_delivery_note` Success count continues without exception.

---

## [yei-v1.3.9] — 2026-05-14

**Hotpatch: live `orders/edited` webhook handler now activates a Shopify session.** Add `@temp_shopify_session` to `handle_order_edited` so the worker's `Order.find(order_id)` REST call inside the handler succeeds. First live manifestation surfaced 2026-05-14 18:28:28Z by Shopify order #4978 (AfterSell Upsell added GNR-AIR-VENT-MANU after initial order creation; webhook errored at `Order.find`; SOI never created; supplier sheet missed the line).

### Why

Webhook dispatch flow (verified by reading [`connection.py:135-159`](ecommerce_integrations/shopify/connection.py#L135-L159)):

1. `store_request_data` receives the POST, runs HMAC validation via `_validate_request`.
2. `_validate_request` ([connection.py:162-188](ecommerce_integrations/shopify/connection.py#L162-L188)) HMAC-verifies only — **does not activate a Shopify session**. The fork's prior docstring on `replay_handle_order_edited` ([order.py:966-968](ecommerce_integrations/shopify/order.py#L966-L968) pre-v1.3.9) claimed it did. False.
3. `process_request` ([connection.py:148](ecommerce_integrations/shopify/connection.py#L148)) enqueues a **background job** via `frappe.enqueue(is_async=True, ...)`. The worker runs in a fresh Frappe context.
4. The worker calls `handle_order_edited(payload, request_id)`. The first thing the handler needs is the full Shopify order — `Order.find(order_id)` at line 881 — because the webhook delta only carries `{additions, removals}` of line_item_ids, no SKUs/prices/titles.
5. With no session: `shopify.base.connection` raises `ValueError: No shopify session is active`.

Pre-v1.3.3 this was latent (every webhook short-circuited at `order_id=None` due to the payload-nesting bug). v1.3.3 fixed the payload nesting → `Order.find` actually runs → first session-bug manifestation. yei-v1.3.4 added `@temp_shopify_session` to the replay wrapper but not the webhook entry point.

Empirical evidence (Ecommerce Integration Log `998sluhb19`):

```
2026-05-14 18:28:28.489872  handle_order_edited  Error
message: orders/edited: Shopify REST fetch failed for order 11320385175915:
         No shopify session is active
traceback: order.py:881 → Order.find → connection → ValueError
```

### Code change

- [`ecommerce_integrations/shopify/order.py`](ecommerce_integrations/shopify/order.py): single decorator add — `@temp_shopify_session` directly preceding `def handle_order_edited(...)`. Docstring updated to record the root-cause history.
- [`ecommerce_integrations/__init__.py`](ecommerce_integrations/__init__.py): version bumped `1.3.8` → `1.3.9`.

### Tests

New test class `TestV139HandleOrderEditedSessionDecoratorSourceInvariant` in [`tests/test_connector_patches.py`](ecommerce_integrations/shopify/tests/test_connector_patches.py):

- `test_version_at_or_above_139` — package version pin.
- `test_handle_order_edited_has_temp_shopify_session_decorator` — regex assertion that `@temp_shopify_session` directly precedes the `def`.
- `test_handle_order_edited_still_calls_order_find` — defensive: handler must still call `Order.find` (otherwise the decorator is dead weight; locks the requirement that the handler fetches the full Shopify order).

Prior version pin in `TestV138GetSalesOrderDisambiguationSourceInvariant` changed from a fixed `1.3.8` string to a `>= 1.3.8` floor so subsequent patch bumps don't break the v1.3.8 test class.

### Reversibility

Single-decorator revert: remove the `@temp_shopify_session` line above `def handle_order_edited`. Bench: revert via Press to v1.3.8 candidate. Note: reverting reopens the live webhook bug — any incoming `orders/edited` will error at `Order.find`. Don't revert without replacing the handler entirely.

### Blast radius before fix

Every `orders/edited` webhook on prod since v1.3.3 went live was returning `Error` at `Order.find`. AfterSell Upsell, Shopify operator-side edits (rep adds a line after submit), customer-side edit-order flows — all of them failed silently into EIL `Error` rows. Until v1.3.9 deploys, `replay_handle_order_edited` is the only working path. Counted today: at least 1 live case (#4978). The wider population is "all post-v1.3.3 `orders/edited` EIL rows with `Error` status and the session-active traceback" — backfill scope for a separate sweep.

---

## [yei-v1.3.7] — 2026-05-14

**Hotpatch for one v1.3.6 regression surfaced by the 2026-05-14 Class A retrofit (11 Baumera-cluster SOs).** v1.3.6 fixed the qty-update path (Class B) by setting `sales_order.flags.ignore_validate_update_after_submit = True` *only* when `qty_updated` is non-empty. The add-only path (Class B-add) still hit `UpdateAfterSubmitError: Not allowed to change Total Quantity after submission from X to Y` because appending an SOI mutates the parent's computed `total_qty` aggregate, which the parent-side `validate_update_after_submit` rejects without the flag. v1.3.7 hoists the parent flag out of `if qty_updated:` so it fires for both `added` and `qty_updated` branches.

### Why

The 2026-05-14 backfill stamped 22 `shopify_line_item_id` values on existing SOIs across 11 SOs (single-clause Class A scope: Baumera-cluster orders where the bare `<order>.pdf` invoice variant was missing the lid linker). Each replay was supposed to append exactly 1 new SOI (and SAL-ORD-2026-00946 +2) for the late-added Shopify line. Instead, every replay hit:

```
Not allowed to change Total Quantity after submission from 1.0 to 2.0
Not allowed to change Total Quantity after submission from 3.0 to 9.0
... (11 such rows in Ecommerce Integration Log)
```

The replay returns HTTP 200 with `status="Error"` on the EIL row (the handler's outer `try/except Exception` swallows the error into the log) — silently false-success at the HTTP layer.

Root cause (single-clause): v1.3.6's save block (yei-v1.3.6 commit `1b517fe`, order.py:1083-1087):

```python
if qty_updated:
    sales_order.flags.ignore_validate_update_after_submit = True
sales_order.save(ignore_permissions=True)
```

The flag is gated on `qty_updated`. The `added`-only path bypasses it. But the parent-side validator (document.py:1098-1101 → controllers/accounts_controller.py) iterates the parent's computed fields and rejects `total_qty` mutation regardless of which branch caused it. The table-level Property Setter `Sales Order.items.allow_on_submit=1` (installed v1.3.3) permits child add/remove but does NOT permit parent computed-field changes.

### Code changes

- [`shopify/order.py:_reconcile_so_line_items`](ecommerce_integrations/shopify/order.py) — Single fix: hoist `sales_order.flags.ignore_validate_update_after_submit = True` out of the `if qty_updated:` nested block. Flag now fires whenever the save block runs (i.e. for either `added` or `qty_updated`). Updated docstring + inline comment to document the v1.3.7 reasoning.

- [`__init__.py`](ecommerce_integrations/__init__.py) — Version bump 1.3.6 → 1.3.7.

### Tests

3 new tests in `tests/test_connector_patches.py`:

- **`TestV137HotpatchSourceInvariant`** (2 tests):
  - `test_parent_flag_not_gated_on_qty_updated_only` — regression guard via `assertNotRegex` against the v1.3.6 buggy pattern (`if qty_updated:` immediately followed by parent flag). Any reintroduction of the gate fails this test.
  - `test_parent_flag_fires_inside_save_block` — positive assertion that the flag appears between the `if added or qty_updated:` guard and `sales_order.save(...)`.

- **`TestV137HotpatchBehaviour`** (1 test):
  - `test_reconcile_pure_add_path_sets_parent_flag` — behavioural unit via the inline `_reconcile_so_line_items_v136` helper (updated to v1.3.7 semantics). Setup: 1 existing SOI (lid=A, qty=1) + Shopify reports [lid=A unchanged, lid=B new with cq=2]. Asserts: `added=["ACC-NEW"]`, `qty_updated=[]`, parent flag set. This is the exact regression case the 2026-05-14 retrofit hit.

Updated existing tests:

- `TestV136HotpatchSourceInvariant.test_class_b_parent_flag_set_in_save_block` (renamed from `…_when_qty_updated`) — broader regex matching the hoisted flag.
- `TestV136HotpatchSourceInvariant.test_version_bumped_to_137` (renamed; expects 1.3.7).
- `TestV136HotpatchBehaviour.test_add_only_sets_parent_flag` (inverted from `_does_not_set_parent_qty_flag` — the prior test pinned the bug).
- Inline `_reconcile_so_line_items_v136` helper updated: parent flag now fires on `if added or qty_updated:` (mirrors real code).

Total: 165 tests in `test_connector_patches.py` (162 baseline + 3 new), 0 regressions across `test_b24_refunds.py` (57), `test_freight_class.py` (39), `test_supplier_sheet_3_issues.py` (65). Aggregate: 326 tests.

### Reversibility

Fully reversible by reverting this commit + redeploying yei-v1.3.6. The fix is pure-additive in semantics: the flag was already set in one branch; now it's set in both. The flag is ephemeral (in-memory, cleared between request cycles) and is the same primitive ERPNext's own `update_child_qty_rate` uses (accounts_controller.py:4167) for the same purpose.

Rollback path: `git revert <commit>` + Press revert-candidate.

### Refs

- 2026-05-14 Class A retrofit ledger: `data/backfills/backfill_class_a_lid_stamps_2026-05-14.jsonl`
- 11 failed EIL rows at 2026-05-14 16:12:37-44Z (each `NONE|Error` with "Not allowed to change Total Quantity after submission from X to Y")
- accounts_controller.py:4167 (the blessed parent flag pattern)
- document.py:1098-1101 (the validate_update_after_submit short-circuit)
- v1.3.6 commit `1b517fe` (the gating bug we hotpatch)

---

## [yei-v1.3.6] — 2026-05-14

**Hotpatch for two v1.3.5 regressions surfaced by autopilot Phase 4 replay sweep (2026-05-14T01:50Z).** Phase 4 replayed 51 stuck SOs through `replay_handle_order_edited`; 28 failed Class A (`TypeError` in `calculate_commission`), 9 failed Class B (`UpdateAfterSubmitError` on per-field qty edit), 14 returned "Success +0 items" (false-negative matches — no work). Every Class A/B failure rolled back the transaction; bench state unchanged from Phase 0 snapshot.

### Why

v1.3.5's save block in `_reconcile_so_line_items` had two latent bugs that only surface when (a) a new SOI is appended OR (b) an existing SOI's qty mutates — both of which v1.3.5 explicitly added to support the Baumera 2026-05-13 audit findings. Phase 0/1/2 of autopilot tested individual call paths but not the wide replay sweep that exercised every code path against historical SOs.

**Class A — `TypeError: unsupported operand type(s) for +: 'float' and 'NoneType'`.** Root: `_build_soi_from_shopify_line` returns a dict with `rate`, `qty`, `price_list_rate`, `uom`, etc. but NOT the per-item calculated fields (`amount`, `net_amount`, `base_amount`, `base_net_amount`). `_reconcile_so_line_items` appends that dict via `sales_order.append("items", new_row)` and immediately calls `sales_order.save(ignore_permissions=True)`. The save triggers `on_update_after_submit` → `calculate_commission` (selling_controller.py:207-228), whose line 222 does `sum(item.base_net_amount for item in self.items if item.grant_commission)`. With `base_net_amount=None` on the new row, `sum([None, 0.0, ...])` raises TypeError.

**Class B — `Row #N: Not allowed to change Quantity after submission from M to K`.** Root: v1.3.5's qty-diff branch (order.py:1024-1032) directly mutates `soi.qty` on an existing existing-LID SOI. The yei-v1.3.3 Property Setter `Sales Order Item.items.allow_on_submit=1` is **table-level** (lets the parent's `items` table accept add/delete rows on submit) but NOT per-field. ERPNext's `base_document._validate_update_after_submit` (base_document.py:1259-1294) iterates each field and raises `UpdateAfterSubmitError` if `df.allow_on_submit` is false and the value changed. The field-level `qty.allow_on_submit` on Sales Order Item is 0.

### Code changes

- [`shopify/order.py:_reconcile_so_line_items`](ecommerce_integrations/shopify/order.py) — Two fixes:
  - **Class A** — before `sales_order.save()`, insert `sales_order.set_missing_values()` + `sales_order.calculate_taxes_and_totals()`. The blessed ERPNext path: `calculate_taxes_and_totals` (accounts_controller.py:736-748) calls the module-level class (taxes_and_totals.py:32) which runs `_calculate()` → `calculate_item_values()` (taxes_and_totals.py:167-243) → per-item `amount`/`net_amount` are computed, then `_set_in_company_currency(item, [...])` (taxes_and_totals.py:245-251) populates `base_amount`, `base_net_amount`, `base_rate`, etc. on every item including the newly-appended row. `set_missing_values()` (selling_controller.py:109-115) is mostly a no-op for already-complete submitted SOs but is cheap+idempotent (defence-in-depth for any price-list-derived defaults on new rows).
  - **Class B** — qty-diff branch sets `soi.flags.ignore_validate_update_after_submit = True` BEFORE `soi.qty = cq`. The save block sets the same flag on the parent SO when `qty_updated` is non-empty. This is the exact same escape hatch ERPNext's own `update_child_qty_rate` uses internally — see `accounts_controller.py:4158` (child flag) and `accounts_controller.py:4167` (parent flag). The parent flag short-circuits `validate_update_after_submit` (document.py:1098-1101) before it can iterate children and hit the per-field check.

### Why not `update_child_qty_rate`

We considered wrapping ERPNext's whitelisted `update_child_qty_rate(parent_doctype, trans_items, parent_doctype_name)` (accounts_controller.py:3848) for Class B. Two reasons against:

1. **Full-replace contract**: `validate_and_delete_children` (accounts_controller.py:3823-3845, called on line 4004) iterates `parent.items` and DELETES any item whose `name` is not in `trans_items`. We'd need to enumerate every existing SOI (changed + unchanged) — a refactor that bloats the reconcile contract.
2. **Side-effect blast radius**: post-mutation, the function runs `make_packing_list`, `set_gross_profit`, `validate_selling_price`, `validate_warehouse`, `update_reserved_qty`, `update_project`, `update_delivery_status`, `update_blanket_order`, `update_billing_percentage`, `set_status`, `check_credit_limit`, `validate_approving_authority`, and stock-reservation cancel+recreate (accounts_controller.py:4166-4255). Any of those may throw in webhook context against a historical SO with stale customer balance / reservation state. Trading 28+9 known failures for an unbounded set of fresh side-effect failures is a bad deal.

The flag itself IS the blessed primitive; `update_child_qty_rate` is just a heavy wrapper around it. We use the primitive directly.

### Tests

12 new tests in `tests/test_connector_patches.py` (`TestV136Hotpatch*` suites):

- 7 source-invariants (Class A: `set_missing_values()` called, `calculate_taxes_and_totals()` called before save, set_missing_values precedes calculate_taxes; Class B: child flag set before qty mutation, parent flag set when qty_updated, parent flag precedes save; version bumped to 1.3.6).
- 5 behavioural unit tests via inline `_reconcile_so_line_items_v136` helper (qty bump sets child flag, qty bump sets parent flag, qty no-op sets no flags, add-only doesn't set parent qty-flag (idempotency check on the flag setter being gated on qty_updated, not added), partial refund still sets both flags).

Total: 162 tests (150 baseline + 12 new), 0 regressions. v1.3.5 source-invariant tests (6) still pass — Class A and B additions are pure-insertion (don't change the existing assignments matched by v135 regexes).

### Reversibility

Fully reversible by reverting this commit + redeploying yei-v1.3.5. Both fixes are pure-additive at runtime:
- Class A adds two method calls before save; both are idempotent on a submitted SO with complete data.
- Class B adds two flag sets; flags are ephemeral (in-memory only, cleared between request cycles).

Rollback path: `git revert <commit>` + Press revert-candidate.

### Refs

- Phase 4 EXECUTION-PAUSE-2026-05-14T01-50Z.md (yel/RX root-cause exhibit)
- accounts_controller.py:3849 (update_child_qty_rate signature + side-effect chain)
- accounts_controller.py:4158, 4167 (the blessed flag pattern this patch mirrors)
- selling_controller.py:222 (the `sum(item.base_net_amount ...)` crash site)
- base_document.py:1259-1294 (per-field allow_on_submit validation)
- taxes_and_totals.py:167-243 + 245-251 (the base_net_amount population path)

---

## [yei-v1.3.5] — 2026-05-14

**Qty-diff branch in `_reconcile_so_line_items` — closes 3 of 6 Baumera audit `quantity_mismatch` findings.** Companion to ygf-v0.5.3 (post-submit DN cascade hook). Trigger: 2026-05-13 Baumera audit surfaced 6 `quantity_mismatch` rows that broke down into four distinct bugs (see `stages/04c-data-sync/working/supplier-sheet-3-issues-2026-05-12/QUANTITY-MISMATCH-INVESTIGATION-2026-05-14.md`). The primary bug, addressed here, accounts for #2962, #4416, #4485.

### Why

`_reconcile_so_line_items` (yei-v1.3.3) handles the **add** (new lid not on SO → append) + **full refund** (cq==0 → flag) branches but **silently `continue`s** when a Shopify line is already on the SO via matching `shopify_line_item_id`. That means:

- **Free-gift cart-promo bumps** (1→2, 2→4 on the same lid) — silently ignored. Symptom: #2962 AUTO 1→2, #4416 AUTO 2→4.
- **Partial refunds where `current_quantity > 0`** (Shopify reduces qty from N to M, M>0) — silently ignored because the refund branch only fires on `current_quantity == 0`. Symptom: #4485 partial refund of 1 unit of 3.

There's a symmetry break between SO-create (`get_order_items` reads `current_quantity` authoritatively) and SO-edit (`_reconcile_so_line_items` only handles add/remove, never qty changes on existing lines).

### Code changes

- [`shopify/order.py:_reconcile_so_line_items`](ecommerce_integrations/shopify/order.py) — Insert qty-diff branch on the existing-lid path. When `lid in existing_by_lid` AND `cint(soi.qty) != cq`, assign `soi.qty = cq` and record the lid in `qty_updated`. Return shape changes from `(added, refunded)` to `(added, refunded, qty_updated)`. Save guard at line 1024 now fires when `added` OR `qty_updated` is non-empty (previously only `added`). Property Setter `Sales Order Item.allow_on_submit=1` (installed by `add_so_item_allow_on_submit` patch in v1.3.3) permits the post-submit `qty` write — no new schema.
- [`shopify/order.py:handle_order_edited`](ecommerce_integrations/shopify/order.py) — Adapt caller at line 874 to unpack the new three-tuple. ToDo description and EIL Success message now include the qty-updated count alongside added + refund-flagged counts. No semantic change for downstream callers — the ToDo body still renders; just one more counter.

No changes needed at `replay_handle_order_edited` (line 959) — it delegates to `handle_order_edited` and doesn't see the return shape.

### Tests

9 new tests in `tests/test_connector_patches.py` (`TestV135QtyDiffBranch*` suites):

- 6 source-invariants on `_reconcile_so_line_items` (qty_updated list initialised, qty-diff guard regex, soi.qty assignment present, qty_updated.append(lid) present, save fires on `added or qty_updated`, return shape is three-tuple, caller unpacks three-tuple).
- 3 behavioural unit tests (inline-copy of the reconcile logic, matching the file's testing pattern):
  1. Existing SOI lid=X qty=1 + Shopify cq=2 → SOI qty=2, qty_updated contains lid.
  2. Existing SOI lid=X qty=3 + Shopify quantity=3 current_quantity=2 → SOI qty=2, refunded stays empty (partial, not full refund), qty_updated contains lid.
  3. Existing SOI lid=X qty=2 + Shopify cq=2 → no-op (qty_updated empty), idempotency check.

Total: 150 tests in `test_connector_patches.py` (141 baseline + 9 new), 0 regressions. Existing test_supplier_sheet_3_issues.py (65 tests), test_b24_refunds.py (57 tests), test_freight_class.py (39 tests) all still pass.

### Resolves

3 of 6 Baumera 2026-05-13 audit `quantity_mismatch` findings:

| # | Shopify # | Symptom | Resolved by v1.3.5 |
|---|---|---|---|
| 1 | #2962 | AUTO Shopify=2, SO/DN=1, same lid | YES — free-gift bump now propagates |
| 4 | #4416 | AUTO Shopify=4, SO/DN=2, same lid | YES — same pattern (likely 2→4) |
| 5 | #4485 | AUTO Shopify quantity=3 current=2, SO/DN=3 | YES — partial refund now propagates |

The remaining 3 findings (#3532 missing AUTO line, #4131 + #4744 DN-cascade gap) are addressed by ygf-v0.5.3 + backfill harness updates landing in companion changes.

### Reversibility

Fully reversible by reverting this commit + redeploying yei-v1.3.4 (candidate `8nvvdi4fdh`). The new branch is pure-additive: idempotent (qty_updated stays empty when `cint(soi.qty) == cq`) and gated behind the existing `lid in existing_by_lid` membership check. Rollback path is `git revert` + Press revert-candidate.

---

## [yei-v1.3.4] — 2026-05-13

**Phase-2.5 micro-release — admin replay wrapper for ``handle_order_edited``.** Companion to yei-v1.3.3 Phase-2 backfill. Trigger: Phase-2 backfill recovered 44 of 136 historical EIL Invalid rows; the remaining 88 ``replay_failed`` because ``handle_order_edited`` is a webhook handler (not @frappe.whitelist'd) and ``frappe.client.insert`` of child rows on submitted parents returned 417 EXPECTATION FAILED even with the v1.3.3 Property Setter.

### Why

The clean path is a thin admin-callable wrapper that delegates to the existing handler. The Property Setter ``Sales Order.items.allow_on_submit=1`` enables server-side ``sales_order.append("items", ...).save()`` (which ``_reconcile_so_line_items`` already uses correctly) — it does NOT enable arbitrary REST inserts onto child tables of submitted parents. So we let the existing reconcile path do its job; we just need to be able to invoke it admin-side.

### Code changes

- [`shopify/order.py:replay_handle_order_edited`](ecommerce_integrations/shopify/order.py) — NEW `@frappe.whitelist()` + `@temp_shopify_session` wrapper. Builds a synthetic ``{"order_edit": {"order_id": str(...)}}`` payload and delegates to ``handle_order_edited``. Permission gate: System Manager role or session user = Administrator (bypasses HMAC validation that ``_validate_request`` does for live webhooks; admin-role is the explicit trust boundary). ``@temp_shopify_session`` establishes the Shopify auth session so the handler's ``Order.find()`` REST call succeeds — live webhook flow gets its session via ``_validate_request`` (in the webhook entry point), which the wrapper bypasses. ``request_id`` is passed through as-is — if ``None``, ``create_shopify_log`` creates a fresh EIL row; forging a fake value breaks the downstream lookup with ``DoesNotExistError``. No new doctypes, no new Custom Fields, no Property Setters — pure code addition.
- [`shopify/order.py:_build_soi_from_shopify_line`](ecommerce_integrations/shopify/order.py) — yei-v1.3.3 latent bug surfaced by v1.3.4 backfill: ``append+save`` on a submitted parent SO does NOT autofill ``uom`` + ``conversion_factor`` the way fresh-doc insert does (the validate hook that copies them from ``Item.stock_uom`` is bypassed under the allow_on_submit code path). Set ``uom = stock_uom`` and ``conversion_factor = 1.0`` explicitly. Closes "Value missing for: UOM" / "Value missing for: UOM Conversion Factor" errors that were causing the reconciler to fail silently (the handler caught the exception in ``except Exception``; the row never inserted but `replay_handle_order_edited` returned `{}` to the caller).

### Tests

13 new tests in `tests/test_supplier_sheet_3_issues.py` (`TestV134*` suite + 1 in `TestV133HandleOrderEditedSourceInvariant`):

- 7 source-invariants on `replay_handle_order_edited` (function defined, @frappe.whitelist + @temp_shopify_session stack, dedicated @temp_shopify_session check, System Manager / Administrator gate, synthetic order_edit payload shape, delegates to handle_order_edited not duplicated logic, request_id passthrough not forged).
- 5 behavioural unit tests (admin check passes for SM / Administrator, blocks regular user; synthetic payload extraction matches handle_order_edited's logic; idempotent no-op for in-sync SO).
- 1 source-invariant on `_build_soi_from_shopify_line` locking in `uom` + `conversion_factor` explicit set.

Total: 302 yei tests (289 baseline + 13 new), 0 regressions.

### Backfill (workspace, post-deploy)

`scripts/backfill_eil_v134_replay.py` — NEW. Filters the Phase-2 jsonl for `event=replay_failed` events on SOs with `transaction_date >= 2026-04-01` AND `docstatus=1`; calls ``replay_handle_order_edited`` via the admin token; per-SO verification (item count delta + line_id stamping on new SOIs). Logs to `data/backfills/backfill_eil_v134_replay.jsonl`.

### Reversibility

Fully reversible by reverting this commit + redeploying yei-v1.3.3. The wrapper is admin-only and additive (zero impact on live webhook flow, which never touches it). Rollback path is `git revert` the commit.

---

## [yei-v1.3.3] — 2026-05-13

**Baumera zero-errors Phase 2 — 4-bug bundle + ``Sales Order Item.allow_on_submit`` PS.** Companion to ygf-v0.5.2. Trigger: EIL audit 2026-05-13 surfaced 136 ``handle_order_edited`` silent line-drops + three smaller error clusters. See ERPNext workspace `stages/04c-data-sync/working/supplier-sheet-3-issues-2026-05-12/YEI-EIL-AUDIT-AND-PATCH-PLAN.md`.

### Why

| # | Symptom | EIL count |
|---|---|---|
| 1 | ``orders/edited`` webhook silently dropped every Shopify line addition since 2026-04-17 (read `payload.get("id")` but the payload nests order_id under `payload["order_edit"]["order_id"]`) | 136 historical Invalid rows |
| 2 | ``cancel_order`` mis-classified ``TimestampMismatchError`` as the AWB-blocking case (`frappe.db.set_value` bumped SO.modified between read and `.cancel()`) | 8 today |
| 3 | ``freight_class.py`` `Product.find(product_id)` passed an int → Shopify HTTP 400 "expected String to be a id" | 18 historical |

### Schema bump (metadata only — zero new Custom Fields)

One Property Setter installed by `add_so_item_allow_on_submit` patch:

- `Sales Order.items` (the Table field) → `allow_on_submit=1`. Required so the new ``_reconcile_so_line_items`` helper can ``sales_order.append("items", ...)`` + ``save()`` on submitted SOs. Native precedent: yei-v1.3.2 `Sales Order.shipping_address_name allow_on_submit` Property Setter.

### Code changes

- [`shopify/order.py:handle_order_edited`](ecommerce_integrations/shopify/order.py) — Patch 1: extract order_id from `payload["order_edit"]["order_id"]` (defensive fallback to top-level `id`). Replace ToDo-only body with line-item reconciliation: fetch full order via Shopify REST, diff against `sales_order.items` by `shopify_line_item_id`, append missing SOIs, flag `current_quantity==0` lines via the existing `flag_so_item_refunded` helper. Preserves the audit-trail ToDo as a non-load-bearing trace. Closes 136 EIL Invalid rows.
- [`shopify/order.py:_reconcile_so_line_items`](ecommerce_integrations/shopify/order.py) — NEW helper (~70 LOC). Idempotent membership check by `shopify_line_item_id`; existing SOIs skipped; new SOIs built via `_build_soi_from_shopify_line`.
- [`shopify/order.py:_build_soi_from_shopify_line`](ecommerce_integrations/shopify/order.py) — NEW helper (~40 LOC). Mirrors per-row logic in `get_order_items` (B12 / B15 / B24b semantics) for a single Shopify line. Stamps `shopify_line_item_id` so subsequent reconciles see the new row as already-present.
- [`shopify/order.py:cancel_order`](ecommerce_integrations/shopify/order.py) — Patch 3a: `frappe.db.set_value(..., update_modified=False)` on the ORDER_STATUS_FIELD write so the in-memory `sales_order` doc doesn't go stale before `.cancel()`. Patch 3b: tighten the `frappe.ValidationError` except clause to match on `"FedEx AWB" in str(e)`; non-AWB ValidationErrors (including residual TimestampMismatch) re-raise to the outer Exception handler for accurate diagnostics rather than the misleading "manual intervention required" label. Closes 8 mis-classified EIL Error rows.
- [`shopify/freight_class.py:make_live_fetcher`](ecommerce_integrations/shopify/freight_class.py) — Patch 4: `Product.find(str(product_id))` to satisfy pyactiveresource string-id requirement. Closes 18 sync_sales_order Shopify-400 errors. Defensive companion at line 174 (`Order.find(str(shopify_order_id))`).
- [`patches/add_so_item_allow_on_submit.py`](ecommerce_integrations/patches/add_so_item_allow_on_submit.py) — NEW. Single `execute()` calling `frappe.make_property_setter` on `Sales Order.items` table field. Idempotent.
- [`patches.txt`](ecommerce_integrations/patches.txt) — registers the new patch.

### Tests

23 new tests in `tests/test_supplier_sheet_3_issues.py` (`TestV133*` suite):

- 9 source-invariants on `handle_order_edited` + `_reconcile_so_line_items` + `_build_soi_from_shopify_line` (order_id extraction, reconcile helper present, line-id-based diff, refund flag call, save() flush, line_id stamping).
- 5 behavioural unit tests on the order_id extraction logic (nested key, top-level fallback, empty payload, non-dict payload, idempotent no-op reconcile).
- 3 source-invariants on `cancel_order` (update_modified=False, FedEx-AWB match in except, non-AWB re-raises).
- 2 source-invariants on `freight_class.py` (str(product_id), str(shopify_order_id)).
- 4 source-invariants on the `add_so_item_allow_on_submit` Property Setter patch.

Total: 289 yei tests (266 baseline + 23 new), 0 regressions.

### Backfill (workspace, post-deploy)

`scripts/backfill_eil_handle_order_edited.py` — NEW. One-shot replay of 136 historical EIL `handle_order_edited` Invalid rows. Per row: extract `order_edit.order_id` from `request_data`; find the SO; fetch full Shopify order; diff lines by `shopify_line_item_id`; (a) flag `current_quantity==0` matches as refunded via `frappe.client.set_value`; (b) delegate additions to the deployed `handle_order_edited` whitelisted method (single source of truth post-deploy). Idempotent (line-id diff is the membership check). Logs to `data/backfills/backfill_eil_handle_order_edited.jsonl`.

### Reversibility

Fully reversible by reverting this commit + redeploying yei-v1.3.2. The Property Setter is metadata-only — no schema mutation. The reconcile helper only adds rows on submitted SOs; rollback path is `frappe.client.delete` on any SOIs stamped with a Shopify line_item_id but absent from the legacy code path (none expected since pre-fix never reconciled).

### Deploy

Bundled with ygf-v0.5.2 in one Press candidate. Defensive pin: frappe `3j5g13nk6q` (16.16.0) + erpnext `a1nmobnt0o` (16.15.1) to current_release. The install patch runs as part of bench migrate.

---

## [yei-v1.3.2] — 2026-05-13

**Issue #1 backfill unblocker.** Companion to no-ygf-change (ygf stays on `0.5.1`). Single-purpose release: install a Property Setter relaxing `allow_on_submit` on the native `Sales Order.shipping_address_name` field, so the Issue #1 backfill (and any future SO-side address-link mutations) can write on submitted SOs without `UpdateAfterSubmitError`.

### Why

The v1.3.1 Issue #1 backfill attempted to CREATE a new per-order Address record + repoint `Sales Order.shipping_address_name` at it. The second step raised `frappe.exceptions.UpdateAfterSubmitError` because the native Frappe field has `allow_on_submit=0`. All 33 candidates errored on apply — 24 docstatus=1 (blocked by allow_on_submit) and 9 docstatus=2 (blocked by docstatus). 33 orphan Address records left behind (harmless; no SO points at them).

Research design at `ERPNext/stages/04c-data-sync/working/supplier-sheet-3-issues-2026-05-12/RESEARCH-v1.3.2-unblocker.md` recommended Option B (edit-in-place on the existing Address record — non-submittable, works for both docstatus scopes) PLUS Option A (Property Setter) as defensive backstop for the <5% non-unique-Address fallback path and for any future SO-side mutation flow. yei-v1.3.2 ships the Property Setter half; the backfill script half lives in the workspace.

### Schema bump

Zero new Custom Fields. Property Setter is metadata-only — no `ALTER TABLE`. Same shape as the `ygh_fedex/patches/relabel_alpha26_dn_lr_fields.py` precedent (alpha26 `lr_no` relabel).

### Code changes

- [`patches/add_so_shipping_address_name_allow_on_submit.py`](ecommerce_integrations/patches/add_so_shipping_address_name_allow_on_submit.py) — NEW. Single `execute()` calling `frappe.make_property_setter({"doctype":"Sales Order", "fieldname":"shipping_address_name", "property":"allow_on_submit", "value":"1", "property_type":"Check"})` + `frappe.clear_cache(doctype="Sales Order")`. Idempotent (`make_property_setter` upserts by `(doctype, fieldname, property)`).
- [`patches.txt`](ecommerce_integrations/patches.txt) — registers the new patch in the v1_3 section.

### Tests

4 new tests in `tests/test_supplier_sheet_3_issues.py` (`TestV132PropertySetterUnblocker`):

- `test_patch_file_exists` — patch module present at expected path.
- `test_patch_registered_in_patches_txt` — patches.txt picks it up at migrate time.
- `test_patch_calls_make_property_setter_on_correct_field` — target = `Sales Order.shipping_address_name.allow_on_submit`, property_type = `Check`.
- `test_patch_clears_cache_after_mutation` — `clear_cache(doctype="Sales Order")` is called so the new metadata takes effect immediately.

Total: 266 yei tests (262 baseline → 266), 0 regressions.

### Backfill (workspace, post-deploy)

`scripts/backfill_issue1_per_order_addresses_v132.py` — NEW. Pivots from "create new Address + repoint SO" to "edit-in-place on the existing Address record". Walks Shopify-sourced SOs at docstatus=1 OR docstatus=2 with a populated `shipping_address_name`; for each, compares the current Address fields to Shopify's `shipping_address.first_name + " " + last_name` (plus all geographic + phone fields); pre-checks uniqueness (`get_count` on Sales Order + Delivery Note linking to the Address must equal 1 SO and ≤ own DN count); writes only the differing fields via `frappe.client.set_value` on the Address doctype (non-submittable — works for both docstatus scopes); idempotent. Does NOT update `Sales Order.shipping_address_name` (avoids the allow_on_submit issue entirely). Does NOT create new Address records (avoids the 33-orphan trap).

### Deploy

Single Press candidate for yei-v1.3.2 alone (ygf untouched). Defensive pin: frappe `3j5g13nk6q` (16.16.0) + erpnext `a1nmobnt0o` (16.15.1) + ygf `5s2aoo5a4b` (0.5.1) to current_release.

---

## [yei-v1.3.1] — 2026-05-13

**Supplier-sheet 3-issue fix bundle (Issues #1, #2, #3).** Companion to ygf-v0.5.1. Trigger: Baumera-YGH + Palmako sheet audits, plus the 2026-05-13 Preorder investigation that reframed Issue #2's root cause from "preorder filter miss" to "refund-match SKU translation bug + B5 line-id capture broken in production."

### Why

Three correctness gaps on the supplier sheets:

| # | Symptom | Audit count |
|---|---|---|
| 1 | Sheet col D shows payer name, not order-level ship-to recipient | ~106 Baumera + ~23 Palmako |
| 2 | Refund flags never landed on past Baumera refunds (`_match_so_item` did naive item_code equality, but yei translates Shopify SKU → ERPNext item_code at sync time) | Every translated-SKU refund silently failed; ~150-300 historical rows in scope |
| 3 | Sheet col A stays "New" after Shopify cancellation (`cancel_order` guards suppressed `.cancel()` whenever a DN existed) | 19 Baumera + 17 Palmako-mirror |

### Schema bump (Fork A — only schema authorization granted this cycle)

Two new Custom Fields installed by `add_shopify_line_item_id_fields` patch:

- `Sales Order Item.shopify_line_item_id` — Data, read_only=1, allow_on_submit=1.
- `Delivery Note Item.shopify_line_item_id` — Data, read_only=1, allow_on_submit=1.

No native ERPNext / Frappe field encodes a Shopify GraphQL `LineItem.id`; integration-specific identifier (same shape as the existing `shopify_order_id` + `shopify_address_id` fields).

### Code changes

- [`shopify/order.py:create_sales_order`](ecommerce_integrations/shopify/order.py) — Issue #1: inline per-order shipping `Address` creation from `shopify_order["shipping_address"]`. `address_title = shipping_address.first_name + " " + last_name` (recipient name, not payer). `address_type="Shipping"`, linked to Customer. Helper `_create_per_order_shipping_address` near `_resolve_currency`. SO is created with `shipping_address_name = <new addr>` overriding ERPNext's default-to-customer-Billing.
- [`shopify/order.py:get_order_items`](ecommerce_integrations/shopify/order.py) — Issue #2 B5 fix: stamps `shopify_line_item_id` on every SO Item from `line_item.id`. Stored as string (Shopify ids are 64-bit, exceed JS-safe integer).
- [`shopify/order.py:cancel_order`](ecommerce_integrations/shopify/order.py) — Issue #3: lifts the SI + DN guards (legacy `if not delivery_notes and not sales_invoice` block); always attempts `sales_order.cancel()` on submitted SOs. Catches `frappe.ValidationError` raised by the alpha26 ygf `on_cancel` hook when any linked DN has `lr_no` (AWB minted). Deletes the dead-code writes of `ORDER_STATUS_FIELD` onto Sales Invoice and Delivery Note (zero readers; SI flow disabled).
- [`shopify/refund.py:_match_so_item`](ecommerce_integrations/shopify/refund.py) — Issue #2 core: primary match key is `shopify_line_item_id`; falls back to Shopify-SKU → ERPNext-item_code translation via `product.get_item_code`; raw SKU equality as final fallback. Fixes the silent-Baumera-refund-fail bug.
- [`shopify/refund.py:_match_shopify_line_to_so_item`](ecommerce_integrations/shopify/refund.py) — symmetric rewrite (reverse direction; used by `reconcile_so_against_current_quantity`).
- [`shopify/doctype/shopify_setting/shopify_setting.py:setup_custom_fields`](ecommerce_integrations/shopify/doctype/shopify_setting/shopify_setting.py) — adds two new field specs (SO Item + DN Item).
- [`shopify/constants.py`](ecommerce_integrations/shopify/constants.py) — declares `LINE_ITEM_ID_FIELD = "shopify_line_item_id"`.

### Tests

25 new tests in `tests/test_supplier_sheet_3_issues.py`:

- 6 schema-bump source-invariants (constants + setup_custom_fields + patches.txt + patch file).
- 3 Issue #1 source-invariants (helper defined, reads first/last name, called by create_sales_order).
- 3 Issue #1 behavioural (recipient-name override, fallback when shipping_address absent, fallback when first/last empty).
- 3 Issue #2 source-invariants (line_id stamp in get_order_items, translation in both match functions).
- 4 Issue #2 behavioural (line_id primary match, SKU fallback for legacy rows, no-match returns None, ambiguous-same-rate tie-break).
- 6 Issue #3 source-invariants (guard lifted, .cancel() called unguarded, ValidationError caught, SI dead-write deleted, DN dead-write deleted, SO ORDER_STATUS_FIELD write preserved).

Total: 262 yei tests (237 baseline + 25 new), 0 regressions.

### Backfill (workspace, post-deploy)

- `scripts/backfill_issue1_per_order_addresses.py` — walks SOs whose current `shipping_address_name` points at a Customer-Billing Address; creates a per-order Shipping Address; PUTs SO + every linked DN. ~129 candidate orders.
- `scripts/backfill_issue2_shopify_walk.py` — Phase A populates `shopify_line_item_id` retroactively (~12K SO Items + cascaded DN Items). Phase B walks Shopify for refund signals and PUTs `shopify_refunded` flag via line-id matching. ~150-300 flag writes expected.
- `scripts/backfill_issue3_cancelled_writer_kickoff.py` — triggers `sync_append_only` per manufacturer immediately rather than waiting for the hourly cron tick.

### Reversibility

Code revert restores yei-v1.3.0 behaviour. Custom Field rows can be retired in a future Wave-C-style cleanup once line-id capture is verified (the two fields are append-only diagnostic / match-key data, never deleted by this release).

### Deploy

Bundled with ygf-v0.5.1 in one Press candidate. Frappe + ERPNext pinned to current_release. The install patch runs as part of bench migrate.

---

## [yei-v1.3.0] — 2026-05-12

**Wave B — shipping-classification reader cutover + dead-code purge.** Companion to ygf-v0.5.0. Contract phase of the expand-contract migration that started in yei-v1.2.7 / ygf-v0.4.1.

### Why

Wave A installed new fields beside old + dual-wrote. Wave A backfill landed (6,299 mutations PASS, idempotent re-run confirmed). Wave B flips every reader to the new field set and stops writing the old. Legacy fields stay in DB (`shopify_freight_class`, `shopify_shipping_method`, `shopify_tags`) — Wave C deletes them once observation gates pass.

### Code changes — writers

- [`shopify/order.py:create_sales_order`](ecommerce_integrations/shopify/order.py) — drops `FREIGHT_CLASS_FIELD` writes at SO header + per SO Item. Drops `ORDER_ITEM_SHIPPING_METHOD_FIELD` writer at the SOI builder. Only `SO_SHIP_CLASS_FIELD` (SO header) + `ITEM_SHIP_METHOD_FIELD` (per SOI, with `ship-` prefix) are written.
- [`shopify/freight_class.py:recompute_for_so`](ecommerce_integrations/shopify/freight_class.py) — drops dual-writes of `shopify_freight_class` at SO + SOI + draft-DN cascade. Only `so_ship_class` / `item_ship_method` / `dn_ship_method` written.

### Code changes — readers

- [`shopify/order.py:create_sales_order`](ecommerce_integrations/shopify/order.py) — B16 has_dropship detection flips from `ORDER_ITEM_SHIPPING_METHOD_FIELD == "ship-dropship"` to `ITEM_SHIP_METHOD_FIELD == "ship-dropship"`. The dropship signal now lives on a single field instead of two.

### Code deletions

- `shopify/order.py:_resolve_shipping_method` — ~35 LOC. Reader of `Item.shopify_tags`; obsolete post-cutover because `item_ship_method` is the live-fetched source of truth. Test surrogates in `test_connector_patches.py` are independent reimplementations and remain.

### Tests

4 tests in `test_freight_class.py::TestWaveBSingleWriteRecomputeForSO` (renamed from `TestWaveADualWriteRecomputeForSO`):

- `test_so_header_writes_new_field_only` — asserts `so_ship_class` IS written, `shopify_freight_class` is NOT.
- `test_soi_writes_item_ship_method_with_prefix` — same shape on SOI with `ship-` prefix.
- `test_cascade_writes_dn_ship_method_only` — same shape on DN cascade.
- `test_no_cascade_when_rollup_is_split_or_dropship` — unchanged behavior; rollups still don't cascade.

Total: 39 freight_class tests pass. `test_connector_patches` 141/141 and `test_b24_refunds` 57/57 unchanged.

### Reversibility

Fully reversible by reverting this commit + redeploying yei-v1.2.7. Wave A's dual-write left legacy fields populated; reverting code makes the readers + writers point at them again.

### Deploy sequence

Bundled with ygf-v0.5.0. Press deploy pin Frappe + ERPNext to current_release. No data mutation (Wave B is code-only).

---

## [yei-v1.2.7] — 2026-05-12

**Wave A — shipping-classification field consolidation (additive expand phase).** Companion to ygf-v0.4.1. Part of the multi-wave consolidation of shipping-class fields across yei + ygf + workspace (see ERPNext workspace `stages/04c-data-sync/references/shipping-fields-consolidation-2026-05-12.md` for the full plan).

### Why

Pre-consolidation surface had 9 shipping-classification fields scattered across SO, SO Item, DN, Item, Manufacturer with overlapping semantics and a 7-anomaly bug class where Shopify retag/recompute updated the SO but not the linked draft DN. The end-state schema collapses to 4 (one per doctype) with cleaner vocabularies per axis. Wave A is the expand phase: install new fields beside old, dual-write, leave readers on old. Wave B cuts readers over; Wave C drops old fields.

### Schema additions (`add_wave_a_shipping_fields` patch)

- `Sales Order.so_ship_class` — Select `air|sea|dropship|split|""`, `allow_on_submit=1`, anchored after `shopify_freight_class`.
- `Sales Order Item.item_ship_method` — Select `ship-air|ship-sea|ship-dropship|""`, `allow_on_submit=1`, anchored after `shopify_freight_class`. Value carries the raw Shopify-tag form WITH the `ship-` prefix (visually distinct from the bare-class rollup at SO level).

### Production dual-writes

- [`shopify/order.py:create_sales_order`](ecommerce_integrations/shopify/order.py) — every SO sync from a Shopify webhook now stamps both legacy `shopify_freight_class` AND new `so_ship_class` at the SO header; per-line writes both `shopify_freight_class` (bare) AND `item_ship_method` (with `ship-` prefix).
- [`shopify/freight_class.py:recompute_for_so`](ecommerce_integrations/shopify/freight_class.py) — manual/backfill recompute path now dual-writes at SO + per SO Item; additionally cascades to any draft Delivery Notes linked to the SO writing both `shopify_freight_class` and `dn_ship_method`. The B25 cascade was the structural fix for the 7-anomaly bug class — pre-Wave-A, a retag on a Shopify product would update the SO but leave the linked draft DN stale.

### Observability

- `freight_class.py:resolve_for_order` (line 122) and `freight_class.py:make_live_fetcher` (line 224) — bare `except Exception` clauses now call `frappe.log_error()`. Previously silent swallow during Shopify-API hiccups; surface-level symptom was DNs with empty `shopify_freight_class` that nobody could trace.

### Tests

4 new tests in `test_freight_class.py::TestWaveADualWriteRecomputeForSO`:
- `test_dual_writes_so_header_old_and_new` — SO.shopify_freight_class AND SO.so_ship_class both written.
- `test_dual_writes_soi_with_value_transform` — bare class on legacy field; `ship-` prefix on new field.
- `test_cascade_dual_writes_to_draft_dn` — draft DN linked to SO gets both shopify_freight_class + dn_ship_method.
- `test_no_cascade_when_rollup_is_split_or_dropship` — split rollups don't trigger DN cascade (DN-level value comes from split.py bucket, not the SO rollup).

Total: 39 freight_class tests (35 baseline + 4 new). All passing. `test_connector_patches` 141/141 and `test_b24_refunds` 57/57 unchanged.

### Backfill (deferred to post-deploy)

Workspace `scripts/backfill_shipping_consolidation_wave_a.py` ports the legacy data into the new fields. SO identity copy (~2266 rows scope), SO Item with value transform (~6000+ rows), draft DN identity copy (2235 rows). Idempotent — re-run yields ok=0. Awaits Press deploy of yei + ygf Wave A first.

### Deploy sequence

Standard yei + ygf deploy ceremony (both apps share Wave A on the same bench bump). Awaits user greenlight per `feedback_no_deploy_without_greenlight.md`.

---

## [yei-v1.2.6] — 2026-05-12

**B24 — refund handling (no-amend, flag-only model).** Triggered by Shopify orders #2993 + #4039 + a 478-order historical refund backlog ERPNext had never seen. The connector previously read `line_items[].quantity` (creation-time) and ignored `current_quantity` (post-refund authoritative) plus the entire `refunds[]` array.

**What ships:**

1. **`refunds/create` webhook subscribed** → new `ecommerce_integrations/shopify/refund.py`. Idempotent via ToDo-lookup (re-fires of the same `refund_id` short-circuit).
2. **No-amend dispatch.** Refund webhook flags `Sales Order Item.shopify_refunded=1` + `shopify_refunded_at=refunds[].created_at`. When a `Delivery Note` exists for the SO, mirrors the flag to `Delivery Note Item.shopify_refunded=1` via `so_detail`. SO and DN docstatus are never amended; `frappe.copy_doc` and `.cancel()` are source-invariant absent from `refund.py`.
3. **Restock-type-aware ToDo.** One native `ToDo` per refund event with refund_id in description (idempotency key). Title varies by `restock_type` (`no_restock` / `return` / `cancel` / `legacy_restock` / shipping-only). Priority `High` if refund total ≥ $1,000 else `Medium`.
4. **`current_quantity` flip at 4 `order.py` call sites** (`_separate_tips:335`, `get_order_items:377`, `_get_item_price:477`, `_build_order_edit_diff:801,811`). Lines with `current_quantity == 0` are skipped entirely in `get_order_items` — new orders never insert SO Item rows for already-refunded-at-creation lines. Fallback to `quantity` for older API responses.
5. **3 `current_*` Currency Custom Fields on Sales Order** (`shopify_current_subtotal_price`, `shopify_current_total_price`, `shopify_current_total_discounts`). Drift indicators populated by `populate_current_totals()` from any `orders/*` or `refunds/*` event.
6. **2 SO Item + 2 DN Item Custom Fields** (`shopify_refunded` Check + `shopify_refunded_at` Datetime, both `allow_on_submit=1`). yei now installs Custom Fields on `Delivery Note Item` — previously yei only touched `Delivery Note` itself. Mirror flag propagates SO Item → DN Item at refund time.
7. **`reconcile_so_against_current_quantity(so_name, dry_run=False)` backfill helper.** Per-SO compare of ERPNext qty vs Shopify `current_quantity`; flags drifted lines with a synthetic refund (`refund_id=reconcile-<so>`). Phase 1 (non-destructive `populate_current_totals` over 478 historical SOs) and Phase 2 (flagging) run post-deploy with `dry_run=True` gate + 50/run hard limit + decision-register entry before non-dry-run.

**Stage 04d / ygh_fedex coupling.** `Delivery Note Item.shopify_refunded_at` is read by ygf's supplier-sheet writer (v0.4.x, not in this release) to prefix descriptor cells with `CANCELLED YYYY-MM-DD: …` on the manufacturer Google Sheet. Deploy order at v0.4.x time: ygf first (writer ready), then yei (starts setting flags). Until ygf-v0.4.x ships, the SO/DN flags are still useful in the ERPNext UI but the supplier sheet won't reflect them.

**Test coverage.** 57 new standalone tests in `tests/test_b24_refunds.py`:
- Source-invariants against the production `order.py` and `refund.py` files (no bare `quantity` reads in 4 functions; no `copy_doc` / `.cancel()` / `.submit()` in `refund.py`).
- Pure-helper unit tests: restock_type → ToDo title mapping; ToDo priority threshold.
- Behavioral (Frappe-mocked) tests on `apply_refund`: no copy_doc, flag wiring, draft-SO qty-reduction branch, Comment with refund_id, populate_current_totals invocation.
- Idempotency tests on `handle_refund_created` (existing-ToDo short-circuit; missing-order short-circuit; missing-order-id short-circuit).

Baseline preserved: 141 + 35 prior tests still pass; no regressions.

**B7 self-heal deferred** to a later v1.2.x release. Original design bundled B7+B24 into `yei-v1.3.0`; B24 ships solo as v1.2.6 to keep the v1.2.x patch rhythm and to let B7 land in its own audited release.

**Post-deploy steps** (not part of this tag):
- Register `refunds/create` via Shopify Admin API (one-off).
- Phase 1 backfill: `populate_current_totals` over 478 historical SOs (non-destructive).
- Phase 2 backfill: `reconcile_so_against_current_quantity` dry-run gate per SO, then non-dry-run with 50/run hard limit + decision-register entry first.
- Smoke test: mint a test refund on a low-value SO with submitted DN; verify chain.

Design refs: `stages/04c-data-sync/references/b24-impl-removed-items.md`, `b24-refunds-and-current-quantity.md`, `stages/04d-exports/references/supplier-sheet-cancel-mark.md`.

---

## [yei-v1.2.5] — 2026-05-09

**Hotfix on top of 1.2.4.** Two follow-ups:

1. **`shopify_freight_class` fields gain `allow_on_submit=1`** on both `Sales Order` and `Sales Order Item`. 1.2.4 created them with default `allow_on_submit=0`, which blocked the workspace backfill from writing to already-submitted SOs (`UpdateAfterSubmitError: Not allowed to change Shopify Freight Class after submission`). Real-time webhook path was unaffected (writes to SO during draft state pre-submit). Patched in place via new `update_freight_class_allow_on_submit` migration that re-runs `setup_custom_fields()`.

2. **`recompute_for_so` whitelisted** so workspace scripts can call it via REST. Same body as 1.2.4; just adds `@frappe.whitelist()` with a graceful no-op fallback when frappe isn't importable (test env). 35 unit tests still pass.

`__version__` bumped 1.2.4 → 1.2.5.

## [yei-v1.2.4] — 2026-05-09

**Target bench:** bench-37067
**Branch:** `version-16`
**Deployed:** 2026-05-09 (pending — written ahead of deploy)

### Added — B23: Shopify freight class on every SO + SO Item

Pure metadata addition. New per-line + order-rollup field derived from the Shopify product's tags, computed live at sync time. Replaces the implicit, brittle dependency on `Item.shopify_tags` (which is stale for any Item not linked via `Ecommerce Item` — historically a long tail of legacy SKU-coded Items).

**Why now.** ygf supplier-sheet writer (alpha51 era) reads `Item.shopify_tags` to drive Baumera-YGH col J highlighting. For the 18 Baumera greenhouse Items registered via master CSV import (no `Ecommerce Item` link), B5 product-update webhooks never reached them, so a 2025-Q4 Shopify retag from `ship-sea` to `ship-air` left the Item field stuck at `ship-sea`. Net effect: 59 Baumera-YGH rows show `ship-sea` in col J today even though their Shopify products are tagged `ship-air`. Stamping a per-line classification on the SO at sync time, sourced live from `Product.tags`, eliminates the staleness vector entirely.

**New fields** (Custom Field, idempotent setup via `add_shopify_freight_class` patch):

| Doctype | Fieldname | Type | Options |
|---|---|---|---|
| Sales Order Item | `shopify_freight_class` | Select | `air` \| `sea` \| `dropship` \| `""` |
| Sales Order | `shopify_freight_class` | Select | `air` \| `sea` \| `dropship` \| `split` \| `""` |

`split` (SO-level only) signals that an order has both `air` and `sea` lines — used by the ygf DN-split grouper (alpha52, separate release) to materialize one DN per `(manufacturer, freight_class)` tuple.

**New module** `ecommerce_integrations/shopify/freight_class.py`:

- `classify_product_tags(tags) -> str` — pure mapper, first matching `ship-*` tag wins. Tested across list/string/None/case/whitespace inputs.
- `rollup_so(line_classes) -> str` — aggregates per-line classes to the SO-level value. `air`+`sea` ⇒ `split`; `air`+`dropship` ⇒ `air` (dropship lines skip DN, so the operational class is what's left); all-blank ⇒ `""`.
- `resolve_for_order(order, fetcher)` — loops the order's `line_items[].product_id`, calls the fetcher, returns `(per_line_classes, rollup)`. Pure — caller supplies the fetcher (testable without Shopify API).
- `make_live_fetcher()` — Shopify-API-backed fetcher with a per-call cache so multi-line orders don't double-hit the API for the same product.
- `recompute_for_so(so_name)` — admin/recovery entry point. Re-fetches the original Shopify order via stored `shopify_order_id`, re-classifies every line, writes through `frappe.db.set_value` (no doc reload, no version bump). Independent of `Ecommerce Item` linkage state — used by the workspace backfill script for the existing ~2k SOs.

**Hook** in [`order.py::create_sales_order`](https://github.com/milky-claw/ecommerce_integrations/blob/version-16/ecommerce_integrations/shopify/order.py): after `get_order_items` returns, call `resolve_for_order(...)` once with a fresh fetcher (cache scopes to one order), stamp `shopify_freight_class` on every items dict, set the SO-level rollup on `so_dict`. Real-time webhook path covered; manual paths (workspace `scripts/backfill_orders.py`, ad-hoc fixes) call `recompute_for_so(so_name)` for the same effect.

**Tests** — 35 new in `test_freight_class.py`:
- `TestClassifyProductTags` (12) — single tags, lists, case/whitespace, unknown tags, first-match-wins
- `TestRollupSo` (13) — uniform/mixed/with-blanks/with-dropship/all-blank
- `TestResolveForOrder` (8) — air-only, sea-only, split, dropship, missing product_id, fetcher-throws, empty payload
- `TestLiveFetcherCaching` (1) — cache hit semantics with a mocked Shopify SDK

**Compat:** purely additive. The older `_resolve_shipping_method` (B7) → `Sales Order Item.shopify_shipping_method` Data field remains unchanged for back-compat; downstream consumers can migrate to `shopify_freight_class` at their own pace.

`__version__` bumped 1.2.3 → 1.2.4.

## [yei-v1.2.3] — 2026-05-08

**Target bench:** bench-37067
**Branch:** `version-16`
**Deployed:** 2026-05-08 (pending — this entry written ahead of deploy)

### Fixed — B22: null-`product_id` line items must not crash whole-order sync

Shopify emits line items with `product_id == None` (or the key omitted entirely) for FREE-GIFT promo lines added by discount apps, MISC-MANUAL freeform entries, and a handful of other store-side patterns. Pre-B22, `create_items_if_not_exist` (called at the top of every order webhook handler) read `item["product_id"]` directly, which raised `KeyError`/`TypeError` on the very first such line. The exception bubbled up and aborted the entire order's item-sync, causing the parent SO never to materialize.

This was the root cause of the 33+ orders that failed to sync between 2026-04-15 and 2026-05-07 (manually backfilled via `scripts/backfill_orders` helpers; ERPNext commits `e7385c8`, `748c72e`, `ce9b516`, et al).

**Fix:** [`ecommerce_integrations/shopify/product.py:269-289`](https://github.com/milky-claw/ecommerce_integrations/blob/version-16/ecommerce_integrations/shopify/product.py#L269) — switch `item["product_id"]` to `item.get("product_id")`, then `continue` past lines where the value is null/missing. These lines have no upstream Shopify Product to sync; yei should pass through. The parent SO still gets created with the placeholder line items pointing at the YGH-managed Items (MISC-MANUAL, etc.) or being skipped entirely for FREE-GIFT lines.

**Tests:** 2 new in `test_product.py::TestCreateItemsIfNotExist`:
- `test_skips_null_product_id_lines` — order with three lines (None, "", real); only the real one constructs `ShopifyProduct(...)`.
- `test_missing_product_id_key_does_not_raise` — line dict without the `product_id` key at all is silently skipped.

`__version__` bumped 1.2.0 → 1.2.3 (catches up with shipped tags 1.2.1, 1.2.2 which never bumped the string).

## [yei-v1.2.2] — 2026-04-24

**Target bench:** bench-37067
**Branch:** `version-16`
**Commit:** [`2653222`](https://github.com/milky-claw/ecommerce_integrations/commit/2653222)
**GitHub Release:** https://github.com/milky-claw/ecommerce_integrations/releases/tag/yei-v1.2.2
**Deployed:** 2026-04-24 15:03Z (candidate `b908suld44`, migration `8fehsl35ir`)

Combined release:
1. **B21** — close the f-027 regression vector (order-sync-path analogue of B20).
2. **Native-field swaps** — retire two custom fields that duplicate native ERPNext slots (`Item.shopify_selling_rate` → native `Item.standard_rate`; `Sales Order Item.shopify_item_discount` → redundant with B15's native rate + price_list_rate).

### Fixed — B21: order-sync path must never mint numeric-ID Items

**Root cause.** B20 (yei-v1.2.1) closed the webhook-path vector for f-020, but the order-sync path was left open. `create_items_if_not_exist` → `ShopifyProduct.sync_product()` → `_make_item` still called `_create_item(..., has_variant=1)` for variant-bearing Shopify products, producing a templated ERPNext Item with `item_code = product_dict["id"]` (numeric Shopify product_id). `_match_sku_and_link_item` short-circuited on `has_variant=True`, so the template was never linked to an existing SKU and instead created a fresh numeric-ID Item.

**Impact.** Item `6970914963546` (f-027) appeared on `2026-04-22 23:50:45Z` through this path — 26 hours after B20 deployed. One-instance, cosmetic orphan (no Sales Orders referenced it), but the vector remained live. Orchestrator flagged this for 2 days.

**Fix.** Kete's flat-Item convention was never properly enforced — every Shopify variant should map 1:1 to a top-level ERPNext Item keyed by SKU, with no `has_variants=1` templates created by the connector. B21 removes the templated-Item code path entirely:

- **Rewritten** `_make_item` as a per-variant loop that produces flat Items only. Variants with SKU → `_create_flat_variant` (new helper) → B14 SKU-match or new flat Item. Variants without SKU → explicit `B21 skip` log, manual Item creation required.
- **Deleted** `_create_item_variants`, `_create_attribute`, `_set_new_attribute_values`, `_get_attribute_value` (four methods, ~100 LOC) — unreachable after the rewrite; this fork no longer uses ERPNext Item Attributes at all.
- **Simplified** `_match_sku_and_link_item` — dropped `has_variant` and `variant_of` parameters since post-B21 it's always called with flat Items. Four-parameter signature becomes three.
- **Added** `_guard_non_numeric_item_code(item_code, source)` — boundary helper at the top of `product.py` that raises `ValueError` if any future code path tries to create an Item with a pure-digit item_code. Called from `_create_flat_variant`. Defense-in-depth beyond the B21 rewrite — catches future regressions at the constructor boundary.

### Swapped — `shopify_selling_rate` → native `Item.standard_rate`

**Rationale.** `Item.standard_rate` is ERPNext's native "Default Selling Rate" field, already on every Item. The custom `shopify_selling_rate` was a parallel slot that yei never actually populated on this site (0 Items with shopify_selling_rate set at deploy time; 0 Items with standard_rate set). Swap is architecturally cleaner with zero data migration risk.

**Changes:**
- `product.py` — 4 read sites in `upload_erpnext_item` + `map_erpnext_variant_to_shopify_variant` swapped from `item.get(ITEM_SELLING_RATE_FIELD)` → `item.standard_rate` / `template_item.standard_rate`.
- `shopify_setting.py::setup_custom_fields` — the Item block drops the `shopify_selling_rate` field install. `shopify_tags` now inserts after `standard_rate` directly.
- `constants.py` — `ITEM_SELLING_RATE_FIELD` import removed from product.py + shopify_setting.py + test_shopify_setting.py. Constant retained in constants.py for now (no fork-external consumers).

### Retired — `shopify_item_discount`

**Rationale.** B15 (yei-v1.2.0) fixed discount handling to write both `rate` and `price_list_rate = effective_rate` on each Sales Order Item, so the discount is embedded in the native price fields (discount = list_rate_from_Shopify − effective_rate). The separate `shopify_item_discount` snapshot was redundant — a cache of data already present on the native SOI row. No operational system on `yourgreenhouses.frappe.cloud` reads this field (verified: 0 Print Formats, 0 Reports, 0 Dashboards, 0 Server Scripts, 0 Client Scripts reference it).

**Changes:**
- `order.py::get_order_items` — drop the `ORDER_ITEM_DISCOUNT_FIELD: per_unit_discount` write from the item row. Native `rate`/`price_list_rate` still set identically to B15.
- `shopify_setting.py::setup_custom_fields` — the Sales Order Item block drops the `shopify_item_discount` field install. `shopify_line_item_properties` now inserts after `discount_and_margin` directly.
- `constants.py` — `ORDER_ITEM_DISCOUNT_FIELD` import removed from order.py + shopify_setting.py + test_shopify_setting.py.

### Tests

New classes:
- `TestB21GuardNonNumericItemCode` — 4 tests on the inline `_guard_non_numeric_item_code_b21` copy (matches the existing `_handle_webhook_branch_b20` / `_build_item_row` pattern — the harness mocks `ecommerce_integrations.shopify.product` wholesale, so we inline the helper for unit-test purposes).
- `TestB21OrderSyncSourceInvariant` — 7 source-text tests reading `product.py`: asserts the templated-Item patterns are gone, `_create_item_variants` + attribute helpers are deleted, `_guard_non_numeric_item_code` exists + is invoked, no `"has_variants": 1,` literals, the simplified `_match_sku_and_link_item` signature is in place, `B21 skip` log-tag is present, and the native-swap points at `standard_rate`.
- `TestB21OrderItemDiscountRetired` — 2 source-text tests reading `order.py`: `ORDER_ITEM_DISCOUNT_FIELD` no longer imported; the retired write line is absent.

Updated tests:
- `test_shopify_setting.py::test_custom_field_creation` — dropped the 2 retired field constants from the expected set; count threshold reduced from `>=13` to `>=11`.
- `test_connector_patches.py::TestB15DiscountDollarAmount` — dropped 7 `assertEqual(row["shopify_item_discount"], ...)` assertions (native `rate` / `price_list_rate` assertions remain and fully validate the business logic).
- `_build_item_row` inline helper — drops the `"shopify_item_discount": per_unit_discount` key from the returned dict.

**Total: 141 tests pass.** Run: `python3 ecommerce_integrations/shopify/tests/test_connector_patches.py`.

### Post-deploy actions (planned)

1. Run one-shot cleanup: disable + rename `6970914963546` → `6970914963546-orphan-f027` and update the paired `Ecommerce Item` row. No data migration needed for the native-field swaps (0 populated rows for either field).
2. Verify `frappe.db.count("Item", {"item_code": ["regexp", "^[0-9]+$"], "disabled": 0})` returns 0.
3. Mark orchestrator finding `f-027` closed in `orchestrator/STATUS.md`.

### Out of scope / deferred

- Other native-field candidates surfaced by the [B21-NATIVE-FIELD-AUDIT](../ERPNext/stages/04c-data-sync/working/B21-NATIVE-FIELD-AUDIT.md) — `shopify_customer_id` / `address_id` / `supplier_id` / `metafields` / `financial_status` / `fulfillment_status` — all stay custom this release. A separate "native-first rebuild" will tackle platform-ownership + a generic `External Entity Map` doctype.

---

## [yei-v1.2.1] — 2026-04-21

**Target bench:** bench-37067
**Branch:** `version-16`
**Commit:** `cf32640`

Bugfix release. Single patch (B20) addressing the f-020 regression introduced when yei-v1.2.0's `products/create` + `products/update` webhooks were registered on 2026-04-20T20:47Z.

### Fixed — B20: `sync_product_from_webhook` must not create Items

**Root cause.** yei-v1.2.0's `sync_product_from_webhook` kept a fallback "create_new" branch that, when a webhook fired for a product with no existing `Ecommerce Item` mapping, called `ShopifyProduct(...).sync_product()` to create ERPNext Items from scratch. That path runs `_create_item`, which sets `item_code = product_dict.get("item_code") or product_dict.get("id")`. For variant-bearing products the template hits `_match_sku_and_link_item` with `has_variant=True`, which short-circuits to `return False` (templates don't have SKUs), so the template's `item_code` defaults to `product_dict["id"]` — a Shopify numeric ID. For 2 draft products without SKUs, every variant also fell through to numeric-ID creation.

**Impact.** 5 Shopify products (3 active, 2 draft) created 70 shadow ERPNext Items (5 templates + 65 variants) with numeric item_codes + 70 `Ecommerce Item` shadow mappings before the webhooks were disabled at 2026-04-21T08:30Z. Zero orders were placed against the duplicates (verified: 0 Sales Order Item refs across all 11 SOs in the window).

**Fix.** `sync_product_from_webhook` is now tag-sync-only:

1. If the product has linked `Ecommerce Item` rows → refresh `shopify_tags` on every linked ERPNext Item (unchanged B5 behavior).
2. If the product is **not** mapped → emit `B20 skip: product <id> has no ERPNext mapping — webhook does not create Items (f-020 fix)` and return. The handler never instantiates `ShopifyProduct` or calls `.sync_product()`.

**New-product promotion** now routes exclusively via:
- **Order sync** — `create_items_if_not_exist` in `order.py`, which runs the B14 SKU-first resolver per line item and handles SKU match / product-level fallback correctly.
- **Manual ERPNext Item creation** — stage-04b-style catalog import.

This matches how we onboarded the Stage-04b catalog (184 Items, 18 BOMs, 52 prices) — orders + manual, never via webhook-triggered create.

### Tests — source-invariant guards

The regression reintroduced behavior that had previously been argued about (B14 deliberately dropped the `variant_of` guard from `_match_sku_and_link_item`); without a test, a future refactor could easily re-add the create path and re-create the regression. Two new test classes:

- **`TestB20UnmappedProductSkipped`**: dispatch-logic tests. Unmapped → 0 tag writes, 1 skip log. Mapped → N tag writes, 1 success log. Isolated inline helper `_handle_webhook_branch_b20`.
- **`TestB20SourceInvariant`**: reads `product.py` source, strips the docstring, asserts the handler body contains **no** `ShopifyProduct(` and **no** `.sync_product()` method call, and **does** contain the `B20 skip` log string. Explicitly guards against reintroduction of the f-020 regression vector by any future refactor.

127/127 tests pass (123 baseline + 4 B20). Run: `python3 ecommerce_integrations/shopify/tests/test_connector_patches.py`.

### Post-deploy actions (completed 2026-04-21)

1. ✅ Re-registered the two products/* webhooks via Shopify Admin API (new IDs: `products/create`=`1892629905771`, `products/update`=`1892629938539`).
2. ✅ Cleaned up the 70 duplicate ERPNext Items (disabled + renamed to `<code>-orphan-f020`, via `frappe.client.rename_doc` — variants first, templates last) + 70 shadow `Ecommerce Item` mappings deleted (snapshot retained at `/tmp/f020_ec_dupes_snapshot.json`).
3. ✅ Verified behaviorally via ambient Shopify Log entries at 11:11:44Z-11:11:48Z showing the new B5 message format on 5 unrelated products — that string exists only in cf32640.

### Known minor

`__version__` in `ecommerce_integrations/__init__.py` was **not** bumped to `"1.2.1"` in this commit. The deployed code is at cf32640 (confirmed via behavioral log format), but `frappe.utils.change_log.get_versions` will continue to report `1.2.0` until the version string is updated. Cosmetic — fix in next commit.

---

## [yei-v1.2.0] — 2026-04-20

**Target bench:** bench-37067
**Branch:** `version-16`

Bundles 4 connector patches (B5, B17, B18, B19) into a single release per the Stage 04c TODO. Three of the four are pre-Kete onboarding blockers (SO name clarity, delivery_date sanity, product tag visibility on line routing). All share the same files and deploy cycle.

### Added — B5: product webhooks (tag sync in real time)

Connector previously subscribed only to order events — product tag changes on Shopify (adding `ship-dropship` / `warranty` / renaming `stockv1`→`stockv2` etc.) never reached `Item.shopify_tags`. Tags only flowed on first product sync via `create_items_if_not_exist`.

1. **`constants.py — WEBHOOK_EVENTS` + `EVENT_MAPPER`** gain `products/create` and `products/update`, both routed to `ecommerce_integrations.shopify.product.sync_product_from_webhook`.
2. **`product.py — sync_product_from_webhook`**: dispatches by Ecommerce Item existence. If the product is already mapped, just `frappe.db.set_value` on `Item.shopify_tags` for every linked ERPNext Item (fast path, DB-only, no Shopify API call). If unmapped, run the full `ShopifyProduct.sync_product()` create flow.
3. **Backfill script** (post-deploy, separate artifact at workspace `scripts/repair/backfill_b5_item_tags.py`) populates tags on the 189 currently-unpopulated active Items in one paginated Shopify read + REST PUT per Item.

B6 (metafields sync) stays parked — webhook payloads don't include metafields, and the mapping hasn't been scoped.

**Post-deploy action:** re-register webhooks so Shopify subscribes to the two new topics. Same mechanism as the 2026-04-17 HMAC rotation.

### Added — B17: SO `delivery_date` from shipping_lines title

Connector was setting `delivery_date = created_at` on every Shopify SO, which flagged every order Overdue within 1–2 days. Fix: parse `shopify_order.shipping_lines[].title` for either of two shapes:

- **Business-day range:** regex `\((\d+)\s*-\s*(\d+)\s+business days\)` (case-insensitive). Uses upper bound; adds that many business days to `order_date`, skipping Saturdays and Sundays.
- **Explicit preorder date:** regex `Estimated (?:to be Delivered|Delivery by)\s+<Month>\s+<Day>(?:st|nd|rd|th)?` (case-insensitive, accepts full + abbreviated month names). If parsed date is before `order_date`, bumps year by 1 (handles Dec → Jan rollover).

Across multiple `shipping_lines` (mixed carts, preorder + regular), picks the **LATER** resolved date. Falls back to `order_date` if nothing parses (preserves current behavior for that one SO; doesn't crash). Per-line `delivery_date` on `Sales Order Item` now matches the SO header.

Coverage verified against 50 recent Shopify SOs — 100% regex match across all four shipping methods observed in the general profile (`FREE Shipping`, `Priority Handling`, `PRIORITY Secured FedEx Shipping with Tracking`, `Secured FedEx Shipping with Tracking`) + both preorder phrasings.

### Added — B18: SO currency from Shopify order

Was storing `currency=EUR, conversion_rate=1.0` for every order regardless of Shopify's actual currency (USD for the US store). Fix: set `SO.currency = shopify_order.currency` verbatim (uppercased, whitespace-stripped). If missing, omits the field so ERPNext's Customer/Company default kicks in.

**`conversion_rate` intentionally not set** — ERPNext applies current FX at Sales Invoice creation time, which avoids stale rates baked into the SO.

No backfill — existing SOs' currency mislabels are frozen; fix applies to new SOs going forward.

### Added — B19: SO name = `SH-YYYY-NNNNN` for Shopify-sourced orders

ERPNext SO name (`SAL-ORD-2026-02333`) had no relation to Shopify order number (`#4395`), making cross-platform lookup painful for the rep and orchestrator. Fix: rename to `SH-{year-from-created_at}-{order_number zero-padded to 5 digits}`.

- Year from Shopify `created_at` (not sync time) so orders keep their year-prefix across year boundaries.
- 5-digit pad covers >10k orders/year (Shopify's counter will exceed 5 digits eventually — zfill degrades gracefully, no truncation).
- Rename is post-submit via `frappe.rename_doc(..., force=True, merge=False)`. Cascades into `Delivery Note Item.against_sales_order` and `Sales Invoice Item.sales_order` automatically. Non-fatal on failure — SO exists with default naming and the error logs to Frappe Error Log.
- Non-Shopify SOs untouched; they keep `SAL-ORD-` naming from their own `naming_series`.

**Backfill scope (post-deploy, separate artifact at `scripts/repair/backfill_b19_so_rename.py`):** Sales Orders `modified` in the last 7 days only (~100 SOs, 0 have linked Delivery Note / Sales Invoice rows per audit — rename cascade is safe). Older SOs keep their historical `SAL-ORD-` names.

### Tests

39 new tests across 4 classes; 123/123 total pass (was 84):
- `TestB18Currency` (8) — USD/EUR present, case/whitespace normalization, missing/empty/None/non-string → None
- `TestB19Naming` (10) — standard format, padding, year-from-created_at, string coercion, malformed data → None, overflow graceful
- `TestB17DeliveryDate` (12) — business-day range (4/7/12/18), explicit date (standard + ordinal + alt phrasing), mixed-cart max, year rollover, full + abbreviated month names, no-match fallback, empty-line skip
- `TestB5ProductWebhookDispatch` + `TestB5TagUpdateApply` + `TestB5WebhookEventsConstants` (9) — mapped→update_tags, unmapped→create_new, int-coerced product_id, multi-variant tag walk, empty-tags still writes (clears stale), constants subscription verified

### Risks / rollback
- **B5 webhook re-registration required post-deploy** — without it, Shopify won't send products/* events; tag sync stays dormant (no regression, just missing new capability).
- **B19 rename failures are non-fatal** — SO persists under default naming + logs. Worst case: one confusing SO name, no data loss.
- **B17 fallback = `order_date`** when regex misses → that one SO keeps Overdue flag (same as today), doesn't cascade.
- **B18 missing currency → ERPNext default** → no worse than today.
- Rollback: revert commit + rebuild bench. Backfill scripts are idempotent; their effects outlive the patch revert (and that's fine — data repair stays).

### Known / not addressed
- B6 (metafield sync) still parked; revisit after Kete meeting scopes which metafields matter.
- HMAC validation still permissive (logs warning, accepts all) — tighten post-Kete stability.
- Warehouse mapping still empty on `Shopify Setting`.
- `orders/edited` webhook: still out-of-band registered (upstream issue from 2026-04-17); untouched here.

---

## [yei-v1.1.0] — 2026-04-19

**Target bench:** bench-37067 (deploy candidate TBD — triggered via Press API)
**Branch:** `version-16`

Bundles the pending B15 revision (see `597356c`) with the new B16 patch below.

### Added — B16: ship-dropship + product_id fallback for SKU-less products

Enables the Stage 04c dropship workflow. Three coordinated changes:

1. **`product.py — get_item_code` product-level fallback.** When the primary lookup `(integration, product_id, variant_id, sku)` misses, a second lookup runs with `variant_id=None, sku=None` — matching any Ecommerce Item row whose `integration_item_code` equals the Shopify product_id. Handles SKU-less catalog products (dropship items, draft products pending SKU assignment) where the fork maintains a product-level `product_id → representative item_code` mapping. Existing SKU matches are unchanged.
2. **`order.py — _resolve_shipping_method` adds `ship-dropship`.** Reads `Item.shopify_tags`; if it contains `ship-dropship` the method returns `'ship-dropship'`, bypassing `ship-sea`/`ship-air`. Priority order: dropship > sea > air.
3. **`order.py — create_sales_order` sets `shopify_fulfillment_source`.** After the SO dict is constructed, if any line's shipping method is `ship-dropship` the custom field is set to `'dropship'`, otherwise `'warehouse'`. Downstream consumers (the Stage 04d FedEx client, dropship reports) can branch on a single field instead of re-scanning line items.

Added `ORDER_FULFILLMENT_SOURCE_FIELD = "shopify_fulfillment_source"` to `constants.py`.

**Tests:** 4 new test classes (18 new tests) in `test_connector_patches.py`:
- `TestB16ProductIdFallback` — single + multi-variant no-SKU resolution, primary-still-wins guard, both-miss → None
- `TestB16ShipDropshipDetection` — dropship detection in various tag strings
- `TestB16ShipDropshipPriority` — dropship > sea > air, plus sea > air regression guard
- `TestB16FulfillmentSourceField` — SO field computed correctly from line-item shipping methods

Total: 84/84 tests pass (was 66; +18 new).

### Changed — B15 revision (from prior [Unreleased])
- **B15 revision** (`597356c` supersedes `f140ffb`): set `rate == price_list_rate` to the same discounted-dollar value rather than using `discount_percentage`. Matches Shopify's dollar-amount discount model (no percentage conversion, no rounding). Root cause of #4344 isolated via direct save+submit test matrix: `rate=0` alone survives cleanly, but `rate=0 + price_list_rate=X>0` triggers ERPNext's reconciliation path. Fix short-circuits reconciliation because plr == rate. Per-unit discount audit preserved in `shopify_item_discount` custom field. 11 rewritten tests in `TestB15DiscountDollarAmount`, including the core invariant `test_rate_equals_price_list_rate_invariant` and explicit `test_no_percentage_conversion_no_rounding` using $33.33/$100.

### Version bump
- `__version__` in `ecommerce_integrations/__init__.py`: `1.17.0` → `1.1.0` (aligning with `yei-*` fork versioning scheme — previously left at upstream's `1.17.0` by oversight).

### Risks / rollback
- New fallback only fires if primary match fails → zero regression risk for existing SKU-matched products.
- Rollback: revert the commit + rebuild bench. Dropship orders then fall back to `MISC-MANUAL` (the safe B12 default).
- No data migration needed — fix is code-only.

### Known (not yet fixed in code)
- `orders/edited` webhook was registered on Shopify out-of-band via Admin API (webhook id `1890513060203`) because the connector's `WEBHOOK_EVENTS` listed it but the 2026-04-17 HMAC re-registration helper somehow missed this one topic. Consider adding a defensive re-registration check in `connection.py` that reconciles Shopify's webhook list against `WEBHOOK_EVENTS` on startup.

---

## [yei-v1.0.0] — 2026-04-17

First tagged fork release. Captures all fork-specific commits through `f140ffb`. Encompasses the full 04c connector-patches body of work (B1–B15) + webhook robustness fixes.

### Commit inventory (fork-vs-upstream)

| Commit | Category | Scope |
|---|---|---|
| `da2d6e2` | webhook robustness | bytes serialization in webhook validation; guard webhook re-registration (idempotent retry) |
| `1024bd1` | connector patches B1–B13 | order + product sync enrichment; new custom fields; `MISC-MANUAL` fallback; `orders/edited` webhook |
| `2415fb7` | upstream hardening | handle null `fulfillment_status` from Shopify; +47 unit tests |
| `6a4a492` | webhook compatibility | accept HMAC mismatches with warning (do not throw) — allows our client-secret rotation to not take the site down |
| `708a753` | **B14 — critical data integrity fix** | `_match_sku_and_link_item` was skipping SKU-match for all variant products (multi-variant SKUs were being linked to phantom variant_id Items instead of the canonical ERPNext Item with the matching SKU). Fix drops the `variant_of` guard. Plus 8 new tests (`TestB14VariantSKUMatch`, `TestB14BugRegression`) |
| `f140ffb` | **B15 — pricing correctness fix** (superseded by `597356c`) | Live webhook #4344 over-charged $69.60: connector set only `rate`, but ERPNext's save/submit reconciles rate from `price_list_rate`. First attempt used `discount_percentage` — introduced percentage rounding, didn't match Shopify's dollar-amount model. Superseded in `[Unreleased]` by the simpler rate-equals-plr approach. |

### What our fork delivers on top of upstream v16

**Custom fields auto-installed on `bench migrate`** (via `shopify_setting.setup_custom_fields()`):

| DocType | Field | Type | Purpose |
|---|---|---|---|
| Item | `shopify_tags` | Small Text | Sync Shopify product tags for downstream lookups (e.g. `ship-sea`/`ship-air`) |
| Item | `shopify_metafields` | Long Text | Sync arbitrary Shopify metafields as JSON |
| Sales Order | `shopify_financial_status` | Data | `paid`, `pending`, `refunded`, etc. |
| Sales Order | `shopify_fulfillment_status` | Data | `unfulfilled`, `fulfilled`, `partial`, `null` |
| Sales Order | `shopify_discount_codes` | Small Text | Comma-separated discount codes applied |
| Sales Order | `shopify_tip_amount` | Currency | Tip line items summed here; never land as SO Items |
| Sales Order Item | `shopify_line_item_properties` | Long Text | Per-item manufacturing instructions (door count, vent spec, etc.) as JSON |
| Sales Order Item | `shopify_shipping_method` | Data | `ship-sea` or `ship-air`, derived from Item tags at sync time |

**New webhook:** `orders/edited` → `handle_order_edited()` — compares existing SO against edited Shopify order, builds a diff, creates a ToDo assigned to the setting owner. Does not auto-modify the SO.

**Enrichment pipeline additions to `order.py`:**

- **B1** — Order tags → `shopify_order_status` field (Preorder, BNPL, split, AfterSell Upsell, influencer, Amazon-US, etc.)
- **B2** — Line item properties → `shopify_line_item_properties` JSON
- **B7** — Shipping method per item (`_resolve_shipping_method()` looks up ERPNext Item via Ecommerce Item → reads `shopify_tags` → returns `ship-sea`/`ship-air`)
- **B8** — Discount codes → `shopify_discount_codes`
- **B9** — Tip handling (`_separate_tips()` filters line items with title `Tip`, sums into `shopify_tip_amount`; tips never land as SO Items)
- **B10** — Financial + fulfillment status → respective custom fields; also updated on `orders/edited` + `orders/cancelled`
- **B12** — `MISC-MANUAL` fallback: `get_order_items()` completely rewritten. If `product_exists` is false OR `get_item_code()` returns None, falls back to `MISC-MANUAL` item_code (auto-created non-stock "Products" group Item if missing). Stores original Shopify title + product_id + variant_id in the SO Item description. **Order sync never fails on unmatched items.**
- **B13** — `orders/edited` webhook handler (above)

**Enrichment pipeline additions to `product.py`:**

- **B5** — Product tags: `_sync_tags_and_metafields()` reads `product_dict.get("tags")` → `shopify_tags` on all linked Items
- **B6** — Product metafields: same function calls `Product.find(product_id).metafields()` within the active Shopify session. Stores as JSON in `shopify_metafields`.
- **B14** (this release) — critical fix: variant products now go through SKU match instead of skipping it

### B14 root cause + impact

**Pattern:** every multi-variant Shopify product (Nordwood, Scandiglas, WMP, Arrow, Classic Arch, raised-bed kits, sliding doors, vents — ~38 products total) was bypassing SKU→Item lookup because `_match_sku_and_link_item` early-returned when `variant_of` was truthy. The fallback path created an Item with `item_code = variant_id` AND an Ecommerce Item mapping pointing to it. Once a broken mapping existed, subsequent orders reused it — no re-lookup.

**Why it went undetected for days:** live order sync succeeded (SOs got created, webhooks fired normally), but every line item for affected variants carried `item_code = <shopify variant_id>` instead of the real SKU. Inventory, pricing, BOM lookups, COGS — all silently wrong. Discovered when the user noticed SO line items had numeric variant-id strings where SKU strings were expected.

**The fix:** [shopify/product.py:286](https://github.com/milky-claw/ecommerce_integrations/blob/ygh-v1.0.0/ecommerce_integrations/shopify/product.py#L286) — drop `variant_of or` from the guard. Keep `has_variant or` (template rows should not get SKU-matched — only real variant rows).

**Data repair** ran alongside the patch:

- **Phase A** — re-pointed 42 self-pointing `Ecommerce Item` mappings via REST PUT
- **Phase B** — updated 36 Sales Order Item rows (SAL-ORD-2026-02260 through 02283) to use canonical SKUs, via a transient Frappe Server Script that bypassed docstatus constraints without touching rate/discount/tax

**Verification** (`scripts/repair/verify_b14_repair.py`): two server-side SQL queries across all 1,921 Shopify-linked SOs. **0 self-pointing mappings, 0 variant-id item_codes remain.**

### Tests

- 55 pass, 0 failure, 0 skip
- 47 pre-existing upstream tests, unchanged behavior
- 8 new tests: `TestB14VariantSKUMatch` (6 cases), `TestB14BugRegression` (2 cases — before/after contract pinned)
- All tests mocked; no live Shopify API calls in CI

### Deployed

- Live on `yourgreenhouses.frappe.cloud` bench-37067 as of 2026-04-17 21:10 LT (via Frappe Cloud dashboard → bench-37067 → Apps → ecommerce_integrations → **Fetch Latest Updates**).

### Known operational caveats

- HMAC validation is permissive (logs warning, doesn't reject) — accepted per phase-0 memo. Affects webhook authenticity signal; doesn't affect order data integrity.
- Warehouse mapping is still empty on `Shopify Setting` — inventory deductions have no source location. Separate card; not blocking order sync.
- Next real Shopify order after `708a753` deploy will test the B14 prevention side end-to-end. If a new SO arrives with `item_code = <variant_id>`, the patch wasn't properly merged on the deployed bench — `scripts/repair/verify_b14_repair.py` one-shot confirms clean state in ~1 sec.

### Repair tooling (lives in workspace repo, not this fork)

- `/Users/milky/ERPNext/scripts/validation/diagnose_so_item_mapping.py` — 8-class failure classifier; idempotent read-only
- `/Users/milky/ERPNext/scripts/repair/fix_b14_ecommerce_item_mappings.py` — Phase A driver (idempotent; on clean data "REPOINT: 0")
- `/Users/milky/ERPNext/scripts/repair/fix_b14_so_item_codes.py` — Phase B driver (idempotent server-side via transient Frappe Server Script)
- `/Users/milky/ERPNext/scripts/repair/verify_b14_repair.py` — one-shot verify, self-cleaning
- Logs: `/Users/milky/ERPNext/logs/b14-phase-a-2026-04-17.log`, `b14-phase-b-2026-04-17.log`, `b14-verify-2026-04-17.log`

Full session narrative (diagnosis method, order count jump 19→24 mid-session, repair pattern): [`logs/session-log-2026-04-17T20-30-b14.md`](../../ERPNext/logs/session-log-2026-04-17T20-30-b14.md) in the workspace repo.

---

## Upstream history

Upstream tags (`v1.0.0` through `v16.0.0`, 60+ releases) are preserved. This fork's `ygh-v*` tags sit alongside them and describe only the fork-local divergence. To compare fork vs upstream: `git log upstream/version-16..origin/version-16`.
