# Local Ollama fixtures

These fixtures are **structurally representative, synthetic** inputs for the
Ollama local runtime collector's parser and contract tests. They carry no
credentials, no Authorization material, no real model inventory and no
filesystem paths. Model names (`test-model:latest`, `other-model:1b`),
digests (synthetic `sha256:<64 hex>` strings) and sizes are synthetic;
numeric values are chosen to exercise the required parsing paths rather than
replay a live reading. The collector validates the digest form
(`sha256:` + 64 lowercase hex) and requires listing/loaded digest agreement
before accepting effective context; `tags-invalid-digest.json`,
`ps-digest-mismatch.json` and `ps-digest-missing.json` exercise the
degraded-identity paths, and no digest value may appear in any normalized
output.

Source of the shapes: bounded read-only reconnaissance of the local Ollama
HTTP interface (`/api/version`, `/api/tags`, `/api/ps`) against the live
local runtime (Ollama 0.33.1) on **2026-09-04**, plus the installed binary's
serialization table for the `/api/ps` `context_length` field name (recorded
in `docs/poc-evidence.md`, "2026-09-04 M1 Ollama local runtime
reconnaissance"). Structure only: shapes are recorded, inventory values are
not.

## Files

### Version probe (reachability)

- `version-ok.json` — evidenced `{"version": "<string>"}` envelope with one
  tolerated additive key. A validated exchange is the reachability fact.
- `version-malformed.json` — object without a string `version`. The probe
  must fail closed (`schema_changed`, runtime not validated as Ollama).

### Model listing (`/api/tags`, presence)

- `tags-present.json` — listing containing the configured synthetic target
  `test-model:latest` plus an unrelated model. Presence comes from exact
  listed-name identity; `details.context_length` (model-file metadata) is
  present in the entry but must never be read as configured or effective
  context.
- `tags-missing.json` — listing without the configured target. The runtime
  explicitly confirmed the model absent: `model_presence: "missing"`.
- `tags-duplicate-names.json` — the same `name` listed twice. Duplicate
  identity is drift, not a redundant entry; parser must fail closed.
- `tags-malformed-entries.json` — `models` containing a non-object entry
  and an entry without a string `name`. Parser must fail closed.
- `tags-schema-changed.json` — a plausibly evolved envelope (`{"items":
  [...]}`) that is not the observed shape. Parser must map to
  `schema_changed`, never partially decode.

### Loaded models (`/api/ps`, effective context)

- `ps-loaded.json` — the configured target loaded with a positive integer
  `context_length`. This is the only accepted effective-context evidence.
- `ps-not-loaded.json` — empty `models` list. Normal operation (nothing
  loaded): effective context is unknown, not zero and not inferred.
- `ps-other-loaded.json` — only an unrelated model loaded. The configured
  target is present (per tags) but not loaded: effective context unknown.
- `ps-missing-context-length.json` — the target listed **without** a usable
  `context_length`. The evidenced contract requires the field on loaded
  entries; its absence is drift, never a guessed value.
- `ps-schema-changed.json` — a plausibly evolved envelope (`{"loaded":
  [...]}`) that is not the observed shape. Parser must fail closed.

## Assertions expected of the collector/parser

- healthy local runtime yields `windows: []` and no quota, unlimited,
  percentage or subscription semantics of any kind;
- configured and effective context stay independent: either may be present
  while the other is omitted, and neither is inferred from the other;
- malformed, duplicate, drifted and oversized responses fail closed to the
  documented statuses without partial decoding;
- failures keep already-validated facts (`reachable`, presence) and never
  guess missing ones (`model_presence: "unknown"` on failures);
- no endpoint URL, path, raw response fragment or credential-shaped string
  ever appears in normalized output, diagnostics or the snapshot.
