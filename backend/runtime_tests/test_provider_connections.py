"""Provider access, model fallback and credential isolation checks."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from open_webui.models.provider_connections import ProviderConnectionUpdate, new_provider_connection
from open_webui.utils import providers


class ProviderConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner = SimpleNamespace(id='owner', role='user')
        self.other = SimpleNamespace(id='other', role='user')
        self.admin = SimpleNamespace(id='admin', role='admin')
        self.connection = new_provider_connection(
            provider_id='openai',
            name='Private',
            auth_type='api_key',
            visibility='private',
            owner_id='owner',
            config={},
            credential=providers.encrypt_credential('secret-value'),
            status='configured',
            models=[],
            is_active=True,
        )
        self.db = SimpleNamespace(commit=AsyncMock())

    async def test_private_connection_is_owner_only_even_for_admin(self):
        self.assertTrue(await providers.can_access_connection(self.connection, self.owner, self.db))
        self.assertFalse(await providers.can_access_connection(self.connection, self.other, self.db))
        self.assertFalse(await providers.can_access_connection(self.connection, self.admin, self.db))

    async def test_platform_web_search_does_not_require_provider_native_search(self):
        for provider_id, features in (('chatgpt', []), ('openai', []), ('custom', []), ('bailian', ['web-search'])):
            with self.subTest(provider=provider_id):
                self.connection.provider_id = provider_id
                model_id = providers.stable_model_id(self.connection.id, 'text-model')
                models = [
                    {
                        'id': model_id,
                        'upstream_id': 'text-model',
                        'capabilities': [],
                        'features': features,
                        'uses': {'chat': 'openai-chat'},
                    }
                ]
                db = SimpleNamespace(
                    get=AsyncMock(return_value=self.connection),
                    scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [self.connection])),
                )
                with (
                    patch.object(providers, 'visible_connection_models', new=AsyncMock(return_value=models)),
                    patch.object(providers, 'disabled_provider_model_ids', new=AsyncMock(return_value=set())),
                ):
                    listing = await providers.list_visible_models(self.owner, db)
                    resolved = await providers.resolve_model(model_id, self.owner, db)
                for model in (listing[0], resolved.model_metadata):
                    self.assertTrue(model['info']['meta']['capabilities']['web_search'])
                    self.assertFalse(model['info']['meta']['capabilities']['vision'])
                    self.assertFalse(model['info']['meta']['capabilities']['file_upload'])
                    self.assertEqual(model['features'], features)

    async def test_disabled_provider_model_is_hidden_and_rejected_without_discovery(self):
        model_id = providers.stable_model_id(self.connection.id, 'text-model')
        models = [{'id': model_id, 'upstream_id': 'text-model', 'uses': {'chat': 'openai-chat'}}]
        db = SimpleNamespace(
            get=AsyncMock(return_value=self.connection),
            scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [self.connection])),
        )
        for user in (self.owner, self.admin):
            with (
                self.subTest(role=user.role),
                patch.object(providers, 'can_access_connection', new=AsyncMock(return_value=True)),
                patch.object(providers, 'require_connection_access', new=AsyncMock()),
                patch.object(providers, 'visible_connection_models', new=AsyncMock(return_value=models)) as discovery,
                patch.object(
                    providers, 'disabled_provider_model_ids', new=AsyncMock(return_value={model_id})
                ) as switches,
            ):
                self.assertEqual(await providers.list_visible_models(user, db), [])
                discovery.reset_mock()
                with self.assertRaises(HTTPException) as raised:
                    await providers.resolve_model(model_id, user, db)
                self.assertEqual(raised.exception.status_code, 404)
                discovery.assert_not_awaited()
                # Re-enabling takes effect immediately, even with cached discovery.
                switches.return_value = set()
                self.assertEqual((await providers.list_visible_models(user, db))[0]['id'], model_id)
                self.assertEqual((await providers.resolve_model(model_id, user, db)).upstream_model_id, 'text-model')

    async def test_shared_connection_uses_existing_user_and_group_grants(self):
        self.connection.visibility = 'shared'
        with (
            patch.object(providers.AccessGrants, 'has_anyone_access', new=AsyncMock(return_value=False)),
            patch.object(providers.AccessGrants, 'has_access', new=AsyncMock(return_value=True)) as has_access,
        ):
            self.assertTrue(await providers.can_access_connection(self.connection, self.other, self.db))
            has_access.assert_awaited_once_with('other', 'provider_connection', self.connection.id, 'read', db=self.db)

    async def test_platform_search_still_requires_config_feature_permission_and_model_capability(self):
        from open_webui.utils import tools

        request = SimpleNamespace(state=SimpleNamespace())
        extra = {'__user__': {'id': self.owner.id, 'role': 'user'}}
        for enabled, selected, allowed, capable in (
            (True, True, True, True),
            (False, True, True, True),
            (True, False, True, True),
            (True, True, False, True),
            (True, True, True, False),
        ):
            with self.subTest(enabled=enabled, selected=selected, allowed=allowed, capable=capable):
                with (
                    patch.object(tools.Config, 'get_many', new=AsyncMock(return_value={'web.search.enable': enabled})),
                    patch.object(tools.Config, 'get', new=AsyncMock(return_value={})),
                    patch.object(tools, 'has_permission', new=AsyncMock(return_value=allowed)),
                ):
                    available = await tools.get_builtin_tools(
                        request,
                        extra,
                        {'web_search': selected},
                        {'info': {'meta': {'capabilities': {'web_search': capable}}}},
                    )
                for name in ('search_web', 'fetch_url'):
                    self.assertEqual(name in available, enabled and selected and allowed and capable)

    async def test_manual_model_fallback_keeps_connection_scoped_stable_ids(self):
        self.connection.config = {'model_ids': ['manual/vision']}
        with patch.object(providers, '_request_json', new=AsyncMock(side_effect=HTTPException(502, 'offline'))):
            models = await providers.discover_connection_models(self.connection, self.owner)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]['upstream_id'], 'manual/vision')
        self.assertEqual(models[0]['id'], f'provider:{self.connection.id}:manual%2Fvision')

    async def test_manual_model_ids_do_not_turn_failed_authentication_into_verification(self):
        self.connection.config = {'model_ids': ['manual/text']}
        with patch.object(providers, '_request_json', new=AsyncMock(side_effect=HTTPException(401, 'rejected'))):
            verified = await providers.verify_provider_connection(self.connection, self.owner)
        self.assertFalse(verified)

    async def test_bailian_text_discovery_paginates_native_catalog_and_keeps_metadata(self):
        self.connection.provider_id = 'bailian'
        self.connection.config = {'region': 'cn-beijing'}

        async def page(_connection, _user, _url, *, method='GET', body=None, params=None):
            if params['page_no'] == 1:
                return {
                    'output': {
                        'total': 2,
                        'models': [
                            {
                                'model': 'qwen-text',
                                'name': 'Qwen Text',
                                'capabilities': ['TG'],
                                'features': ['web-search'],
                            }
                        ],
                    }
                }
            return {
                'output': {
                    'total': 2,
                    'models': [
                        {'model': 'qwen-vision', 'name': 'Qwen Vision', 'capabilities': ['TG', 'VU'], 'features': []}
                    ],
                }
            }

        with patch.object(providers, '_request_json', new=AsyncMock(side_effect=page)) as request:
            models = await providers.discover_connection_models(self.connection, self.owner)
        self.assertEqual([model['upstream_id'] for model in models], ['qwen-text', 'qwen-vision'])
        self.assertEqual([call.kwargs['params']['page_no'] for call in request.await_args_list], [1, 2])
        self.assertIn('VU', models[1]['capabilities'])

    async def test_bailian_region_and_workspace_drive_inference_and_discovery(self):
        self.connection.provider_id = 'bailian'
        cases = [
            ({'region': 'cn-beijing'}, 'https://dashscope.aliyuncs.com/compatible-mode/v1'),
            ({'region': 'ap-southeast-1'}, 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1'),
            ({'region': 'cn-hongkong'}, 'https://cn-hongkong.dashscope.aliyuncs.com/compatible-mode/v1'),
            (
                {'region': 'cn-beijing', 'workspace_id': 'team-123'},
                'https://team-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
            ),
            (
                {'region': 'ap-southeast-1', 'workspace_id': 'team-123'},
                'https://team-123.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1',
            ),
        ]
        for config, expected in cases:
            with self.subTest(config=config):
                self.connection.config = config
                self.assertEqual(providers.provider_base_url(self.connection), expected)
                parsed = providers.urlsplit(providers._bailian_models_url(self.connection))
                self.assertEqual(parsed.netloc, providers.urlsplit(expected).netloc)
                self.assertEqual(parsed.path, '/api/v1/models')

    async def test_bailian_explicit_base_url_controls_both_protocols(self):
        self.connection.provider_id = 'bailian'
        self.connection.config = {
            'region': 'ap-southeast-1',
            'workspace_id': 'workspace-1',
            'base_url': 'https://dashscope.example.test/compatible-mode/v1',
        }
        self.assertEqual(
            providers.provider_base_url(self.connection), 'https://dashscope.example.test/compatible-mode/v1'
        )
        self.assertEqual(providers._bailian_models_url(self.connection), 'https://dashscope.example.test/api/v1/models')

    async def test_bailian_rejects_unknown_regions_and_workspace_host_lookalikes(self):
        self.connection.provider_id = 'bailian'
        for config in (
            {'region': 'moon-1'},
            {'region': 'cn-beijing', 'workspace_id': 'bad.example.com'},
        ):
            with self.subTest(config=config), self.assertRaises(HTTPException):
                self.connection.config = config
                providers.provider_base_url(self.connection)

    async def test_provider_patch_rejects_null_name_and_config(self):
        from open_webui.routers.providers import update_connection

        db = SimpleNamespace(get=AsyncMock(return_value=self.connection), commit=AsyncMock())
        for form in (ProviderConnectionUpdate(name=None), ProviderConnectionUpdate(config=None)):
            with self.subTest(form=form), self.assertRaises(HTTPException) as caught:
                await update_connection(self.connection.id, form, self.owner, db)
            self.assertEqual(caught.exception.status_code, 400)
        db.commit.assert_not_awaited()

    async def test_chatgpt_oauth_connection_allows_name_only_edit(self):
        from open_webui.routers.providers import update_connection

        self.connection.provider_id = 'chatgpt'
        self.connection.auth_type = 'oauth'
        db = SimpleNamespace(get=AsyncMock(return_value=self.connection), commit=AsyncMock())
        updated = await update_connection(
            self.connection.id, ProviderConnectionUpdate(name='My ChatGPT'), self.owner, db
        )
        self.assertEqual(updated['name'], 'My ChatGPT')
        db.commit.assert_awaited_once()

    async def test_credential_ciphertext_is_never_in_public_connection_shape(self):
        safe = providers.public_connection(self.connection)
        self.assertNotIn('credential', safe)
        self.assertNotIn('secret-value', repr(safe))
        self.assertNotEqual(self.connection.credential, 'secret-value')
        self.assertTrue(safe['configured'])

    async def test_decryption_failure_returns_fixed_error_without_secret_material(self):
        self.connection.credential = 'not-a-valid-ciphertext'
        with self.assertRaises(HTTPException) as caught:
            providers.decrypt_credential(self.connection.credential)
        self.assertEqual(caught.exception.status_code, 500)
        self.assertNotIn(self.connection.credential, str(caught.exception.detail))

    async def test_model_id_parser_rejects_non_provider_ids(self):
        with self.assertRaises(HTTPException) as caught:
            providers.parse_model_id('openai/gpt-4o')
        self.assertEqual(caught.exception.status_code, 404)


if __name__ == '__main__':
    unittest.main()
