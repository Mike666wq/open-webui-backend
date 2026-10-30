# Markdown folder import (P-006 r5 contract)

This contract applies to the version 2 knowledge-base folder importer. Existing notes import, generic uploads, and knowledge file APIs retain their previous behavior. Upgrade does not repair or re-import existing knowledge files.

## Content and access

The stored original Markdown retains its text and Markdown image syntax. Only confirmed local images are rewritten to same-origin `/api/v1/files/{file_id}/content` references; external references remain unchanged and are not fetched. Images are attached to their target knowledge directory without vectorization. The separately generated index text preserves prose and replaces image syntax with a short marker or alt text, excluding image URLs, data URIs and long asset paths. Existing indexed-text APIs and edits operate on index text only; original-file preview uses the protected file-content API. Version 2 metadata under `file.meta.data.markdown_import` identifies version, source relative path, source-byte SHA-256, operation UUID and `reference_format: file-url`. Old documents are not guessed or automatically repaired.

## Write and recovery protocol

Before uploading, the client invokes `POST /api/v1/knowledge/{id}/directory/batch`, including an empty `directories` array for root-only imports, to preflight write permission. Each `POST /api/v1/knowledge/{id}/import/file` multipart request repeats authorization and target-directory validation before persistence and carries `file`, nullable `directory_id`, normalized `source_path`, source-byte `source_hash`, stable UUID `operation_id`, `kind` (`markdown` or `image`) and, for Markdown only, non-blank `index_text`. Existing generic upload and `file/add` remain unchanged; an unavailable import endpoint requires upgrading the backend.

The server validates type, size, paths and access, obtains a side-effect-free, versioned provider target, then commits and reads back a managed File reservation before any storage I/O. That record owns the immutable target, operation identity, status, lease token, lease expiry and generation. Provider prepare, upload-prepared and delete-prepared operations preserve existing provider APIs; prepared deletion occurs only at the existing lifecycle purge stage and treats missing objects idempotently while retaining retryable errors. Never persist credentials in a target or expose its internal location to the client.

The operation identity converges to one File for a given owner, knowledge and operation UUID. Mismatched input returns 409. An uncertain response is reconciled against persisted operation state; it is not automatically replayed. A new explicit retry of a failed operation uses a fresh UUID and target. A live lease and token guard completion; expired or failed imports are recovered through a narrow scan and existing preview → task → recycle → seven-day retention → purge workflow, independent of the general temporary-upload setting. Successful or referenced files are protected. Cleanup status identifies recycle versus purge: a file in the recycle bin is not physically deleted yet.

Successful responses provide `file_id`, `directory_id`, `source_path`, `operation_id`, `status: completed` and `indexed` (false for images). Structured FastAPI `detail` errors contain `code`, `stage`, `message`, `operation_id`, optional `file_id` and `cleanup_status`, with optional cleanup task and phase. Missing/blank Markdown index text is rejected before storage with `422 empty_index_text`. Authorization, validation, reservation, upload, attach, index, confirmation, recovery and cleanup failures remain distinguishable.

## Ordering, conflicts and reports

Resolve paths and conflicts first, process images before dependent Markdown regardless of selection order, and only reference images confirmed attached in the target directory. Skip-existing images must still enter the reference map. De-duplication is confined to the same account, knowledge base, target directory, filename, size and source hash; different directories require separate file records. Do not silently fall back to root when a directory mapping is missing. The default conflict strategy is skip; copy chooses a unique destination; overwrite keeps the old item until its replacement is confirmed and reports cleanup failures.

Reports distinguish completed, completed with errors, failed and cancelled, including unconfirmed requests. Each selected item has one final classification, and failures show relative path, stage, reason and cleanup state; pending and invalid paths remain visible. The most recent bounded report is isolated by account and knowledge base and contains no tokens, file content, binary data or absolute source paths; unavailable local storage is reported to the user.

## Existing data and verification

Upgrading never changes existing imported files. Existing placeholder text cannot reconstruct missing original Markdown, image alt text, position or context. If the user later chooses recovery using source files, preview and back up the existing content first, explicitly choose overwrite or copy, and independently verify images and retrieval. Never infer that a filename match reconstructs the original.

Automated tests and CI do not establish real-cloud, production, isolated real-sample, accessibility or Android-device acceptance. Each requires separate evidence.

## Version 2 source knowledge access (P-006 r4)

A server-registered v2 import is bound to its immutable source knowledge base. Public reads require a completed and visible file, an active source knowledge base, the original association, and current read permission on that source; uploader ownership and other shared containers do not override this check. Admin access follows the r4 active-resource exception. Writes require current source knowledge write permission. Invalid or incomplete bindings fail closed; ordinary uploads and legacy files retain their existing rules. Cross-knowledge attachment or indexing of a v2 file is rejected with 409 `knowledge_import_scope_mismatch`. Internal recovery is separately fenced and cannot be invoked through a client-controlled bypass. Protected responses use private, no-store caching. Operation confirmation is limited to the uploader with current source write access and exposes no content or storage target.

## Trusted indexing and shared-chat filtering (P-006 r5)

During a v2 Markdown import, the import route may let `process_file` index the still-`processing` reservation before the knowledge association exists. This exception is a server-created context containing the file, owner, immutable source knowledge, collection, lease token and generation. `process_file` re-reads the committed row and accepts the context only while every proof field matches, the state is `processing`, and the lease is unexpired. The context exits and is restored before the route continues. It is not a request parameter or public bypass; direct callers without the context still receive 404 for an inaccessible or incomplete v2 import.

Every shared-chat response re-evaluates `files` references against the requester's current source-knowledge read permission before serialization. A ref is removed in full, including its nested `file.data.content` and URL, when it identifies an inaccessible v2 file. Removing a knowledge grant or group membership takes effect on the next response. Ordinary and legacy file references retain their existing shared-chat behavior.

The original Markdown preview keeps authenticated image replacements and their temporary data URLs only in memory. Closing the preview, changing items, opening a replacement request, an item load failure, or destroying the component aborts outstanding requests and clears the rendered Markdown and temporary image data; neither data URLs nor raw content are written back to the file, report, or persistent cache.
