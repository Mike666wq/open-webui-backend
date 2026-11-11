"""Media purposes resolve to one provider connection, not a parallel config set.

Transcript, speech, image and realtime voice each used to carry their own
engine, URL and plaintext key. The instance also held provider connections that
declare those same services, so two authorities described one call path. They
drifted: production ended up with speech pointing at a transcription URL and
image holding a plaintext vendor key, while a shared MiniMax connection
declaring both services sat unused.

These tests pin the replacement contract:

* assignment is explicit (connection + whitelisted model), never inferred
* a private connection may not back a purpose every user calls
* the legacy block stays readable for revert but stops being authoritative
* config GET never returns a secret
* a legacy full-endpoint URL is normalised so the relative path is added once

The last point was not theoretical: `image_generation.openai.api_base_url`
held `…/v1/images/generations` and the call path appended `/images/generations`
again, so the live instance was calling `…/images/generations/images/generations`.
"""

import unittest
import uuid

from open_webui.main import app
from open_webui.models.config import Config
from open_webui.utils.media_assignment import (
    ASSIGNMENT_ERROR_MODEL,
    ASSIGNMENT_ERROR_NOT_CONFIGURED,
    ASSIGNMENT_ERROR_PRIVATE,
    MIGRATION_RETIRED,
    TRANSCRIPTION,
    IMAGE,
    SPEECH,
    legacy_state,
    normalize_base_url,
    read_assignment,
    resolve_media_target,
    restore_secrets,
)


class AssignmentHarness(unittest.IsolatedAsyncioTestCase):
    """Shared setup: an admin, a normal user, and one shared API-key connection."""

    async def asyncSetUp(self):
        import httpx

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://p009', timeout=120
        )
        self.addAsyncCleanup(self.client.aclose)

        suffix = uuid.uuid4().hex[:10]
        self.admin_email = f'p009-admin-{suffix}@example.invalid'
        self.user_email = f'p009-user-{suffix}@example.invalid'

        await Config.upsert({'ui.enable_signup': True})

        admin = await self._call(
            'POST',
            '/api/v1/auths/signup',
            {'name': 'p009-admin', 'email': self.admin_email, 'password': 'Admin-pass-123'},
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
            {'name': 'p009-user', 'email': self.user_email, 'password': 'User-pass-123', 'role': 'user'},
            token=self.admin_token,
        )
        signed = await self._call(
            'POST', '/api/v1/auths/signin', {'email': self.user_email, 'password': 'User-pass-123'}
        )
        self.user_token = signed['token']
        self.user_id = signed['id']

    async def _set_config(self, updates):
        """Write config keys and restore the originals when the test ends.

        These tests share one database with the rest of the runtime suite. A
        test that leaves `image_generation.*` behind changes what a later,
        unrelated catalog test sees — the same class of cross-test pollution
        that made a genuine product bug look like a flaky CI failure before.
        """
        if not hasattr(self, '_config_originals'):
            self._config_originals = {}
            self.addAsyncCleanup(self._restore_config)
        for key in updates:
            if key not in self._config_originals:
                self._config_originals[key] = await Config.get(key)
        await Config.upsert(updates)

    async def _restore_config(self):
        if not getattr(self, '_config_originals', None):
            return
        await Config.upsert(self._config_originals)

    async def _call(self, method, path, body=None, token=None):
        credential = token or getattr(self, 'admin_token', None)
        headers = {'Authorization': f'Bearer {credential}'} if credential else {}
        response = await self.client.request(method, path, json=body, headers=headers)
        try:
            payload = response.json()
        except Exception:
            payload = response.text
        return payload

    async def _create_connection(self, *, provider_id='minimax', visibility='shared', name=None):
        body = {
            'provider_id': provider_id,
            'name': name or f'p009-{uuid.uuid4().hex[:6]}',
            'auth_type': 'api_key',
            'visibility': visibility,
            'api_key': 'test-key-' + uuid.uuid4().hex[:8],
            'config': {'base_url': 'https://api.minimax.cn/v1'},
        }
        payload = await self._call('POST', '/api/v1/providers/connections', body)
        self.assertIn('id', payload, payload)
        # A leftover shared connection shows up in every later catalog listing
        # and changes what unrelated tests count. Delete it with the test.
        self._created_connections = getattr(self, '_created_connections', [])
        self._created_connections.append(payload['id'])
        if len(self._created_connections) == 1:
            self.addAsyncCleanup(self._delete_created_connections)
        return payload

    async def _delete_created_connections(self):
        for connection_id in getattr(self, '_created_connections', []):
            try:
                await self._call('DELETE', f'/api/v1/providers/connections/{connection_id}')
            except Exception:
                pass


