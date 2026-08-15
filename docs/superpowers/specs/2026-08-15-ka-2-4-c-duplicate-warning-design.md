# 2026-08-15 Knowledge Assistant 2.4-C Duplicate Warning Design

## Goal

After batch SHA-256 precheck, warn if selected files already exist as indexed documents in the target collection. Confirmation still proceeds; the server remains the dedup authority.

## In scope

1. `DocumentService.find_indexed_by_checksums` — tenant + collection + indexed + not archived; max 20 SHA-256 hex digests.
2. Additive `POST /v1/documents/lookup` returning `{items:[{id,filename,collection_id,checksum}]}` with no bodies.
3. Desktop client wrapper; `BatchUploadDialog` advisory label after precheck and when the collection changes.
4. Lookup failures are fail-open: do not block confirm or create outbox.

## Out of scope

- Blocking/excluding duplicates by default
- Single-file upload dialog hashing on the GUI thread
- Batch failure summary
- Changing server dedup behavior

## Non-negotiables

- Tenant filter before lookup; collection allowlist via `require_collection`.
- Empty allowlist is deny-all (403/empty as existing contracts).
- Cross-tenant matches must not appear; do not leak existence via 404 vs 403 beyond current collection-scope rules.
- No outbox/HTTP write before confirm; lookup is read-only `knowledge:read`.

## Acceptance

- Document/API tests: match, other collection, other tenant, archived skipped, invalid checksum, too many checksums, deny-all/restricted.
- UI test: precheck then hint lists local filename; collection change re-queries; submit still enabled.
