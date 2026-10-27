"""Outbound HTTP for provider connections.

Fork-owned module. The upstream `utils/apps.py` SSRF guard is reused only for
URL validation; its DNS-level private-address rejection is deliberately not
reused, because it also rejects the RFC 2544 benchmark range
(198.18.0.0/15) that proxy stacks such as Clash use for fake-IP DNS. That
collision made every provider on a proxied host fail with an opaque HTTP 502.

The guard stays strict by default. An operator may opt in to a specific range
through `PROVIDER_ALLOWED_IP_RANGES` (comma-separated CIDRs). When a request is
blocked the failure is reported as `provider_network` with the resolved address
included, never as a generic network error.
"""

import ipaddress
import json
import os
from typing import Any

import aiohttp
from open_webui.utils.apps import validate_app_url
from open_webui.utils.provider_errors import (
    CONNECTION,
    PROVIDER_NETWORK,
    ProviderRequestError,
    category_for_status,
)

DEFAULT_TIMEOUT = 20
MAX_RESPONSE_BYTES = 2_000_000
ALLOWED_RANGES_ENV = 'PROVIDER_ALLOWED_IP_RANGES'


class ProviderAddressBlocked(Exception):
    """A hostname resolved to an address the security policy refuses to use."""

    def __init__(self, host: str, addresses: list[str]):
        self.host = host
        self.addresses = addresses
        super().__init__(f'Provider endpoint resolves to a prohibited address: {host}')


def allowed_ip_networks() -> list:
    """Parse `PROVIDER_ALLOWED_IP_RANGES` into CIDR networks (empty by default)."""
    raw = os.getenv(ALLOWED_RANGES_ENV, '')
    networks = []
    for item in raw.split(','):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return networks


def is_public_ip_address(ip_address: str) -> bool:
    """Return whether an address is globally routable.

    Mirrors the upstream guard so provider traffic is not more permissive than
    the rest of the platform, while keeping the logic in a fork-owned module.
    """
    try:
        address = ipaddress.ip_address(ip_address)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        embedded = [address.ipv4_mapped, address.sixtofour]
        if address.teredo:
            embedded.extend(address.teredo)
        if address in ipaddress.ip_network('64:ff9b::/96') or address in ipaddress.ip_network('64:ff9b:1::/48'):
            return False
        return all(x is None or (x.is_global and not x.is_multicast) for x in embedded)
    return True


def is_explicitly_allowed(ip_address: str, networks: list) -> bool:
    """Return whether an address falls into an operator-allowed range."""
    try:
        address = ipaddress.ip_address(ip_address)
    except ValueError:
        return False
    for network in networks:
        if address.version == network.version and address in network:
            return True
    return False


class ProviderTCPConnector(aiohttp.TCPConnector):
    """TCP connector that refuses non-public destinations unless allowed."""

    def __init__(self, *, allow_private: bool = False, allowed_networks: list | None = None):
        super().__init__(use_dns_cache=False)
        self.allow_private = allow_private
        self.allowed_networks = allowed_networks if allowed_networks is not None else []

    async def _resolve_host(self, host, port, traces=None):
        addresses = await super()._resolve_host(host, port, traces)
        if self.allow_private:
            return addresses
        blocked = []
        for entry in addresses:
            ip_address = entry['host']
            if is_public_ip_address(ip_address):
                continue
            if is_explicitly_allowed(ip_address, self.allowed_networks):
                continue
            blocked.append(ip_address)
        if blocked:
            raise ProviderAddressBlocked(host, blocked)
        return addresses


async def _read_bounded_body(response, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    """Read through EOF, including chunked bodies, with a hard memory limit."""
    chunks = []
    size = 0
    async for chunk in response.content.iter_chunked(16384):
        size += len(chunk)
        if size > limit:
            raise ProviderRequestError(
                PROVIDER_NETWORK, 502, detail='Provider response is too large', retryable=False
            )
        chunks.append(chunk)
    return b''.join(chunks)


def _blocked_hint(host: str, addresses: list[str]) -> str:
    resolved = '、'.join(addresses[:4])
    return (
        f'地址 {host} 解析到 {resolved}，落在保留网段（代理的 fake-ip 常用该段），已被服务器安全策略拦截。'
        f'如确认该网段可信，请为服务设置环境变量 {ALLOWED_RANGES_ENV} 后重启；'
        '或改用 redir-host 模式 / HTTP 代理方式接入。'
    )


async def provider_request_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    method: str = 'GET',
    body: Any = None,
    params: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    allowed_networks: list | None = None,
) -> Any:
    """Perform a provider request and return the decoded JSON body.

    Raises `ProviderRequestError` with one of the five categories on failure.
    """
    request_headers = dict(headers or {})
    if body is not None:
        request_headers['Content-Type'] = 'application/json'

    allow_private = validate_app_url(url.split('?', 1)[0])
    networks = allowed_networks if allowed_networks is not None else allowed_ip_networks()
    connector = ProviderTCPConnector(allow_private=allow_private, allowed_networks=networks)

    try:
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=timeout),
            trust_env=False,
        ) as session:
            async with session.request(
                method, url, json=body, params=params, headers=request_headers, allow_redirects=False
            ) as response:
                raw = await _read_bounded_body(response)
                if response.status < 200 or response.status >= 300:
                    raise ProviderRequestError(
                        category_for_status(response.status),
                        response.status,
                        upstream_status=response.status,
                        detail=_status_detail(response.status),
                        retryable=response.status >= 500,
                    )
                try:
                    return json.loads(raw) if raw else {}
                except (ValueError, UnicodeDecodeError):
                    raise ProviderRequestError(
                        PROVIDER_NETWORK,
                        502,
                        detail='Provider returned an invalid response',
                        hint='厂商返回的内容不是合法 JSON，请确认 API 地址指向正确的接口根路径。',
                        retryable=False,
                    ) from None
    except ProviderRequestError:
        raise
    except ProviderAddressBlocked as exc:
        raise ProviderRequestError(
            PROVIDER_NETWORK,
            502,
            detail='Provider endpoint resolves to a prohibited address',
            hint=_blocked_hint(exc.host, exc.addresses),
        ) from None
    except aiohttp.ClientSSLError:
        raise ProviderRequestError(
            CONNECTION,
            502,
            detail='Provider TLS verification failed',
            hint='与厂商的 TLS 握手失败，请检查证书链、服务器时间或中间代理。',
        ) from None
    except aiohttp.ClientConnectorDNSError:
        raise ProviderRequestError(
            CONNECTION,
            502,
            detail='Provider hostname could not be resolved',
            hint='服务器无法解析厂商域名，请检查 DNS 与 hosts 配置。',
        ) from None
    except (aiohttp.ClientError, TimeoutError):
        raise ProviderRequestError(
            CONNECTION,
            502,
            detail='Could not reach the provider',
            hint='连接厂商超时或被拒绝，请检查服务器出网网络与代理设置。',
        ) from None


def _status_detail(status: int) -> str:
    if status in (401, 403):
        return 'Provider rejected the configured credentials or permissions'
    if status == 429:
        return 'Provider usage limit reached; wait or check your plan quota'
    if status == 402:
        return 'Provider quota or balance is insufficient'
    return f'Provider request failed with HTTP {status}'
