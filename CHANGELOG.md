# Changelog — `milky-claw/ecommerce_integrations` fork

Fork-specific changes on top of upstream `frappe/ecommerce_integrations` (branch `version-16`). Upstream tags (`v1.x.y`, `v16.0.0`) remain as-is; our fork's additions use the **`yei-v*`** prefix (YGH Ecommerce Integrations) to avoid namespace collision with upstream and with sibling apps like `ygf-*` (the YGH FedEx app).

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning SemVer-for-our-fork starting at `ygh-v1.0.0`.

**Scope of this fork:** runtime fixes + connector patches deployed to `yourgreenhouses.frappe.cloud` to support YourGreenhouses' Shopify → ERPNext order + product sync. Upstream PRs are the right destination for most of these; we fork for speed while stabilizing, and merge upstream when the patches are broadly useful.

**Target site:** `yourgreenhouses.frappe.cloud` (bench-37067, f2-virginia.frappe.cloud).

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
