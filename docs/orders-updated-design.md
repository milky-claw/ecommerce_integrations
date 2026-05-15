# Design: `orders/updated` webhook subscription

**Status:** Draft for review
**Date:** 2026-05-15
**Author:** Brainstorm with Milky
**Scope:** yei (ecommerce_integrations fork — milky-claw)
**Pairs with:** None (yei-only ship)
**Touches Press deploy:** Yes (new webhook subscription requires `shopify_setting` re-install)

---

## Problem

yei currently subscribes to six Shopify webhook topics:

```
orders/create
orders/paid
orders/fulfilled
orders/partially_fulfilled
orders/cancelled
orders/edited
```

It does **not** subscribe to `orders/updated` — the catch-all "something on the order object mutated" event. Two real consequences:

1. **Shipping-address edits go unmirrored.** A rep edits the customer's ship-to in Shopify Admin (typo fix, customer call-in correction). No subscribed webhook fires. The ERPNext SO mirror stays stale until a `orders/edited` event happens to fire on the same order — often never.

2. **Defense-in-depth gap on fulfillment.** If `orders/fulfilled` is missed for any reason (Shopify retry exhaustion, queue stall, transient handler exception), `Sales Order.shopify_fulfillment_status` never reaches `"fulfilled"`. Downstream (`writer.py:1659`, `writer.py:1825` in ygf) reads this field to drive supplier-sheet status. Stale mirror = wrong sheet state.

## Out of scope (explicit non-goals)

The following payload fields on `orders/updated` are **ignored** by the new handler:

| Field | Reason for exclusion |
|---|---|
| `financial_status` | Mirror is unreliable without a real Shopify hold-signal; financial transitions handled elsewhere (`orders/create` initial mirror, `orders/paid` for completion). On Hold derivation flagged as flawed (ygf-v0.5.8 CHANGELOG). |
| `tags` | Shopify Flow churns these constantly. No downstream consumer in ERPNext. |
| `note` | No downstream consumer. |
| `customer.*` | Billing-side, not shipped-to. |
| `email` (order-level) | Receipt email, not shipping contact. |
| `fulfillment_status` values other than `"fulfilled"` | Per scope: only the "Fulfilled" state mirrors via this handler. Un-fulfill / `partial` / `null` are ignored. |

## In scope

Two payload conditions trigger handler action; everything else is a no-op.

### Condition 1: fulfillment_status = "fulfilled"

When `payload["fulfillment_status"] == "fulfilled"` and `SO.shopify_fulfillment_status != "fulfilled"`:

→ write `SO.shopify_fulfillment_status = "fulfilled"` via `frappe.db.set_value(..., update_modified=False)`.

Idempotent. The normal case is `orders/fulfilled` already wrote this, so the comparison short-circuits and no DB write happens. The defense-in-depth case (orders/fulfilled missed) catches up on the next `orders/updated`.

### Condition 2: shipping_address differs from SO mirror

When `payload["shipping_address"]` differs from the SO's current shipping address (field-by-field comparison on the labelled subset below), invoke the subhandler `_handle_shipping_address_change`.

**Labelled subset** (the fields that go on a FedEx label):
- `first_name`, `last_name`
- `company`
- `address1`, `address2`
- `city`, `province_code`, `zip`, `country_code`
- `phone`

Everything else on the `shipping_address` sub-object (e.g., `latitude`, `longitude`, `name` composite) is ignored.

---

## Subhandler: `_handle_shipping_address_change(so, payload_address)`

Branches on linked Delivery Note state (tier 1 → tier 4 escalation).

### Tier computation

```
def _compute_tier(so_name):
    dni_rows = frappe.get_all(
        "Delivery Note Item",
        filters={"against_sales_order": so_name},
        fields=["parent"],
        distinct=True,
    )
    if not dni_rows:
        return 1, []          # no DN exists
    dns = [frappe.get_doc("Delivery Note", r.parent) for r in dni_rows]
    dns_alive = [d for d in dns if d.docstatus != 2]   # exclude cancelled
    if not dns_alive:
        return 1, []
    has_awb = any(getattr(d, "fedex_awb_number", None) for d in dns_alive)
    if not has_awb:
        return 2, dns_alive
    has_pickup = any(
        getattr(d, "fedex_pickup_status", "") in ("PICKED_UP", "IN_TRANSIT", "DELIVERED")
        for d in dns_alive
    )
    return (4, dns_alive) if has_pickup else (3, dns_alive)
```

### Tier action matrix

