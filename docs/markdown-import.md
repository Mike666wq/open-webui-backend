# Markdown folder import (P-006, S4)

The markdown import lets the client push a local folder of notes (`.md` files plus their referenced images) into an existing knowledge base. This stage ships the backend groundwork: batch directory get-or-create, attach-only file placement for images, and the documented conflict/idempotency rules the frontend importer must follow. The import UI itself lands in a later stage.

## Batch directory creation

`POST /api/v1/knowledge/{id}/directory/batch` with body `{"directories": ["19.Zabbix", "linux基础/sub"]}` (knowledge-base **write** permission required, same as `dirs/create`). Every path is get-or-created level by level, shallowest first. The response maps each **normalized** path — including intermediate levels that were not requested explicitly — to its directory ID:

```json
{ "19.Zabbix": "<id>", "linux基础": "<id>", "linux基础/sub": "<id>" }
```

The endpoint is idempotent: same-name directories at the same level are reused, never duplicated (enforced by the `(knowledge_id, parent_id, name)` unique constraint). A concurrent writer that wins the insert race is handled by re-indexing and retrying once. All paths are validated **before any write**, so one bad entry never leaves partial directories behind. `KNOWLEDGE_DIRECTORY_CREATED` events are published only for directories actually created.

## Path normalization and security

Directory paths are slash-joined strings; segments are stored verbatim — unicode, numeric prefixes (`19.Zabbix`), spaces and dots are preserved, never slugified. Rules, applied by `_normalize_directory_path`:

- Percent-decoding happens **first**: `%E7%AC%94%E8%AE%B0` → `笔记`, `x%20y` → `x y`, and `%2F` decodes into a real separator (`a%2Fb` == `a/b`). Path boundaries are established after decoding, never before.
- Because decoding precedes validation, `%2e%2e` cannot smuggle a traversal: any `..` segment, absolute path (`/abs`, and after decoding `%2fetc` → `/etc`), backslash separator, Windows drive-letter segment (`C:/x`) or NUL byte is rejected with `400` — no folding, no clamping to root.
- Empty and `.` segments collapse; `linux基础//sub/.` == `linux基础/sub`. A path that normalizes to nothing (`""`, `"."`) is rejected.
- Decoding is single-pass: `%252e%252e` becomes the literal directory name `%2e%2e`. Directory names live in the database, not on a filesystem, so this is safe.

Same-name rule: `19.Zabbix.md` (file) and `19.Zabbix/` (directory) are independent first-class objects — files and directories never share a namespace constraint.

## Image handling: attach-only placement

`POST /api/v1/knowledge/{id}/file/add` accepts an optional `process` flag (default `true`, fully backward compatible). With `process: false` the file is attached to the knowledge base and its directory **without vectorization**: the extracted-content gate (`FILE_NOT_PROCESSED`) does not apply, and `process_file` is never invoked, so images never enter the retrieval index. Authorization is unchanged — knowledge write access plus file read access are still enforced.

The importer uploads each image with `process=false`, then calls `file/add` with the `directory_id` from the batch-directory response. Images stay searchable by path through the knowledge tree but are excluded from RAG. When a note is vectorized, image references inside its content are replaced with `[图片 <文件名>]` placeholders; external `http(s)` image links are kept verbatim and never downloaded.

## Reference forms in note content

Notes may reference files three ways, and the importer preserves each:

1. **Note-attached files**: `data://<fileId>` URLs pointing at the note's own attachments (uploaded with `process=false` when they are images).
2. **Knowledge-base files**: the file's knowledge-base access URL, used when the file already lives in the target knowledge base.
3. **External links**: `http(s)` URLs are preserved as-is.

## Idempotency and conflict strategy

- **Knowledge-base side**: the idempotency key is `(normalized directory path, filename)`. Re-importing the same folder into the same knowledge base reuses existing directories and updates existing files instead of creating duplicates.
- **Note side**: notes carry `meta.markdown_import.content_hash` (plus the source title). A note whose hash matches the on-disk content is skipped entirely.
- **Conflict strategy** when content changed: `skip` (default — keep the knowledge-base copy untouched), `overwrite` (call `POST /{id}/file/update`, which deletes the file's vectors from the knowledge collection and re-processes the new content), or `create copy` (import as a new file). The importer surfaces this choice; the backend already supports all three.

## Known limitations

- `.md` notes are vectorized as plain text (`TextLoader`); Markdown structure is not chunked specially.
- Image references inside vectorized note content become `[图片 <文件名>]` placeholders; images themselves are never vectorized.
- External image links are stored verbatim; fetching them requires manual download.
- Directory names are database records, not filesystem paths: no symlink/case-folding semantics, and double-encoded sequences stay literal.

Automated coverage lives in `backend/runtime_tests/test_knowledge_import.py` (CI, `scripts/test_provider_runtime.py`). Import of a real folder, image placement in the UI, and overwrite behavior require manual acceptance after CI.
