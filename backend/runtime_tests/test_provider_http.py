"""Provider transport invariants: address policy and the five error categories.

The proxy fake-IP range (RFC 2544, 198.18.0.0/15) used to be rejected by the
shared SSRF guard, which made every provider on a proxied host fail with an
opaque HTTP 502. These tests pin the address policy and the error taxonomy that
replaced it.
"""

import ipaddress
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp.client_reqrep import ConnectionKey
from fastapi import HTTPException
from open_webui.utils import chatgpt_auth, provider_errors, provider_http, providers


def connection_key(host='api.example.com', port=443):
    return ConnectionKey(
        host,
        port,
        is_ssl=True,
        ssl=None,
        proxy=None,
        proxy_auth=None,
        proxy_headers_hash=None,
        server_hostname=None,
    )


class FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status, body=b'{}'):
        self.status = status
        self.content = FakeContent([body])


class FakeRequestContext:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return self.response

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def request(self, *args, **kwargs):
        return FakeRequestContext(self.response, self.error)


class FakeTokenSession(FakeSession):
    """A session for the OAuth token endpoint, which is posted to."""

    def post(self, *args, **kwargs):
        return FakeRequestContext(self.response, self.error)


class ProviderHttpTests(unittest.IsolatedAsyncioTestCase):
    # --- address policy -------------------------------------------------

    def test_reserved_and_private_addresses_are_not_public(self):
        for address in ('127.0.0.1', '10.0.0.1', '172.16.0.1', '169.254.169.254', '198.18.0.27', '::1'):
            self.assertFalse(provider_http.is_public_ip_address(address), address)

    def test_public_addresses_stay_public(self):
        for address in ('93.184.216.34', '8.8.8.8', '2606:2800:220:1:248:1893:25c8:1946'):
            self.assertTrue(provider_http.is_public_ip_address(address), address)

    def test_fake_ip_range_is_allowed_only_when_explicitly_listed(self):
        networks = [ipaddress.ip_network('198.18.0.0/15')]
        self.assertTrue(provider_http.is_explicitly_allowed('198.18.0.27', networks))
        self.assertFalse(provider_http.is_explicitly_allowed('198.18.0.27', []))
        # The allow list must not silently widen to unrelated private ranges.
        self.assertFalse(provider_http.is_explicitly_allowed('127.0.0.1', networks))
        self.assertFalse(provider_http.is_explicitly_allowed('169.254.169.254', networks))

    def test_allowed_ip_ranges_reads_the_environment(self):
        with patch.dict('os.environ', {provider_http.ALLOWED_RANGES_ENV: ''}):
            self.assertEqual(provider_http.allowed_ip_networks(), [])
        with patch.dict('os.environ', {provider_http.ALLOWED_RANGES_ENV: '198.18.0.0/15, 10.0.0.0/8'}):
            networks = provider_http.allowed_ip_networks()
        self.assertEqual(
            [str(network) for network in networks], ['198.18.0.0/15', '10.0.0.0/8']
        )

    async def test_connector_blocks_non_public_address(self):
        connector = provider_http.ProviderTCPConnector()
        try:
            with patch.object(
                aiohttp.TCPConnector,
                '_resolve_host',
                new=AsyncMock(return_value=[{'host': '198.18.0.27'}]),
            ):
                with self.assertRaises(provider_http.ProviderAddressBlocked) as ctx:
                    await connector._resolve_host('api.openai.com', 443)
            self.assertEqual(ctx.exception.addresses, ['198.18.0.27'])
        finally:
            await connector.close()

    async def test_connector_permits_explicitly_allowed_address(self):
        connector = provider_http.ProviderTCPConnector(
            allowed_networks=[ipaddress.ip_network('198.18.0.0/15')]
        )
        try:
            with patch.object(
                aiohttp.TCPConnector,
                '_resolve_host',
                new=AsyncMock(return_value=[{'host': '198.18.0.27'}]),
            ):
                addresses = await connector._resolve_host('api.openai.com', 443)
            self.assertEqual([entry['host'] for entry in addresses], ['198.18.0.27'])
        finally:
            await connector.close()

    # --- transport error mapping ---------------------------------------

    async def test_blocked_address_becomes_provider_network_with_hint(self):
        session = FakeSession(
            error=provider_http.ProviderAddressBlocked('api.openai.com', ['198.18.0.27'])
        )
        with patch.object(provider_http.aiohttp, 'ClientSession', return_value=session):
            with self.assertRaises(provider_errors.ProviderRequestError) as ctx:
                await provider_http.provider_request_json(
                    'https://api.openai.com/v1/models', headers={}
                )
        self.assertEqual(ctx.exception.category, provider_errors.PROVIDER_NETWORK)
        self.assertIn('198.18.0.27', ctx.exception.hint)
        self.assertIn(provider_http.ALLOWED_RANGES_ENV, ctx.exception.hint)

    async def test_dns_failure_becomes_connection(self):
        session = FakeSession(
            error=aiohttp.ClientConnectorDNSError(connection_key(), OSError('no such host'))
        )
        with patch.object(provider_http.aiohttp, 'ClientSession', return_value=session):
            with self.assertRaises(provider_errors.ProviderRequestError) as ctx:
                await provider_http.provider_request_json('https://api.example.com/v1/models', headers={})
        self.assertEqual(ctx.exception.category, provider_errors.CONNECTION)

    async def test_tls_failure_becomes_connection(self):
        session = FakeSession(error=aiohttp.ClientSSLError(connection_key(), OSError('bad handshake')))
        with patch.object(provider_http.aiohttp, 'ClientSession', return_value=session):
            with self.assertRaises(provider_errors.ProviderRequestError) as ctx:
                await provider_http.provider_request_json('https://api.example.com/v1/models', headers={})
        self.assertEqual(ctx.exception.category, provider_errors.CONNECTION)

    async def test_timeout_becomes_connection(self):
        session = FakeSession(error=TimeoutError())
        with patch.object(provider_http.aiohttp, 'ClientSession', return_value=session):
            with self.assertRaises(provider_errors.ProviderRequestError) as ctx:
                await provider_http.provider_request_json('https://api.example.com/v1/models', headers={})
        self.assertEqual(ctx.exception.category, provider_errors.CONNECTION)

    async def test_upstream_status_keeps_its_category(self):
        cases = {401: provider_errors.AUTHENTICATION, 403: provider_errors.AUTHENTICATION,
                 402: provider_errors.RATE_LIMIT, 429: provider_errors.RATE_LIMIT,
                 500: provider_errors.UPSTREAM, 503: provider_errors.UPSTREAM}
        for status, category in cases.items():
            session = FakeSession(response=FakeResponse(status))
            with patch.object(provider_http.aiohttp, 'ClientSession', return_value=session):
                with self.assertRaises(provider_errors.ProviderRequestError) as ctx:
                    await provider_http.provider_request_json('https://api.example.com/v1/models', headers={})
            self.assertEqual(ctx.exception.category, category, status)
            self.assertEqual(ctx.exception.upstream_status, status, status)

    async def test_invalid_json_becomes_upstream_hint(self):
        session = FakeSession(response=FakeResponse(200, b'not-json'))
        with patch.object(provider_http.aiohttp, 'ClientSession', return_value=session):
            with self.assertRaises(provider_errors.ProviderRequestError) as ctx:
                await provider_http.provider_request_json('https://api.example.com/v1/models', headers={})
        self.assertEqual(ctx.exception.category, provider_errors.PROVIDER_NETWORK)
        self.assertIn('JSON', ctx.exception.hint)

    # --- taxonomy -------------------------------------------------------

    def test_catalog_error_uses_the_five_categories(self):
        cases = [
            (providers.provider_http_error(401), provider_errors.AUTHENTICATION, 401),
            (providers.provider_http_error(403), provider_errors.AUTHENTICATION, 403),
            (providers.provider_http_error(402), provider_errors.RATE_LIMIT, 402),
            (providers.provider_http_error(429), provider_errors.RATE_LIMIT, 429),
            (providers.provider_http_error(500), provider_errors.UPSTREAM, 500),
        ]
        for exc, category, status in cases:
            error = providers.catalog_error(exc)
            self.assertEqual(error['category'], category)
            self.assertEqual(error['status'], status)
            self.assertTrue(error['hint'])

    def test_upstream_fault_stays_distinct_from_local_gateway_error(self):
        upstream = providers.catalog_error(providers.provider_http_error(500))
        self.assertEqual(upstream['category'], provider_errors.UPSTREAM)
        self.assertEqual(upstream['status'], 500)
        self.assertEqual(upstream['http_status'], 502)

        local = providers.catalog_error(HTTPException(502, 'Could not reach provider'))
        self.assertEqual(local['category'], provider_errors.CONNECTION)
        self.assertEqual(local['status'], 502)
        self.assertIsNone(local['upstream_status'])

    def test_chatgpt_errors_keep_the_upstream_status(self):
        error = providers.catalog_error(
            chatgpt_auth._chatgpt_error(502, 500, 'ChatGPT request failed with HTTP 500', provider_errors.UPSTREAM)
        )
        self.assertEqual(error['category'], provider_errors.UPSTREAM)
        self.assertEqual(error['status'], 500)
        self.assertEqual(error['http_status'], 502)

        error = providers.catalog_error(
            chatgpt_auth._chatgpt_error(
                401, 401, 'ChatGPT rejected the connected account', provider_errors.AUTHENTICATION
            )
        )
        self.assertEqual(error['category'], provider_errors.AUTHENTICATION)
        self.assertEqual(error['status'], 401)

    def test_error_payload_never_leaks_exception_text(self):
        error = providers.catalog_error(HTTPException(502, 'secret upstream body synthetic'))
        self.assertNotIn('secret', str(error))
        self.assertIn('hint', error)
        self.assertIn('retryable', error)

    # --- reason, stage and suggested action ------------------------------

    def test_the_five_categories_still_carry_a_reason_and_an_action(self):
        cases = [
            (401, provider_errors.REASON_INVALID_CREDENTIALS, provider_errors.ACTION_REPLACE_KEY),
            (403, provider_errors.REASON_INSUFFICIENT_PERMISSIONS, provider_errors.ACTION_CHECK_PERMISSIONS),
            (402, provider_errors.REASON_QUOTA_EXHAUSTED, provider_errors.ACTION_RETRY_LATER),
            (429, provider_errors.REASON_RATE_LIMITED, provider_errors.ACTION_RETRY_LATER),
            (500, provider_errors.REASON_UPSTREAM_FAILURE, provider_errors.ACTION_RETRY_LATER),
        ]
        for status, reason, action in cases:
            error = providers.catalog_error(providers.provider_http_error(status))
            self.assertEqual(error['reason'], reason, status)
            self.assertEqual(error['suggested_action'], action, status)

    def test_a_forbidden_catalog_does_not_ask_for_a_new_key(self):
        """403 means the account lacks a permission, not that the key is wrong."""
        error = providers.catalog_error(providers.provider_http_error(403))
        self.assertEqual(error['category'], provider_errors.AUTHENTICATION)
        self.assertEqual(error['reason'], provider_errors.REASON_INSUFFICIENT_PERMISSIONS)
        self.assertEqual(error['suggested_action'], provider_errors.ACTION_CHECK_PERMISSIONS)
        self.assertNotEqual(error['suggested_action'], provider_errors.ACTION_REPLACE_KEY)

    def test_the_fake_ip_address_is_reported_as_a_local_block(self):
        """A6: 198.18.0.28 is a local policy decision, not an upstream status."""
        error = providers.catalog_error(
            provider_http.ProviderRequestError(
                provider_errors.PROVIDER_NETWORK,
                502,
                reason=provider_errors.REASON_ADDRESS_BLOCKED,
                suggested_action=provider_errors.ACTION_CHECK_SERVER_NETWORK,
            )
        )
        self.assertEqual(error['category'], provider_errors.PROVIDER_NETWORK)
        self.assertEqual(error['reason'], provider_errors.REASON_ADDRESS_BLOCKED)
        self.assertEqual(error['suggested_action'], provider_errors.ACTION_CHECK_SERVER_NETWORK)
        self.assertIsNone(error['upstream_status'])

    async def test_a_blocked_address_carries_the_network_reason(self):
        session = FakeSession(
            error=provider_http.ProviderAddressBlocked('api.siliconflow.cn', ['198.18.0.28'])
        )
        with patch.object(provider_http.aiohttp, 'ClientSession', return_value=session):
            with self.assertRaises(provider_errors.ProviderRequestError) as ctx:
                await provider_http.provider_request_json(
                    'https://api.siliconflow.cn/v1/models', headers={}
                )
        self.assertEqual(ctx.exception.category, provider_errors.PROVIDER_NETWORK)
        self.assertEqual(ctx.exception.reason, provider_errors.REASON_ADDRESS_BLOCKED)
        self.assertEqual(
            ctx.exception.suggested_action, provider_errors.ACTION_CHECK_SERVER_NETWORK
        )
        self.assertIsNone(ctx.exception.upstream_status)

    async def test_dns_tls_and_timeout_carry_their_own_reason(self):
        cases = [
            (
                aiohttp.ClientConnectorDNSError(connection_key(), OSError('no such host')),
                provider_errors.REASON_DNS_FAILURE,
            ),
            (
                aiohttp.ClientSSLError(connection_key(), OSError('bad handshake')),
                provider_errors.REASON_TLS_FAILURE,
            ),
            (TimeoutError(), provider_errors.REASON_TIMEOUT),
        ]
        for error, reason in cases:
            session = FakeSession(error=error)
            with patch.object(provider_http.aiohttp, 'ClientSession', return_value=session):
                with self.assertRaises(provider_errors.ProviderRequestError) as ctx:
                    await provider_http.provider_request_json(
                        'https://api.example.com/v1/models', headers={}
                    )
            self.assertEqual(ctx.exception.category, provider_errors.CONNECTION)
            self.assertEqual(ctx.exception.reason, reason)
            self.assertEqual(
                ctx.exception.suggested_action, provider_errors.ACTION_CHECK_SERVER_NETWORK
            )

    # --- OAuth failures are not all authentication failures ---------------

    async def test_an_invalid_grant_asks_for_a_new_sign_in(self):
        body = b'{"error": "invalid_grant", "error_description": "refresh token revoked"}'
        with patch.object(
            chatgpt_auth.aiohttp, 'ClientSession', return_value=FakeTokenSession(FakeResponse(400, body))
        ):
            with self.assertRaises(HTTPException) as ctx:
                await chatgpt_auth.oauth_form({'grant_type': 'refresh_token'})
        error = ctx.exception
        self.assertEqual(error.provider_category, provider_errors.AUTHENTICATION)
        self.assertEqual(error.provider_reason, provider_errors.REASON_TOKEN_EXPIRED)
        self.assertEqual(error.provider_suggested_action, provider_errors.ACTION_RECONNECT)
        self.assertEqual(error.provider_stage, provider_errors.STAGE_OAUTH_REFRESH)

    async def test_a_throttled_token_endpoint_is_not_an_authentication_failure(self):
        with patch.object(
            chatgpt_auth.aiohttp,
            'ClientSession',
            return_value=FakeTokenSession(FakeResponse(429, b'{"error": "slow_down"}')),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await chatgpt_auth.oauth_form({'grant_type': 'refresh_token'})
        error = ctx.exception
        self.assertEqual(error.provider_category, provider_errors.RATE_LIMIT)
        self.assertEqual(error.provider_reason, provider_errors.REASON_RATE_LIMITED)
        self.assertEqual(error.provider_suggested_action, provider_errors.ACTION_RETRY_LATER)

    async def test_a_broken_token_service_is_an_upstream_failure(self):
        with patch.object(
            chatgpt_auth.aiohttp,
            'ClientSession',
            return_value=FakeTokenSession(FakeResponse(503, b'{"error": "unavailable"}')),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await chatgpt_auth.oauth_form({'grant_type': 'refresh_token'})
        error = ctx.exception
        self.assertEqual(error.provider_category, provider_errors.UPSTREAM)
        self.assertEqual(error.provider_reason, provider_errors.REASON_UPSTREAM_FAILURE)
        self.assertEqual(error.provider_suggested_action, provider_errors.ACTION_RETRY_LATER)

    async def test_an_unreachable_token_endpoint_stays_a_connection_failure(self):
        with patch.object(
            chatgpt_auth.aiohttp, 'ClientSession', return_value=FakeTokenSession(error=TimeoutError())
        ):
            with self.assertRaises(HTTPException) as ctx:
                await chatgpt_auth.oauth_form({'grant_type': 'refresh_token'})
        error = ctx.exception
        self.assertEqual(error.provider_category, provider_errors.CONNECTION)
        self.assertEqual(error.provider_stage, provider_errors.STAGE_OAUTH_REFRESH)

    def test_an_oauth_connection_asks_for_a_sign_in_not_a_new_key(self):
        """The suggested action has to match how the connection authenticates."""
        connection = SimpleNamespace(auth_type='oauth')
        error = providers.provider_http_error(401)
        providers._recovery_action(connection, error)
        self.assertEqual(error.provider_suggested_action, provider_errors.ACTION_RECONNECT)

        key_connection = SimpleNamespace(auth_type='api_key')
        key_error = providers.provider_http_error(401)
        providers._recovery_action(key_connection, key_error)
        self.assertEqual(key_error.provider_suggested_action, provider_errors.ACTION_REPLACE_KEY)

    def test_an_unknown_error_stays_unknown(self):
        error = providers.catalog_error(HTTPException(502, 'Could not reach provider'))
        self.assertEqual(error['reason'], provider_errors.REASON_UNKNOWN)
        self.assertIsNone(error['stage'])
