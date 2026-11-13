# Workspace Loop Evidence — 用户 / 对话 / 笔记 / 知识库

Date: 2026-11-13 (Asia/Shanghai)
Branches: `codex/workspace-loop-closed` in both `open-webui-backend` and `open-webui-frontend`.

## Implemented

### Backend
- New server-owned loop router: `backend/open_webui/routers/workspace_loop.py`.
  - `GET /api/v1/notes/loop/readiness`
  - `GET /api/v1/notes/{id}/knowledge/readiness`
  - `POST /api/v1/notes/{id}/knowledge/{knowledge_id}/sync`
  - `DELETE /api/v1/notes/{id}/knowledge/{knowledge_id}/snapshot`
  - `POST /api/v1/notes/from-chat`
- Mounted before `notes.router`, so literal loop paths win over `/notes/{id}`.
- Canonical fingerprint is server-owned:
  `sha256(JSON.stringify([canonicalTitle, content]))` with UTF-8/no-ASCII-escaping semantics.
- Note list/search now expose `write_access`, so the frontend can gate save/menu actions.
- Index state now writes both `File.data.status` and `File.meta.index_state[knowledge_id]`.
- `track_indexing()` accepts a coroutine or Task; shutdown drains tracked indexing tasks.
- Agent `add_text_to_knowledge()` links the KB relation before scheduling indexing.
- Frontend modal is now readiness/sync/remove based; no upload→rename→content→add-saga in the component.
- Chat knowledge/note pickers now expose readable errors, Retry, and reset on account/token change.
- i18n keys added for both zh-CN and en-US; canonical tests cover writable base filtering and server states.

## Verification evidence

### Backend
```text
.venv/bin/python -m unittest discover -s tests -v
Ran 68 tests in 0.075s
OK
```

```text
.venv/bin/python scripts/check_source.py
exit=0
pythonFilesParsed=378
```

```text
WEBUI_SECRET_KEY=... PYTHONPATH=backend .venv/bin/python -c "from open_webui.main import app; ..."
ROUTES_LOADED 662
LOOP_READINESS True
NOTE_SYNC True
FROM_CHAT True
ORDER_OK True
```

```text
scripts/check_source.py                    exit=0
scripts/test_resource_lifecycle.py        exit=0 (43 cases)
scripts/test_provider_runtime.py          exit=0
scripts/test_provider_chat_integration.py exit=0 (18 checks)
```

```text
.venv/bin/ruff check --select E9,F63,F7,F82 <changed backend files>
All checks passed!
```

### Frontend
```text
npx vitest run src/lib/components/notes/ src/lib/utils/api-error.test.ts \
  src/lib/i18n/locales/i18n-parity.test.ts \
  src/lib/components/chat/MessageInput/workspace-pickers.test.ts
Test Files  10 passed (10)
Tests       142 passed (142)
```

```text
npm run test:frontend -- --run
full_vitest_exit=0
Test Files  53 passed (53)

npm run frontend:test
frontend_test_exit=0 (3 tests)

NODE_OPTIONS=--max-old-space-size=8192 npm run build
build_exit=0
✓ 6593 modules transformed
✓ built in 1m 44s
```

Known baseline:
```text
npm run check
svelte-check found 5782 errors and 191 warnings in 325 files
```
This is repository-wide legacy type debt; it is not reported as green. Targeted tests above are the acceptance evidence for the changed loop.

### Mint live data
The existing instance was repaired so the data matches the new contract:

```text
REINDEX 3747e09f 200
REINDEX 4cd0984a 200
REINDEX 6489bb5d 200
REINDEX f3c743e2 200
REINDEX bbb11ae8 200
NOTE_VECTOR_MATCH_COUNT 32
CANONICAL_FP_MATCH True
KB_LINKED 1
NOTE_FILE_STATUS completed completed completed
```

- The `C语言` KB was rebuilt at 1024 dimensions for `BAAI/bge-m3`; all five linked files reindexed successfully.
- The note snapshot fingerprint now matches the frontend canonical fingerprint.
- The note's vector chunks are queryable by `file_id`.
- `open-webui` was recreated healthy with RAG embedding env vars and restored provider keys.
- C语言 KB access grants include read/write.
- Final live check: `open-webui` healthy; `SNAPSHOT_STATE synced`; `INDEX_STATE completed`; `KB_GRANTS [('*','read'), ('*','write')]`.

## Honest remaining scope

- The new backend/frontend code is on local feature branches; it has not been pushed, opened as a PR, or deployed as a new image to mint.
- The mint runtime data is repaired, but the running old image does not expose the new HTTP routes until a new image is built/published.
- Browser/device/production acceptance was not run against the new image.
- `npm run check` remains non-green due pre-existing repository type debt; targeted workspace-loop tests and backend source/runtime checks pass.
