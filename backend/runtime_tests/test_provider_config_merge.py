"""The connection config blob must not lose a concurrent writer's keys.

`provider_connection.config` is one JSON blob shared by writers that run on
different schedules: the per-user catalog snapshot (including the background
catalog refresh), the enabled map, the manual purpose overrides and the model
test results. Each rewrite merges the whole blob, so a writer that bases its
merge on a copy older than another writer's commit silently destroys that
writer's keys.

Observed before the fix: a model test committed `_model_tests`, and a catalog
refresh that had loaded its copy a moment earlier wrote the blob back without
it. The immediately following `/providers/models` listing then reported the
test as `untested`, roughly one run in ten.
"""

import unittest

from open_webui.internal.db import get_async_db_context
from open_webui.models.provider_connections import ProviderConnection, new_provider_connection
from open_webui.utils import providers
from sqlalchemy import delete

OWNER = 'config-merge-owner'
MODEL_RESULT = {'speech': {'status': 'failed', 'tested_at': 1, 'fingerprint': 'f'}}


class ConfigMergeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connection = new_provider_connection(
            provider_id='custom',
            name='Merge',
            auth_type='api_key',
            visibility='private',
            owner_id=OWNER,
            config={'base_url': 'https://example.invalid/v1'},
            credential=providers.encrypt_credential('synthetic-key'),
            status='configured',
            models=[{'id': 'provider:x:test', 'upstream_id': 'test', 'name': 'test'}],
            is_active=True,
        )
        async with get_async_db_context() as db:
            await db.execute(delete(ProviderConnection).where(ProviderConnection.owner_id == OWNER))
            db.add(self.connection)
            await db.commit()

    async def asyncTearDown(self):
        async with get_async_db_context() as db:
            await db.execute(delete(ProviderConnection).where(ProviderConnection.owner_id == OWNER))
            await db.commit()

    async def test_snapshot_write_keeps_a_concurrent_model_test_result(self):
        snapshot = {'fingerprint': 'stamp', 'models': [], 'updated_at': 1}

        async with get_async_db_context() as stale_db:
            # A background catalog refresh loads its own copy first.
            stale = await stale_db.get(ProviderConnection, self.connection.id)
            self.assertNotIn('_model_tests', stale.config or {})

            # Meanwhile a model test commits its result.
            async with get_async_db_context() as test_db:
                tested = await test_db.get(ProviderConnection, self.connection.id)
                tested.config = {**(tested.config or {}), '_model_tests': {'test': MODEL_RESULT}}
                await test_db.commit()

            # The refresh now writes the blob back from its older copy.
            await providers.write_snapshot(stale, OWNER, snapshot, stale_db)
            await stale_db.commit()

        async with get_async_db_context() as db:
            row = await db.get(ProviderConnection, self.connection.id)
            self.assertEqual(
                (row.config or {}).get('_model_tests'),
                {'test': MODEL_RESULT},
                'the catalog snapshot write discarded the model test result',
            )
            # The snapshot itself still landed.
            self.assertIn(OWNER, (row.config or {}).get('_models_by_user', {}))

    async def test_snapshot_write_keeps_manual_purpose_overrides(self):
        async with get_async_db_context() as stale_db:
            stale = await stale_db.get(ProviderConnection, self.connection.id)

            async with get_async_db_context() as user_db:
                curated = await user_db.get(ProviderConnection, self.connection.id)
                curated.config = {**(curated.config or {}), '_model_overrides': {'test': {'speech': 'openai-speech'}}}
                await user_db.commit()

            await providers.write_snapshot(stale, OWNER, {'fingerprint': 's', 'models': [], 'updated_at': 1}, stale_db)
            await stale_db.commit()

        async with get_async_db_context() as db:
            row = await db.get(ProviderConnection, self.connection.id)
            self.assertEqual((row.config or {}).get('_model_overrides'), {'test': {'speech': 'openai-speech'}})