class NormalisationTests(unittest.TestCase):
    """A full endpoint and a base must resolve to the same outbound URL."""

    def test_full_endpoint_is_reduced_to_base(self):
        base, stripped = normalize_base_url('https://api.minimax.cn/v1/images/generations', IMAGE)
        self.assertEqual(base, 'https://api.minimax.cn/v1')
        self.assertTrue(stripped)

    def test_base_is_left_alone(self):
        base, stripped = normalize_base_url('https://api.minimax.cn/v1', IMAGE)
        self.assertEqual(base, 'https://api.minimax.cn/v1')
        self.assertFalse(stripped)

    def test_each_adapter_path_is_recognised(self):
        cases = {
            SPEECH: ('https://host/v1/audio/speech', 'https://host/v1'),
            TRANSCRIPTION: ('https://host/v1/audio/transcriptions', 'https://host/v1'),
            IMAGE: ('https://host/v1/image_generation', 'https://host/v1'),
        }
        for purpose, (url, expected) in cases.items():
            base, _ = normalize_base_url(url, purpose)
            self.assertEqual(base, expected, purpose)

    def test_trailing_slash_is_not_doubled(self):
        base, _ = normalize_base_url('https://api.minimax.cn/v1/images/generations/', IMAGE)
        self.assertEqual(base, 'https://api.minimax.cn/v1')

    def test_empty_value_stays_empty(self):
        self.assertEqual(normalize_base_url('', IMAGE), ('', False))

    def test_legacy_state_reports_normalisation(self):
        values = {
            'engine': 'openai',
            'url': 'https://api.minimax.cn/v1/images/generations',
            'has_api_key': True,
            'model': 'MiniMax-H3-Image',
        }
        state = legacy_state(IMAGE, values)
        self.assertTrue(state['normalized'])
        self.assertEqual(state['normalized_base_url'], 'https://api.minimax.cn/v1')
        self.assertTrue(state['configured'])

    def test_legacy_state_without_key_is_not_configured(self):
        values = {'engine': 'openai', 'url': 'https://x/v1', 'has_api_key': False, 'model': 'm'}
        self.assertFalse(legacy_state(IMAGE, values)['configured'])


class SecretRedactionTests(unittest.IsolatedAsyncioTestCase):
    """Keys are write-only: report presence, never the value."""

    def test_secret_fields_are_blanked(self):
        redacted, present = __import__(
            'open_webui.utils.media_assignment', fromlist=['redact_nested']
        ).redact_nested(
            {'tts': {'OPENAI_API_KEY': 'sk-real', 'ENGINE': 'openai'}, 'stt': {'OPENAI_API_KEY': ''}}
        )
        self.assertEqual(redacted['tts']['OPENAI_API_KEY'], '')
        self.assertEqual(redacted['tts']['ENGINE'], 'openai')
        self.assertTrue(present['tts.OPENAI_API_KEY'])
        self.assertFalse(present['stt.OPENAI_API_KEY'])
        self.assertNotIn('sk-real', str(redacted))

    async def test_blank_key_keeps_stored_value(self):
        # The browser never received the secret, so a blank box must not erase it.
        updates = {'audio.tts.openai.api_key': '', 'audio.tts.engine': 'provider'}
        result = await restore_secrets(updates, {})
        self.assertNotIn('audio.tts.openai.api_key', result)
        self.assertEqual(result['audio.tts.engine'], 'provider')

    async def test_clear_sentinel_deletes(self):
        result = await restore_secrets({'audio.tts.openai.api_key': '__clear__'}, {})
        self.assertEqual(result['audio.tts.openai.api_key'], '')

    async def test_explicit_key_is_written(self):
        result = await restore_secrets({'audio.tts.openai.api_key': 'sk-new'}, {})
        self.assertEqual(result['audio.tts.openai.api_key'], 'sk-new')

    def test_non_secret_fields_pass_through(self):
        redacted, _ = __import__(
            'open_webui.utils.media_assignment', fromlist=['redact_secrets']
        ).redact_secrets({'ENGINE': 'openai', 'MODEL': 'm'})
        self.assertEqual(redacted, {'ENGINE': 'openai', 'MODEL': 'm'})


