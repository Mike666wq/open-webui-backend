"""Provider model visibility must remain scoped to the request, including bypass mode."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from open_webui.utils import models
from open_webui.utils import providers


class ProviderRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_overlays_do_not_mutate_the_shared_registry(self):
        shared = {'legacy': {'id': 'legacy'}}
        app = SimpleNamespace(state=SimpleNamespace(MODELS=shared))
        alice = SimpleNamespace(id='alice', role='user')
        bob = SimpleNamespace(id='bob', role='user')
        a = SimpleNamespace(app=app, state=SimpleNamespace())
        b = SimpleNamespace(app=app, state=SimpleNamespace())

        async def catalog(user):
            return [{'id': 'provider:' + user.id + ':model'}]

        with patch.object(providers, 'list_visible_models', side_effect=catalog):
            await models.load_provider_models(a, alice)
            await models.load_provider_models(b, bob)
        self.assertEqual(set(models.get_request_models(a)), {'legacy', 'provider:alice:model'})
        self.assertEqual(set(models.get_request_models(b)), {'legacy', 'provider:bob:model'})
        self.assertEqual(set(shared), {'legacy'})

    async def test_admin_bypass_does_not_skip_private_catalog_filter(self):
        user = SimpleNamespace(id='admin', role='admin')
        private = {'id': 'provider:alice:model'}
        with (
            patch.object(providers, 'list_visible_models', new=AsyncMock(return_value=[])),
            patch.object(models, 'BYPASS_MODEL_ACCESS_CONTROL', True),
        ):
            result = await models.get_filtered_models([private, {'id': 'legacy'}], user)
        self.assertEqual(result, [{'id': 'legacy'}])

    async def test_provider_access_is_rechecked_at_invocation(self):
        user = SimpleNamespace(id='alice', role='admin')
        with patch.object(providers, 'resolve_model', new=AsyncMock(side_effect=PermissionError('revoked'))) as resolve:
            with self.assertRaises(PermissionError):
                await models.check_model_access(user, {'id': 'provider:connection:model'})
            resolve.assert_awaited_once_with('provider:connection:model', user, None)
