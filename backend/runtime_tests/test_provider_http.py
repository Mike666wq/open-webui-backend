"""Provider transport invariants: address policy and the five error categories.

The proxy fake-IP range (RFC 2544, 198.18.0.0/15) used to be rejected by the
shared SSRF guard, which made every provider on a proxied host fail with an
opaque HTTP 502. These tests pin the address policy and the error taxonomy that
replaced it.
"""

import ipaddress
import unittest
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
