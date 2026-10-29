"""Provider model capability precedence and OpenAI image transport contracts."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from open_webui.models.models import Models
from open_webui.models.provider_connections import new_provider_connection
from open_webui.utils import providers
from open_webui.utils.provider_capabilities import (
    resolve_model_capabilities, update_model_capability_declarations, validate_model_capabilities,
)


class ProviderModelCapabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = SimpleNamespace(id='owner', role='user')
        self.connection = new_provider_connection(
            provider_id='openai', name='Private', auth_type='api_key', visibility='private',
            owner_id='owner', config={}, credential='encrypted', status='configured', models=[], is_active=True,
        )
        self.model_id = providers.stable_model_id(self.connection.id, 'vision-model')

    async def test_list_and_resolve_share_three_state_capabilities(self):
        cases = (
            ('uv_missing_explicit_true', [], {'vision': True}, True),
            ('uv_missing_explicit_false', [], {'vision': False}, False),
            ('uv_present_no_explicit', ['VU'], {}, True),
            ('explicit_false_overrides_vendor', ['Visual Understanding'], {'vision': False}, False),
            ('unknown_is_not_false', [], {}, None),
        )
        for name, tags, explicit, expected in cases:
            with self.subTest(name=name):
                row = {
                    'id': self.model_id, 'upstream_id': 'vision-model',
                    'uses': {'chat': 'openai-chat'}, 'capabilities': [],
                    'model_info': {'description': 'preserved', 'capabilities': explicit},
                }
                vendor = {'upstream_id': 'vision-model', 'capabilities': tags}
                snapshot = {'fingerprint': providers.catalog_fingerprint(self.connection), 'models': [vendor]}
                db = SimpleNamespace(
                    get=AsyncMock(return_value=self.connection),
                    scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [self.connection])),
                )
                with (
                    patch.object(providers, 'visible_connection_models', new=AsyncMock(return_value=[row])),
                    patch.object(providers, 'disabled_provider_model_ids', new=AsyncMock(return_value=set())),
                    patch.object(providers, 'catalog_snapshot', new=AsyncMock(return_value=snapshot)),
                ):
                    listed = (await providers.list_visible_models(self.user, db))[0]
                    resolved = (await providers.resolve_model(self.model_id, self.user, db)).model_metadata
                for model in (listed, resolved):
                    flags = model['info']['meta']['capabilities']
                    self.assertEqual(flags.get('vision'), expected)
                    self.assertEqual('vision' in flags, expected is not None)
                    self.assertTrue(flags['web_search'])
                    self.assertNotIn('file_upload', flags)
                    self.assertNotIn('file_context', flags)
                    self.assertEqual(model['model_info']['description'], 'preserved')
                self.assertEqual(listed['info'], resolved['info'])
                self.assertEqual(row['capabilities'], [])

    async def test_changed_endpoint_ignores_old_catalog_vision_but_keeps_legacy_row_labels(self):
        row = {
            'id': self.model_id, 'upstream_id': 'vision-model', 'uses': {'chat': 'openai-chat'},
            'capabilities': [], 'model_info': {'capabilities': {}},
        }
        old_fingerprint = providers.catalog_fingerprint(self.connection)
        self.connection.config = {'base_url': 'https://new-provider.invalid/v1'}
        self.assertNotEqual(old_fingerprint, providers.catalog_fingerprint(self.connection))
        stale = {
            'fingerprint': old_fingerprint,
            'models': [{'upstream_id': 'vision-model', 'capabilities': ['VU']}],
        }
        db = SimpleNamespace(
            get=AsyncMock(return_value=self.connection),
            scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [self.connection])),
        )
        with (
            patch.object(providers, 'visible_connection_models', new=AsyncMock(return_value=[row])),
            patch.object(providers, 'disabled_provider_model_ids', new=AsyncMock(return_value=set())),
            patch.object(providers, 'catalog_snapshot', new=AsyncMock(return_value=stale)),
        ):
            listed = (await providers.list_visible_models(self.user, db))[0]
            resolved = (await providers.resolve_model(self.model_id, self.user, db)).model_metadata
            for model in (listed, resolved):
                self.assertNotIn('vision', model['info']['meta']['capabilities'])
            # A saved legacy row tag belongs to the row, not the stale catalog.
            row['capabilities'] = ['Visual Understanding']
            listed = (await providers.list_visible_models(self.user, db))[0]
            resolved = (await providers.resolve_model(self.model_id, self.user, db)).model_metadata
            for model in (listed, resolved):
                self.assertTrue(model['info']['meta']['capabilities']['vision'])

    async def test_legacy_alias_uses_provider_vision_and_preserves_workspace_metadata_and_access(self):
        alias = 'old-vision-model'
        self.connection.config = {'_legacy_aliases': {alias: 'vision-model'}}
        cases = (
            ('explicit_true', {'vision': True}, [], True),
            ('explicit_false_over_vendor', {'vision': False}, ['VU'], False),
            ('vendor_true', {}, ['VU'], True),
            ('unknown_removes_workspace_vision', {}, [], None),
        )
        for name, explicit, tags, expected in cases:
            with self.subTest(name=name):
                row = {
                    'id': self.model_id, 'upstream_id': 'vision-model',
                    'uses': {'chat': 'openai-chat'}, 'capabilities': [],
                    'model_info': {'capabilities': explicit},
                }
                saved = SimpleNamespace(
                    is_active=True,
                    model_dump=lambda: {
                        'id': alias, 'user_id': 'original-owner',
                        'meta': {'description': 'Workspace description', 'capabilities': {
                            'vision': False, 'file_upload': True, 'web_search': False,
                        }},
                        'access_grants': [{'principal_id': 'member'}],
                        'params': {'temperature': 0.5},
                    },
                )
                db = SimpleNamespace(
                    scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [self.connection])),
                )
                with (
                    patch.object(providers, 'visible_connection_models', new=AsyncMock(return_value=[row])),
                    patch.object(providers, 'disabled_provider_model_ids', new=AsyncMock(return_value=set())),
                    patch.object(providers, 'catalog_snapshot', new=AsyncMock(return_value={
                        'fingerprint': providers.catalog_fingerprint(self.connection),
                        'models': [{'upstream_id': 'vision-model', 'capabilities': tags}],
                    })),
                    patch.object(providers, 'legacy_model_access', new=AsyncMock(return_value=True)) as access,
                    patch.object(Models, 'get_model_by_id', new=AsyncMock(return_value=saved)),
                ):
                    listed = await providers.list_visible_models(self.user, db)
                access.assert_awaited_once_with(self.connection, 'vision-model', self.user, db)
                self.assertEqual(len(listed), 1)
                model = listed[0]
                self.assertEqual(model['id'], alias)
                self.assertEqual(model['provider_model_id'], self.model_id)
                self.assertEqual(model['info']['meta']['capabilities'].get('vision'), expected)
                self.assertEqual('vision' in model['info']['meta']['capabilities'], expected is not None)
                self.assertTrue(model['info']['meta']['capabilities']['file_upload'])
                self.assertFalse(model['info']['meta']['capabilities']['web_search'])
                self.assertEqual(model['info']['meta']['description'], 'Workspace description')
                self.assertEqual(model['info']['access_grants'], [{'principal_id': 'member'}])
                self.assertEqual(model['info']['params'], {'temperature': 0.5})

    async def test_legacy_alias_is_filtered_by_existing_model_access_check(self):
        alias = 'old-vision-model'
        self.connection.config = {'_legacy_aliases': {alias: 'vision-model'}}
        row = {'id': self.model_id, 'upstream_id': 'vision-model', 'uses': {'chat': 'openai-chat'}}
        db = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [self.connection])))
        with (
            patch.object(providers, 'visible_connection_models', new=AsyncMock(return_value=[row])),
            patch.object(providers, 'disabled_provider_model_ids', new=AsyncMock(return_value=set())),
            patch.object(providers, 'catalog_snapshot', new=AsyncMock(return_value={})),
            patch.object(providers, 'legacy_model_access', new=AsyncMock(return_value=False)) as access,
            patch.object(Models, 'get_model_by_id', new=AsyncMock()) as saved,
        ):
            self.assertEqual(await providers.list_visible_models(self.user, db), [])
        access.assert_awaited_once_with(self.connection, 'vision-model', self.user, db)
        saved.assert_not_awaited()

    async def test_explicit_fields_override_default_without_changing_vendor_labels(self):
        row = {'capabilities': ['VU'], 'model_info': {'capabilities': {
            'vision': False, 'file_upload': True, 'file_context': False, 'web_search': False,
        }}}
        self.assertEqual(resolve_model_capabilities(self.connection, row, None), {
            'vision': False, 'file_upload': True, 'file_context': False, 'web_search': False,
        })
        self.assertEqual(row['capabilities'], ['VU'])

    async def test_validation_requires_known_boolean_keys(self):
        self.assertEqual(validate_model_capabilities({'vision': False}), {'vision': False})
        self.assertEqual(validate_model_capabilities({'vision': None}), {'vision': None})
        for value in ([], {'vision': 1}, {'vision': 'true'}, {'unknown': True}):
            with self.subTest(value=value), self.assertRaises(HTTPException) as caught:
                validate_model_capabilities(value)
            self.assertEqual(caught.exception.status_code, 400)

    def _write_db(self, connection, row):
        return SimpleNamespace(
            get=AsyncMock(return_value=connection),
            scalars=AsyncMock(return_value=SimpleNamespace(first=lambda: row)),
            commit=AsyncMock(),
        )

    def _provider_row(self):
        return SimpleNamespace(
            display_name='Vision',
            uses={'chat': 'openai-chat'},
            enabled=True,
            updated_at=0,
            model_info={'description': 'kept', 'capabilities': {'vision': True, 'web_search': False}},
        )

    async def test_capability_write_permission_follows_connection_ownership(self):
        from open_webui.routers import providers as api

        async def write(connection, user):
            row = self._provider_row()
            db = self._write_db(connection, row)
            await api.update_connection_model(
                connection.id, 'vision-model', api.ModelUpdate(capabilities={'vision': False}), user, db
            )
            db.commit.assert_awaited_once()
            return row

        shared = new_provider_connection(
            provider_id='openai', name='Shared', auth_type='api_key', visibility='shared',
            owner_id='someone-else', config={}, credential='encrypted', status='configured', models=[], is_active=True,
        )
        # Ruling A: administrators may write a shared connection's declarations.
        row = await write(shared, SimpleNamespace(id='root', role='admin'))
        self.assertEqual(row.model_info['capabilities'], {'vision': False, 'web_search': False})
        self.assertEqual(row.model_info['description'], 'kept')
        # A non-admin can never manage a shared connection, even their own row.
        with self.assertRaises(HTTPException) as caught:
            await write(shared, SimpleNamespace(id='someone-else', role='user'))
        self.assertEqual(caught.exception.status_code, 403)
        # The owner of a private connection may write their own declarations.
        row = await write(self.connection, self.user)
        self.assertEqual(row.model_info['capabilities'], {'vision': False, 'web_search': False})
        # Any other user is denied on a private connection.
        with self.assertRaises(HTTPException) as caught:
            await write(self.connection, SimpleNamespace(id='intruder', role='user'))
        self.assertEqual(caught.exception.status_code, 404)

    async def test_update_connection_model_three_state_and_omitted_capabilities(self):
        from open_webui.routers import providers as api

        async def run(form):
            row = self._provider_row()
            db = self._write_db(self.connection, row)
            await api.update_connection_model(self.connection.id, 'vision-model', form, self.user, db)
            return row

        # Omitted capabilities never touch model_info.
        row = await run(api.ModelUpdate(display_name='Renamed'))
        self.assertEqual(row.model_info['capabilities'], {'vision': True, 'web_search': False})
        self.assertEqual(row.display_name, 'Renamed')
        # A single-key null deletes one declaration and keeps the rest.
        row = await run(api.ModelUpdate(capabilities={'vision': None, 'file_upload': True}))
        self.assertEqual(row.model_info['capabilities'], {'web_search': False, 'file_upload': True})
        # A top-level null clears every explicit declaration, keeping other keys.
        row = await run(api.ModelUpdate(capabilities=None))
        self.assertEqual(row.model_info, {'description': 'kept'})

    async def test_update_connection_model_rejects_invalid_capabilities_without_commit(self):
        from open_webui.routers import providers as api

        row = self._provider_row()
        db = self._write_db(self.connection, row)
        with self.assertRaises(HTTPException) as caught:
            await api.update_connection_model(
                self.connection.id, 'vision-model', api.ModelUpdate(capabilities={'vision': 'yes'}), self.user, db
            )
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(row.model_info['capabilities'], {'vision': True, 'web_search': False})
        db.commit.assert_not_awaited()

    async def test_null_deletes_one_declaration_without_losing_other_metadata(self):
        info = {'description': 'preserved', 'capabilities': {'vision': False, 'web_search': False}}
        updated = update_model_capability_declarations(info, {'vision': None, 'file_upload': True})
        self.assertEqual(updated, {
            'description': 'preserved', 'capabilities': {'web_search': False, 'file_upload': True},
        })
        self.assertEqual(info['capabilities']['vision'], False)
        self.assertEqual(update_model_capability_declarations(updated, {}), updated)
        self.assertEqual(update_model_capability_declarations(updated, None), {'description': 'preserved'})

    async def test_image_url_is_forwarded_unchanged_to_openai_chat(self):
        image = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AQID', 'detail': 'high'}}
        messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'Describe'}, image]}]
        response = SimpleNamespace(status=200, headers={}, release=Mock())
        session = SimpleNamespace(close=AsyncMock())
        resolved = SimpleNamespace(connection=self.connection, upstream_model_id='vision-model', model_metadata={})
        with (
            patch.object(providers, 'resolve_model', new=AsyncMock(return_value=resolved)),
            patch.object(providers, '_authorization', new=AsyncMock(return_value='token')),
            patch.object(providers, 'provider_base_url', return_value='https://example.invalid/v1'),
            patch.object(providers, 'provider_request_headers', return_value={}),
            patch.object(providers, '_post_provider_request', new=AsyncMock(return_value=(response, session))) as post,
            patch.object(providers, 'read_bounded_response', new=AsyncMock(return_value=b'{"choices": []}')),
        ):
            result = await providers.provider_chat_completion(self.model_id, {'messages': messages}, self.user)
        self.assertEqual(result['model'], self.model_id)
        self.assertEqual(post.await_args.args[2]['messages'], messages)
        self.assertIs(post.await_args.args[2]['messages'][0]['content'][1], image)
        self.assertEqual(post.await_args.args[1], 'https://example.invalid/v1/chat/completions')
        response.release.assert_called_once()
        session.close.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
