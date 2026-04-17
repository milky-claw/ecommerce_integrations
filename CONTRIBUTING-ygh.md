# Contributing to the `milky-claw/ecommerce_integrations` fork

This is a **fork** of `frappe/ecommerce_integrations` maintained for YourGreenhouses. Contributions here land in our `yei-v*` tagged releases. Upstream contributions go to `frappe/ecommerce_integrations` directly — different process.

## Before you commit

Follow the [**Documentation-at-Every-Update Framework**](../.claude/projects/-Users-milky-ERPNext/memory/documentation_framework.md) in the workspace auto-memory. Key obligations:

| Event | Required artifact |
|---|---|
| **Code commit** | Conventional-commit message. Scope = `B<N>` for connector patches (B1-B15 etc.) or the file name for other fixes. Subject ≤70 chars, WHY over WHAT. |
| **Release / deploy** | Tag at deployed commit: `yei-vX.Y.Z[-suffix]` — see [versioning_convention.md](../.claude/projects/-Users-milky-ERPNext/memory/versioning_convention.md). Append a section to [`CHANGELOG.md`](CHANGELOG.md). Publish a GitHub Release. |
| **New connector patch (B-series)** | Document in [`CHANGELOG.md`](CHANGELOG.md) + add an entry to `/Users/milky/ERPNext/research/stage-04c-connector-patches-overview.md` + append to `data/DECISION-REGISTER.md` if behaviorally significant. |
| **Patch-driven data repair** | Repair scripts live in `/Users/milky/ERPNext/scripts/repair/` (not this repo). Logs at `/Users/milky/ERPNext/logs/<patch>-*.log`. Verification script must be idempotent. |

## Upstream sync discipline

When merging upstream tags into `version-16`:
- Rebase, don't merge, to keep history linear where possible
- Preserve our patches on top of upstream by design (our `B*` commits sit above upstream merges)
- Re-tag `yei-vX.Y.Z` AFTER the rebase — the tag points at our fork-local HEAD, not upstream commit

## Tests

- Run `python3 -m pytest ecommerce_integrations/shopify/tests/test_connector_patches.py -v` before every push
- Target: 65+ passing (47 original + B14 8 + B15 10 + any new). 0 failures.

## Never commit

- Shopify API tokens / secrets (they live in Shopify Setting doctype encrypted fields)
- Webhook HMAC secrets
- Any customer PII from webhook payloads (logs can redact)

## Release process (shorthand)

1. Implement B\<N\> → tests green → commit with `fix(B<N>): ...` or `feat(B<N>): ...`
2. Append to `CHANGELOG.md` under `[yei-vX.Y.Z]` (bump version per SemVer — patch for fix, minor for new B-number)
3. `git commit -m "docs: yei-vX.Y.Z CHANGELOG"` → `git push`
4. `git tag -a yei-vX.Y.Z -m "..."` → `git push origin yei-vX.Y.Z`
5. Frappe Cloud dashboard → bench → Apps → **Fetch Latest Updates** on this row
6. GitHub Releases page → New Release → pick tag → paste CHANGELOG section → Publish
7. If the patch is broadly useful, open an upstream PR to `frappe/ecommerce_integrations`
