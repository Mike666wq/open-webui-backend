#!/usr/bin/env python3
"""Run resource migrations on an isolated PostgreSQL database."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import sys
import tempfile

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import BigInteger, Column, MetaData, String, Table, Text, create_engine, inspect, select, text

from dev_backend import REPO_ROOT, configuration


def prepare_environment(database_url: str, root: Path) -> None:
    env = configuration(REPO_ROOT, 8081, dict(os.environ))
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "static").mkdir(parents=True, exist_ok=True)
    env.update({
        "DATA_DIR": str(root / "data"),
        "STATIC_DIR": str(root / "static"),
        "DATABASE_URL": database_url,
        "WEBUI_SECRET_KEY": secrets.token_urlsafe(48),
        "WEBUI_SECRET_KEY_FILE": str(root / "secret"),
        "FRONTEND_BUILD_DIR": str(root / "no-frontend"),
    })
    os.environ.clear()
    os.environ.update(env)


def migration_config() -> Config:
    config = Config(str(REPO_ROOT / "backend" / "open_webui" / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "backend" / "open_webui" / "migrations"))
    return config


def verify(database_url: str, root: Path) -> None:
    prepare_environment(database_url, root)
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    config = migration_config()
    engine = create_engine(database_url, pool_pre_ping=True)

    # Build the real pre-lifecycle schema, then simulate duplicate tombstones
    # from the superseded implementation. The migration must preserve every
    # alias while labeling the entry as non-restorable.
    command.upgrade(config, "0bd252b206fb")
    metadata = MetaData()
    legacy = Table(
        "resource_recycle_chat", metadata,
        Column("id", String(), primary_key=True),
        Column("original_id", Text(), nullable=False),
        Column("user_id", Text(), nullable=False),
        Column("title", Text(), nullable=False),
        Column("size_bytes", BigInteger(), nullable=False),
        Column("deleted_at", BigInteger(), nullable=False),
        Column("expires_at", BigInteger(), nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(legacy.insert(), [
            {"id": "old-tombstone-1", "original_id": "same-chat", "user_id": "user-1",
             "title": "Earlier tombstone", "size_bytes": 0, "deleted_at": 10, "expires_at": 20},
            {"id": "old-tombstone-2", "original_id": "same-chat", "user_id": "user-1",
             "title": "Latest tombstone", "size_bytes": 0, "deleted_at": 11, "expires_at": 21},
        ])

    command.upgrade(config, "head")
    engine.dispose()
    verification_engine = create_engine(database_url, pool_pre_ping=True)
    with verification_engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        assert {"resource_entry", "resource_task", "resource_rule", "alembic_version"} <= tables, tables
        row = connection.execute(text(
            "SELECT state, title, data FROM resource_entry WHERE type='chat' AND resource_id='same-chat'"
        )).mappings().one()
        assert row["state"] == "legacy", row
        assert row["title"] == "Latest tombstone", row
        data = json.loads(row["data"])
        assert data["reason"] == "legacy_body_unavailable", data
        assert set(data["legacy_ids"]) == {"old-tombstone-1", "old-tombstone-2"}, data
        indexes = {item["name"] for item in inspect(connection).get_indexes("resource_entry")}
        assert "ix_resource_visibility" in indexes, indexes
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == ScriptDirectory.from_config(config).get_current_head(), version
    verification_engine.dispose()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="owui-resource-pg-") as temporary:
        root = Path(temporary)
        database_url = os.environ.get("TEST_POSTGRES_URL")
        server = None
        if not database_url:
            try:
                import pgserver
            except ImportError as error:
                raise SystemExit(
                    "Set TEST_POSTGRES_URL or install the local test helper: "
                    ".venv/bin/pip install pgserver==0.1.4"
                ) from error
            server = pgserver.get_server(root / "pgdata", cleanup_mode="delete")
            database_url = server.get_uri()
        try:
            verify(database_url, root)
            print(json.dumps({"postgres_resource_migration": "passed"}))
        finally:
            if server:
                server.cleanup()


if __name__ == "__main__":
    main()
