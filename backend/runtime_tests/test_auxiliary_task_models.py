"""Auxiliary tasks select their model per purpose and never borrow the chat model."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException
from open_webui.models.config import Config
from open_webui.routers import tasks as task_api
from open_webui.utils.auth import get_admin_user, get_verified_user

AUXILIARY_KEYS = [
    'task.model.default',
    'task.model.external',
    'task.model.title.default',
    'task.model.title.external',
    'task.model.tags.default',
    'task.model.tags.external',
    'task.model.followup.default',
    'task.model.followup.external',
    'task.model.query.default',
    'task.model.query.external',
]


class AuxiliaryTaskModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        persistent = patch.object(Config, 'PERSISTENT_ENABLED', True)
        persistent.start()
        self.addCleanup(persistent.stop)
        self.user = SimpleNamespace(id='auxiliary-owner', role='admin')
        self.app = FastAPI()
        self.app.include_router(task_api.router, prefix='/tasks')
        self.app.dependency_overrides[get_verified_user] = lambda: self.user
        self.app.dependency_overrides[get_admin_user] = lambda: self.user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://fixture')
        self.addAsyncCleanup(self.client.aclose)
        self.models = {
            'chat-local': {'connection_type': 'local'},
            'chat-remote': {'connection_type': 'external'},
            'aux-local': {'connection_type': 'local'},
            'aux-remote': {'connection_type': 'external'},
            'title-local': {'connection_type': 'local'},
            'title-remote': {'connection_type': 'external'},
            'tags-local': {'connection_type': 'local'},
            'tags-remote': {'connection_type': 'external'},
        }

    async def asyncTearDown(self):
        await Config.upsert({key: '' for key in AUXILIARY_KEYS})

    async def resolve(self, purpose, chat_model='chat-local'):
        model_id, _ = await task_api.get_task_model_generation_config(chat_model, self.models, purpose)
        return model_id

    async def test_purpose_model_overrides_shared_default(self):
        await Config.upsert(
            {
                'task.model.default': 'aux-local',
                'task.model.external': 'aux-remote',
                'task.model.title.default': 'title-local',
                'task.model.title.external': 'title-remote',
            }
        )
        self.assertEqual(await self.resolve('title', 'chat-local'), 'title-local')
        self.assertEqual(await self.resolve('title', 'chat-remote'), 'title-remote')
        # Other purposes keep using the shared default.
        self.assertEqual(await self.resolve('tags', 'chat-local'), 'aux-local')

    async def test_unset_purpose_inherits_shared_default(self):
        await Config.upsert({'task.model.default': 'aux-local', 'task.model.external': 'aux-remote'})
        for purpose in ('title', 'tags', 'followup', 'query'):
            self.assertEqual(await self.resolve(purpose, 'chat-local'), 'aux-local')
            self.assertEqual(await self.resolve(purpose, 'chat-remote'), 'aux-remote')

    async def test_each_purpose_selects_its_own_model(self):
        await Config.upsert(
            {
                'task.model.default': 'aux-local',
                'task.model.external': 'aux-remote',
                'task.model.title.default': 'title-local',
                'task.model.tags.default': 'tags-local',
            }
        )
        self.assertEqual(await self.resolve('title'), 'title-local')
        self.assertEqual(await self.resolve('tags'), 'tags-local')
        self.assertEqual(await self.resolve('followup'), 'aux-local')
        self.assertEqual(await self.resolve('query'), 'aux-local')

    async def test_missing_configuration_never_uses_the_chat_model(self):
        for purpose in ('title', 'tags', 'followup', 'query'):
            with self.assertRaises(HTTPException) as error:
                await self.resolve(purpose)
            self.assertEqual(error.exception.status_code, 409)
            self.assertNotIn('chat-local', error.exception.detail)
            self.assertNotIn('chat-remote', error.exception.detail)

    async def test_purpose_model_missing_from_the_catalog_is_not_silently_ignored(self):
        await Config.upsert({'task.model.title.default': 'not-in-catalog'})
        with self.assertRaises(HTTPException) as error:
            await self.resolve('title')
        self.assertEqual(error.exception.status_code, 409)

    async def test_task_config_round_trip_keeps_purpose_models(self):
        exposed = (await self.client.get('/tasks/config')).json()
        for field in (
            'TASK_MODEL_TITLE',
            'TASK_MODEL_TITLE_EXTERNAL',
            'TASK_MODEL_TAGS',
            'TASK_MODEL_TAGS_EXTERNAL',
            'TASK_MODEL_FOLLOWUP',
            'TASK_MODEL_FOLLOWUP_EXTERNAL',
            'TASK_MODEL_QUERY',
            'TASK_MODEL_QUERY_EXTERNAL',
        ):
            self.assertIn(field, exposed)

        payload = dict(exposed)
        payload.update(
            {
                'TASK_MODEL_TITLE': 'title-local',
                'TASK_MODEL_TITLE_EXTERNAL': 'title-remote',
                'TASK_MODEL_TAGS': 'tags-local',
                'TASK_MODEL_TAGS_EXTERNAL': '',
                'TASK_MODEL_FOLLOWUP': '',
                'TASK_MODEL_FOLLOWUP_EXTERNAL': '',
                'TASK_MODEL_QUERY': '',
                'TASK_MODEL_QUERY_EXTERNAL': '',
            }
        )
        self.assertEqual((await self.client.post('/tasks/config/update', json=payload)).status_code, 200)
        stored = (await self.client.get('/tasks/config')).json()
        self.assertEqual(stored['TASK_MODEL_TITLE'], 'title-local')
        self.assertEqual(stored['TASK_MODEL_TITLE_EXTERNAL'], 'title-remote')
        self.assertEqual(stored['TASK_MODEL_TAGS'], 'tags-local')
        self.assertEqual(stored['TASK_MODEL_TAGS_EXTERNAL'], '')

        self.assertEqual(await self.resolve('title', 'chat-local'), 'title-local')
        self.assertEqual(await self.resolve('title', 'chat-remote'), 'title-remote')
        self.assertEqual(await self.resolve('tags', 'chat-local'), 'tags-local')

    async def test_payload_without_purpose_models_keeps_the_stored_selection(self):
        await Config.upsert({'task.model.title.default': 'title-local'})
        payload = (await self.client.get('/tasks/config')).json()
        for field in (
            'TASK_MODEL_TITLE',
            'TASK_MODEL_TITLE_EXTERNAL',
            'TASK_MODEL_TAGS',
            'TASK_MODEL_TAGS_EXTERNAL',
            'TASK_MODEL_FOLLOWUP',
            'TASK_MODEL_FOLLOWUP_EXTERNAL',
            'TASK_MODEL_QUERY',
            'TASK_MODEL_QUERY_EXTERNAL',
        ):
            payload.pop(field, None)

        # A cached older frontend does not send the purpose fields at all.
        self.assertEqual((await self.client.post('/tasks/config/update', json=payload)).status_code, 200)
        stored = (await self.client.get('/tasks/config')).json()
        self.assertEqual(stored['TASK_MODEL_TITLE'], 'title-local')
        self.assertEqual(await self.resolve('title', 'chat-local'), 'title-local')


if __name__ == '__main__':
    unittest.main()