| Tier | DN state | SO mirror update | DN address update | Sheet refresh | Telegram alert |
|---|---|---|---|---|---|
| 1 | No DN (or all cancelled) | Yes | n/a | Yes | No |
| 2 | DN(s) draft, no AWB | Yes | Yes (draft DNs only) | Yes | No |
| 3 | DN(s) with AWB, pre-pickup | Yes | **No** (label printed; SO/DN drift recorded as `address_drift_pending`) | Yes | **Yes — tier 3** |
| 4 | DN(s) with AWB + pickup | Yes | **No** (label printed) | Yes | **Yes — tier 4 (URGENT)** |

Tier 3 alert payload (sketch):

```
🚨 Address change [Tier 3 — AWB minted, pre-pickup]
SO: SAL-ORD-2026-XXXXX (Shopify #YGH-NNNN)
Customer: <name>

OLD ship-to:
  <addr block>
NEW ship-to (Shopify edit at HH:MM UTC):
  <addr block>

DN(s) affected:
  MAT-DN-2026-NNNNN — AWB <awb> (FedEx, pickup scheduled YYYY-MM-DD)

ACTION REQUIRED: Decide cancel-and-remint (free pre-pickup) or accept divergence.
```

Tier 4 variant: header says **`Tier 4 — URGENT (TRUCK ROLLING)`** and footer says **`Manual intercept required — call FedEx customer service`**.

### Sheet refresh

After SO + DN writes commit, enqueue `ygh_fedex.supplier_sync.writer.refresh_so_row(so_name)` (or equivalent). The existing writer reads SO + DN state and rewrites the supplier-sheet row.

### Telegram alert plumbing

New entry point in stage 04e bot: `notify_kete_address_drift(so_name, tier, old_addr, new_addr, dn_awbs)`. Posts to the Kete-whitelisted group via existing bot infrastructure (no new bot, no new whitelist). Payload above gets formatted server-side.

---

## Handler skeleton

```python
# ecommerce_integrations/shopify/order.py

from ecommerce_integrations.shopify.constants import (
    ORDER_FULFILLMENT_STATUS_FIELD,
)


def handle_order_updated(payload, request_id=None):
    """Webhook handler for orders/updated.

    Reacts only to:
      - fulfillment_status == "fulfilled"
      - shipping_address subset changes

    All other payload fields are ignored by design (see design doc).
    """
    shopify_order_id = str(payload.get("id") or "")
    if not shopify_order_id:
        return

    so_name = _resolve_so_by_shopify_order_id(shopify_order_id)
    if not so_name:
        # SO not mirrored yet (out-of-order webhook delivery, or pre-cutover order)
        return

    so = frappe.get_doc("Sales Order", so_name)

    # Condition 1: fulfillment_status mirror
    if (
        payload.get("fulfillment_status") == "fulfilled"
        and so.get(ORDER_FULFILLMENT_STATUS_FIELD) != "fulfilled"
    ):
        frappe.db.set_value(
            "Sales Order",
            so_name,
            ORDER_FULFILLMENT_STATUS_FIELD,
            "fulfilled",
            update_modified=False,
        )

    # Condition 2: shipping_address subset change
    payload_addr = payload.get("shipping_address") or {}
    if _shipping_address_differs(so, payload_addr):
        _handle_shipping_address_change(so, payload_addr)


def _resolve_so_by_shopify_order_id(shopify_order_id):
    """Resolve SO name from shopify_order_id, handling non-unique field.

    Per feedback_shopify_order_id_not_unique.md, the Custom Field is
    `unique=0` and amendments produce duplicates. Pick the non-amended
    SO (docstatus != 2, smallest name lexically) if multiples match.
    """
    candidates = frappe.get_all(
        "Sales Order",
        filters={"shopify_order_id": shopify_order_id},
        fields=["name", "docstatus", "amended_from"],
        order_by="name asc",
    )
    if not candidates:
        return None
    alive = [c for c in candidates if c.docstatus != 2 and not c.amended_from]
    if alive:
        return alive[0].name
    # All amended or cancelled — fall back to first
    return candidates[0].name


def _shipping_address_differs(so, payload_addr):
    """True if any labelled field on payload differs from SO mirror."""
    LABELED = (
        ("first_name", "shipping_first_name"),
        ("last_name", "shipping_last_name"),
        ("company", "shipping_company"),
        ("address1", "shipping_address_line1"),
        ("address2", "shipping_address_line2"),
        ("city", "shipping_city"),
        ("province_code", "shipping_state"),
        ("zip", "shipping_zip"),
        ("country_code", "shipping_country_code"),
        ("phone", "shipping_phone"),
    )
    for payload_key, so_key in LABELED:
        if (payload_addr.get(payload_key) or "") != (so.get(so_key) or ""):
            return True
    return False
```

