# AGENTS.md — Open WebUI Backend

Python / FastAPI backend for the Open WebUI fork stack. The source was imported from the **complete
official Open WebUI v0.12.2 backend** at
`open-webui/open-webui@6a18585ff4c2e5f6805f682222cfbc46dc669ef8` and is pinned in
`backend.upstream.json` (repository, upstream repo, version, commit, source archive URL + SHA-256,
and the pinned `ghcr.io/open-webui/open-webui:v0.12.2` base-image digest).

The backend **consumes** the web bundle published by `Mike666wq/open-webui-frontend`; it does not
build one. Accounts, models and conversations stay managed here.

## Setup commands

- Install deps: `uv sync` (or use the checked-in `.venv`, Python 3.11)
- Start dev:    `python backend/dev_backend.py` (isolated local launcher)
- Build image:  `docker build -t <tag> .` (replaces `/app/backend/open_webui` in the pinned base image)
- Unit test:    `python -m unittest discover -s tests -v`
- Runtime test: `python scripts/test_resource_lifecycle.py`, `scripts/test_provider_runtime.py`,
  `scripts/test_provider_chat_integration.py`, `scripts/test_resource_postgres.py`
- Source check: `python scripts/check_source.py` (validates upstream + frontend locks, no app import)
- Lint/format:  `ruff check .` / `ruff format .` (line length 120, single quotes)

Use the venv interpreter explicitly: `.venv/bin/python`, not the system Python 3.14.

## Project layout

- `backend/open_webui/` — the application package (upstream layout, patched)
- `backend/open_webui/routers/` — HTTP routers; `providers.py`, `provider_auth.py`, `kimi_auth.py`,
  `recordings.py`, `resources.py` are fork-specific
- `backend/open_webui/models/` — ORM models; `provider_connections.py`, `recordings.py`,
  `resources.py` are fork-specific
- `backend/open_webui/utils/` — helpers; `providers.py`, `provider_capabilities.py`,
  `provider_stream.py`, `resource_service.py`, `resource_visibility.py`, `chatgpt_auth.py`,
  `kimi_auth.py` are fork-specific
- `backend/open_webui/migrations/versions/` — Alembic migrations (upstream chain + fork revisions)
- `backend/runtime_tests/` — integration-style runtime tests
- `scripts/` — CI-mirroring test scripts, frontend sync, image smoke
- `tests/` — source-level unit tests
- `docs/` — resource management, provider connections, recording contracts

## Fork-specific features and their invariants

- **Resource lifecycle** (`/api/v1/resources`): preview → idempotent task → purge, never a direct
  delete. 7-day recycle window. `utils/resource_visibility.py` installs a global SQLAlchemy
  `do_orm_execute` listener so recycled rows are invisible to lists, deep links and search, and
  writes referencing them return **409**. `resource_worker()` ticks every 30 s for lease expiry,
  failed-purge retry and expired recycle entries. Preserve all three.
- **Provider connections** (`/api/v1/providers`): each connection is an owner-scoped **DB row**
  (not a `Config` entry) with Fernet-encrypted credentials, a catalog lease and typed per-use
  capabilities. Model IDs stay stable as `connection_id:upstream_id`.
- **Recordings**: durable DB-leased jobs, ffmpeg chunks ≤10 min, `interrupted` is terminal and
  must not auto-replay.
- **Frontend serving**: `utils/frontend_routes.py` validates the manifest digest against
  `frontend.lock.json` and restricts SPA fallback to manifest page routes.

## Upstream patch discipline

- Upstream files are patched surgically; **nothing upstream is deleted or gutted**. Do not remove an
  upstream router/model/util to make room for a fork feature.
- `env.py`, `tasks.py`, `internal/db.py`, `tools/` and `storage/provider.py` are currently
  untouched upstream files — keep it that way unless the brief explicitly says otherwise.
- Upstream `CHANGELOG.md` is byte-identical to upstream. Fork work is documented in `README.md` and
  `docs/`.
- New schema changes need an Alembic migration under `backend/open_webui/migrations/versions/`.

## Frontend lock discipline

`frontend.lock.json` must point at a **published** `frontend-<SHA12>` release from
`Mike666wq/open-webui-frontend` and carry the matching `archiveSha256` and `manifestSha256`.
`scripts/sync_frontend.py --check` and `scripts/check_source.py` enforce this in CI. The lock must
agree with `open-webui-frontend/apps/client/frontend.lock.json` — check both before changing one.

## PR & commit conventions

- Branch from `main` as `codex/<feature-slug>`; never push to `main` directly
- Conventional commits (`feat:` / `fix:` / `docs:` / `test:`)
- `backend.yml` runs source checks, unit tests, resource-lifecycle and provider runtime tests, then
  the multi-arch image build + smoke + publish. `frontend-sync.yml` picks up the newest published
  frontend.
