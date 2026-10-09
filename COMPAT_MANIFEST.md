# file-memory COMPAT_MANIFEST

Tracks breaking changes against the Hermes plugin compat window.

## Demo external sync removed (2026-09-24)

- **What changed**: The optional external Demo API sync was removed from
  `__init__.py` and `config_schema.py`. Deleted: `demo_api_url` /
  `sync_to_demo` config fields, the `demo_api_url` default + config read,
  the `_sync_to_demo` method (its only `import requests` consumer), and the
  `if self.demo_api_url:` gate in the auto-extract path.
- **Impact**: Any `plugins.entries.file-memory.demo_api_url` /
  `sync_to_demo` values in an existing `config.yaml` are now read but
  silently ignored — the fields no longer exist in the schema. file-memory is
  now a pure standalone local system (zero network egress, zero optional
  external dependency). No stored data is lost.
- **Scope kept**: The local M6 `TCMPromotionEngine` promotion pipeline
  (deep-writes into `local_semantic` / native `memories/`) is a separate
  local mechanism and is NOT affected — only the remote-POST sync path was
  removed.
- **Acceptance check**: `grep -rniE 'demo|_sync_to_demo|demo_api_url' *.py config_schema.py`
  in the plugin dir must hit 0 in code. Active docs (README / IMPLEMENTATION /
  extractor/README) carry no Demo feature description; the two historical
  evidence docs (PREFETCH / RUNTIME_EVIDENCE) retain old test-fixture text but
  are annotated with a D1 note.

## `ollama_url` removed (2026-09-23)

- **What changed**: The `ollama_url` provider config field was removed from
  `config_schema.py` and `__init__.py`. The `EmbeddingClient` class,
  `process_pending_embeddings` tool, cosine-similarity deduplication path,
  and `pending_embed.json` queue were all deleted.
- **Impact**: Any `plugins.entries.file-memory.ollama_url` value in an
  existing `config.yaml` will be read but silently ignored — the field no
  longer exists in the schema. No data is lost; embeddings stored in
  `storage/embedding_cache.json` are inert.
- **Migration**: Nothing required for keyword-only mode (the new default).
  Model-assisted search now routes through Hermes `ctx.llm` on the host
  main-model path (no auxiliary task, no user-configurable model/provider).
  The former `auxiliary.filememory_consolidation` config block was removed in
  the 2026-09-30 alignment: any existing value is silently ignored.
- **Acceptance check (PRD item 8)**: `grep "ollama_url" COMPAT_MANIFEST.md`
  must hit ≥ 1.
