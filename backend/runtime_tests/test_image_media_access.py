"""Image/audio media features must explain a private-connection refusal.

An administrator configures one provider connection for image generation and
every user holding `features.image_generation` then calls it. Leaving that
connection `private` is the common misconfiguration, and it used to surface as
`404 Provider connection not found` — indistinguishable from a deleted
connection, so the administrator had nothing to act on. The list endpoint made
it worse by turning the 404 into a generic 400 "Failed to retrieve image
generation models".

The media resolver keeps its 404 contract (chat depends on it to keep
inaccessible models invisible); the explanation is added at the feature
boundary in `routers/images.py`.

Isolation notes: connections are created through the real API so the real
access-control path runs, but the image configuration is patched instead of
saved — writing it would leak into every other module sharing this database.
"""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from open_webui.main import app

PRIVATE_MESSAGE = 'private to its owner'


class ImageMediaAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from open_webui.models.config import Config

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://image-media', timeout=120
        )
        self.addAsyncCleanup(self.client.aclose)

        suffix = uuid.uuid4().hex[:10]
        self.admin_email = f'media-admin-{suffix}@example.invalid'
        self.user_email = f'media-user-{suffix}@example.invalid'
        self.created_connections = []

        await Config.upsert({'ui.enable_signup': True})

        admin = await self._call(
            'POST',
            '/api/v1/auths/signup',
            {'name': 'media-admin', 'email': self.admin_email, 'password': 'Admin-pass-123'},
        )
        self.admin_token = admin['token']
        self.admin_id = admin['id']
        if admin.get('role') != 'admin':
            from open_webui.internal.db import get_async_db_context
            from open_webui.models.users import Users

            async with get_async_db_context() as db:
                await Users.update_user_role_by_id(self.admin_id, 'admin', db=db)
            admin = await self._call(
                'POST',
                '/api/v1/auths/signin',
                {'email': self.admin_email, 'password': 'Admin-pass-123'},
            )
            self.admin_token = admin['token']
        self.assertEqual(admin.get('role'), 'admin')

        await self._call(
            'POST',
            '/api/v1/auths/add',
            {'name': 'media-user', 'email': self.user_email, 'password': 'User-pass-123', 'role': 'user'},
            token=self.admin_token,
        )
        signed = await self._call(
            'POST', '/api/v1/auths/signin', {'email': self.user_email, 'password': 'User-pass-123'}
        )
        self.assertEqual(signed.get('role'), 'user')
        self.user_token = signed['token']
        self.user_id = signed['id']

    async def asyncTearDown(self):
        from sqlalchemy import delete

        from open_webui.internal.db import get_async_db_context
        from open_webui.models.access_grants import AccessGrant
        from open_webui.models.provider_connections import ProviderConnection
        from open_webui.models.users import User

        if self.created_connections:
            async with get_async_db_context() as db:
                await db.execute(
                    delete(AccessGrant).where(
                        AccessGrant.resource_type == 'provider_connection',
                        AccessGrant.resource_id.in_(self.created_connections),
                    )
                )
                await db.execute(
                    delete(ProviderConnection).where(ProviderConnection.id.in_(self.created_connections))
                )
                await db.execute(
                    delete(User).where(User.email.in_([self.admin_email, self.user_email]))
                )
                await db.commit()

    async def _call(self, method, path, body=None, token=None, expected=200):
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        response = await self.client.request(method, path, json=body, headers=headers)
        payload = response.json() if response.content else None
        self.assertEqual(response.status_code, expected, (path, response.status_code, response.text[:400]))
        return payload

    async def _connection(self, name, visibility, upstream_id, access_control=None):
        connection = await self._call(
            'POST',
            '/api/v1/providers/connections',
            {
                'provider_id': 'custom',
                'name': name,
                'auth_type': 'api_key',
                'visibility': visibility,
                'api_key': 'sk-synthetic-media-key',
                'config': {'base_url': 'https://example.invalid/v1'},
                'access_control': access_control or [],
                'model_selections': [
                    {
                        'upstream_id': upstream_id,
                        'display_name': upstream_id,
                        'uses': {'image': 'openai-image'},
                        'source': 'manual',
                    }
                ],
            },
            token=self.admin_token,
        )
        self.created_connections.append(connection['id'])
        return connection['id']

    def _image_config(self, connection_id, model):
        return SimpleNamespace(
            ENABLE_IMAGE_GENERATION=True,
            ENABLE_IMAGE_PROMPT_GENERATION=True,
            IMAGE_GENERATION_ENGINE='provider',
            IMAGE_GENERATION_MODEL=model,
            IMAGES_PROVIDER_CONNECTION_ID=connection_id,
            IMAGE_SIZE='1024x1024',
            IMAGE_STEPS=4,
            IMAGE_EDIT_ENGINE='',
            IMAGE_EDIT_MODEL='',
            IMAGES_EDIT_PROVIDER_CONNECTION_ID='',
            IMAGE_EDIT_SIZE=None,
            USER_PERMISSIONS={'features': {'image_generation': True}},
        )

    async def test_private_connection_is_explained_not_hidden(self):
        connection_id = await self._connection('PrivateImage', 'private', 'private-imagen')
        config = self._image_config(connection_id, 'private-imagen')

        with patch('open_webui.routers.images.get_image_config', return_value=config):
            listings = await self.client.request(
                'GET', '/api/v1/images/models', headers={'Authorization': f'Bearer {self.user_token}'}
            )
            self.assertEqual(listings.status_code, 403, listings.text[:300])
            self.assertIn(PRIVATE_MESSAGE, listings.json()['detail'])

            generation = await self.client.request(
                'POST',
                '/api/v1/images/generations',
                json={'prompt': 'a cat', 'model': 'private-imagen', 'n': 1, 'size': '1024x1024'},
                headers={'Authorization': f'Bearer {self.user_token}'},
            )
            self.assertEqual(generation.status_code, 403, generation.text[:300])
            self.assertIn(PRIVATE_MESSAGE, generation.json()['detail'])

    async def test_shared_connection_is_reachable_by_other_users(self):
        connection_id = await self._connection(
            'SharedImage',
            'shared',
            'shared-imagen',
            access_control=[{'principal_type': 'anyone', 'principal_id': '*', 'permission': 'read'}],
        )
        config = self._image_config(connection_id, 'shared-imagen')

        with patch('open_webui.routers.images.get_image_config', return_value=config):
            models = await self._call('GET', '/api/v1/images/models', token=self.user_token)
        self.assertEqual([model['id'] for model in models], ['shared-imagen'])

    async def test_owner_is_unaffected(self):
        # The administrator who configured it keeps working: the explanation is
        # about sharing, not about locking the feature down.
        connection_id = await self._connection('PrivateOwner', 'private', 'owner-imagen')
        config = self._image_config(connection_id, 'owner-imagen')

        with patch('open_webui.routers.images.get_image_config', return_value=config):
            models = await self._call('GET', '/api/v1/images/models', token=self.admin_token)
        self.assertEqual([model['id'] for model in models], ['owner-imagen'])