class MediaAssignmentEndpointTests(AssignmentHarness):
    """The HTTP contract the settings screen consumes."""

    async def test_assignment_view_lists_four_purposes_in_order(self):
        payload = await self._call('GET', '/api/v1/providers/media-assignment')
        purposes = [item['purpose'] for item in payload['purposes']]
        self.assertEqual(purposes, [TRANSCRIPTION, SPEECH, IMAGE, 'realtime'])
        for item in payload['purposes']:
            self.assertIn('mode', item)
            self.assertIn('legacy', item)
            self.assertIsInstance(item['legacy']['has_api_key'], bool)
            self.assertNotIn('sk-', str(item['legacy']))

    async def test_private_connection_cannot_back_a_media_purpose(self):
        connection = await self._create_connection(visibility='private')
        payload = await self._call(
            'PUT',
            f'/api/v1/providers/media-assignment/{IMAGE}',
            {'connection_id': connection['id'], 'model_id': 'provider:x:y'},
        )
        self.assertIn(ASSIGNMENT_ERROR_PRIVATE, str(payload))

    async def test_model_outside_whitelist_is_refused(self):
        connection = await self._create_connection(visibility='shared')
        payload = await self._call(
            'PUT',
            f'/api/v1/providers/media-assignment/{IMAGE}',
            {'connection_id': connection['id'], 'model_id': 'provider:x:not-there'},
        )
        self.assertIn(ASSIGNMENT_ERROR_MODEL, str(payload))

    async def test_unassignable_purpose_is_reported_not_hidden(self):
        payload = await self._call('GET', '/api/v1/providers/media-assignment')
        realtime = next(item for item in payload['purposes'] if item['purpose'] == 'realtime')
        self.assertFalse(realtime['ready'])
        self.assertTrue(realtime['reasons'])
        self.assertEqual(realtime['mode'], 'unconfigured')

    async def test_migration_is_retired_with_a_reason(self):
        payload = await self._call('GET', '/api/v1/providers/media-migration')
        self.assertIn(MIGRATION_RETIRED, str(payload))

    async def test_non_admin_cannot_read_assignment(self):
        payload = await self._call(
            'GET', '/api/v1/providers/media-assignment', token=self.user_token
        )
        self.assertNotIn('purposes', payload)


class ResolutionTests(AssignmentHarness):
    """`resolve_media_target` is the single place a media call asks "where?"."""

    async def test_unconfigured_purpose_refuses_with_a_code(self):
        from open_webui.models.users import Users

        await self._set_config(
            {
                'image_generation.engine': '',
                'image_generation.openai.api_base_url': '',
                'image_generation.openai.api_key': '',
                'image_generation.provider_connection_id': '',
                'image_generation.model': '',
            }
        )
        admin = await Users.get_user_by_id(self.admin_id)
        with self.assertRaises(Exception) as raised:
            await resolve_media_target(IMAGE, admin)
        self.assertIn(ASSIGNMENT_ERROR_NOT_CONFIGURED, str(raised.exception))

    async def test_legacy_endpoint_is_normalised_for_outbound(self):
        # This is the shape production was actually in.
        await self._set_config(
            {
                'image_generation.engine': 'openai',
                'image_generation.openai.api_base_url': 'https://api.minimax.cn/v1/images/generations',
                'image_generation.openai.api_key': 'sk-legacy',
                'image_generation.provider_connection_id': '',
            }
        )
        from open_webui.models.users import Users

        admin = await Users.get_user_by_id(self.admin_id)
        target = await resolve_media_target(IMAGE, admin)
        self.assertEqual(target.mode, 'legacy')
        self.assertEqual(target.base_url, 'https://api.minimax.cn/v1')

    async def test_legacy_assignment_is_readable_for_revert(self):
        await self._set_config(
            {
                'audio.tts.engine': 'openai',
                'audio.tts.openai.api_base_url': 'https://api.example/v1',
                'audio.tts.model': 'tts-1',
            }
        )
        values = await read_assignment(SPEECH)
        self.assertEqual(values['engine'], 'openai')
        self.assertFalse(values['has_api_key'])


if __name__ == '__main__':
    unittest.main()
