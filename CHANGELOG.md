# Changelog — `milky-claw/ecommerce_integrations` fork

Fork-specific changes on top of upstream `frappe/ecommerce_integrations` (branch `version-16`). Upstream tags (`v1.x.y`, `v16.0.0`) remain as-is; our fork's additions use the **`yei-v*`** prefix (YGH Ecommerce Integrations) to avoid namespace collision with upstream and with sibling apps like `ygf-*` (the YGH FedEx app).

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning SemVer-for-our-fork starting at `ygh-v1.0.0`.

**Scope of this fork:** runtime fixes + connector patches deployed to `yourgreenhouses.frappe.cloud` to support YourGreenhouses' Shopify → ERPNext order + product sync. Upstream PRs are the right destination for most of these; we fork for speed while stabilizing, and merge upstream when the patches are broadly useful.

**Target site:** `yourgreenhouses.frappe.cloud` (bench-37067, f2-virginia.frappe.cloud).

---

## [Unreleased] — pending deploy to bench-37067

Commit `597356c` on `version-16`. Will be tagged `yei-v1.1.0` once deploy is confirmed Active.

### Changed
- **B15 revision** (`597356c` supersedes `f140ffb`): set `rate == price_list_rate` to the same discounted-dollar value rather than using `discount_percentage`. Matches Shopify's dollar-amount discount model (no percentage conversion, no rounding). Root cause of #4344 isolated via direct save+submit test matrix: `rate=0` alone survives cleanly, but `rate=0 + price_list_rate=X>0` triggers ERPNext's reconciliation path. Fix short-circuits reconciliation because plr == rate. Per-unit discount audit preserved in `shopify_item_discount` custom field. 11 rewritten tests in `TestB15DiscountDollarAmount`, including the core invariant `test_rate_equals_price_list_rate_invariant` and explicit `test_no_percentage_conversion_no_rounding` using $33.33/$100. 66/66 total tests pass.

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