(Field-name mapping in `_shipping_address_differs` is illustrative — the actual SO fields depend on how Address links are stored. Implementation will confirm against the existing `orders/create` write path.)

---

## Idempotency & loop prevention

The handler reads SO state, compares to payload, only writes on actual delta. **Equal state → no DB write → no downstream events → no loop.**

This survives the future ygf → Shopify fulfillment push (currently out of scope, but architecturally enabled):

1. ygf detects FedEx pickup → calls Shopify Admin `fulfillment.create`
2. Shopify fires `orders/fulfilled` + `orders/updated`
3. `orders/fulfilled` handler: reads SO, sees DN already exists + submitted, idempotent no-op
4. `orders/updated` handler: reads `SO.shopify_fulfillment_status == "fulfilled"`, payload says same, no write → loop dies

No echo-suppression markers needed.

---

## Webhook registration

Two files need updating:

1. **`shopify/constants.py`** — add `"orders/updated"` to:
   - `WEBHOOK_EVENTS` (the subscription list, around line 11-17)
   - `EVENT_MAPPER` (around line 27-32), pointing at `ecommerce_integrations.shopify.order.handle_order_updated`

2. **`shopify/doctype/shopify_setting/shopify_setting.py`** — the install routine that POSTs `/webhooks.json` to Shopify needs `orders/updated` added to its registration loop. Existing webhooks should not be re-registered (Shopify returns 422 if topic+address already exists; the install routine handles this).

After deploy, **manual verification step:** GET `/admin/api/2026-04/webhooks.json` to confirm `orders/updated` is registered against the yei callback URL.

---

## Test plan

| # | Scenario | Expected |
|---|---|---|
| 1 | BNPL preorder paid (financial_status pending → paid) | Handler skips (financial_status out of scope) |
| 2 | Rep marks fulfilled in Shopify → orders/fulfilled writes mirror → orders/updated arrives moments later | Handler sees match, no-op |
| 3 | orders/fulfilled lost (retry exhausted) → orders/updated catches up | Mirror written ✓ |
| 4 | Tier 1: address edit, no DN | SO + sheet, silent |
| 5 | Tier 2: address edit, DN draft no AWB | SO + DN + sheet, silent |
| 6 | Tier 3: address edit, AWB minted pre-pickup | SO + sheet + Telegram alert, DN unchanged |
| 7 | Tier 4: address edit, post-pickup | SO + sheet + URGENT Telegram alert |
| 8 | SO not yet mirrored (out-of-order webhook) | Skip silently |
| 9 | Address payload identical to SO (no-op detection) | No DB write, no alert |
| 10 | Future loop test: ygf pushes fulfillment.create → Shopify fires orders/updated back | Handler no-ops via state comparison |
| 11 | `orders/updated` arrives with `fulfillment_status: null` (un-fulfill) | Ignored per scope; SO mirror unchanged |

Unit tests live in `ecommerce_integrations/shopify/tests/test_order_updated.py` (new file).

---

## Files touched

| File | Change |
|---|---|
| `ecommerce_integrations/shopify/constants.py` | Add `orders/updated` to `WEBHOOK_EVENTS` + `EVENT_MAPPER` |
| `ecommerce_integrations/shopify/order.py` | New `handle_order_updated` + helpers (`_resolve_so_by_shopify_order_id` if not already exists, `_shipping_address_differs`, `_handle_shipping_address_change`, `_compute_tier`, `_telegram_alert`) |
| `ecommerce_integrations/shopify/doctype/shopify_setting/shopify_setting.py` | Add `orders/updated` to install-time webhook registration |
| `ecommerce_integrations/shopify/tests/test_order_updated.py` | New — unit tests covering scenarios 1-11 |
| `CHANGELOG.md` | New release entry under `[Unreleased]` |
| Stage 04e bot | New `notify_kete_address_drift` entry point — separate PR in workspace, not in yei |

---

## Release plan

- **Tag:** `yei-v1.4.0` (feature-additive, minor bump)
- **Press deploy:** standard — bench update + migration step
- **Post-deploy verification:** GET `/admin/api/2026-04/webhooks.json` → confirm registration
- **Smoke test:** edit a shipping address on a test order in Shopify Admin → confirm SO mirror flips + supplier sheet row refreshes
- **Rollback:** revert constants.py + order.py changes; re-deploy. Shopify-side webhook registration leaks (deregistration is not automatic) but is harmless — yei will return 200 to unknown topics.

---

## Open questions for review

None — all design decisions settled during brainstorm.
