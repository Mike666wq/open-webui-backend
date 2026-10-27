"""Provider error taxonomy.

Five categories, matching the official documentation: a failure to reach the
provider at all, a network path blocked between this server and the provider,
rejected credentials, exhausted quota, and the provider's own service fault.

The category, the HTTP status and the Chinese `hint` are produced here so the
backend and the frontend never grow two different vocabularies for the same
failure. `hint` is display-ready: the frontend renders it verbatim.
"""

import time

CONNECTION = 'connection'
PROVIDER_NETWORK = 'provider_network'
AUTHENTICATION = 'authentication'
RATE_LIMIT = 'rate_limit'
UPSTREAM = 'upstream'

CATEGORIES = (CONNECTION, PROVIDER_NETWORK, AUTHENTICATION, RATE_LIMIT, UPSTREAM)

RETRYABLE_CATEGORIES = (CONNECTION, PROVIDER_NETWORK, UPSTREAM)

_DEFAULT_HINTS = {
    CONNECTION: '无法连接到厂商接口，请检查服务器网络、DNS 与代理设置。',
    PROVIDER_NETWORK: '连接被服务器的安全策略拦截，请检查地址解析结果与代理配置。',
    AUTHENTICATION: '厂商拒绝了当前凭据，请重新填写 API Key 或重新登录账号。',
    RATE_LIMIT: '厂商限流或额度不足，请等待片刻或检查账户套餐与余额。',
    UPSTREAM: '厂商服务自身返回异常，请稍后重试或联系厂商。',
}


class ProviderRequestError(Exception):
    """A provider call failed with a known category."""

    def __init__(
        self,
        category: str,
        status: int,
        *,
        http_status: int | None = None,
        upstream_status: int | None = None,
        detail: str | None = None,
        hint: str | None = None,
        retryable: bool | None = None,
    ):
        self.category = category if category in CATEGORIES else UPSTREAM
        self.status = status
        self.http_status = http_status if http_status is not None else status
        self.upstream_status = upstream_status
        self.detail = detail or _DEFAULT_HINTS[self.category]
        self.hint = hint or _DEFAULT_HINTS[self.category]
        self.retryable = (
            retryable if retryable is not None else self.category in RETRYABLE_CATEGORIES
        )
        super().__init__(self.detail)


def category_for_status(status: int) -> str:
    """Map an upstream HTTP status onto one of the five categories."""
    if status in (401, 403):
        return AUTHENTICATION
    if status in (402, 429):
        return RATE_LIMIT
    return UPSTREAM


def build_error(
    category: str,
    status: int,
    *,
    http_status: int | None = None,
    upstream_status: int | None = None,
    hint: str | None = None,
    retryable: bool | None = None,
) -> dict:
    """Build the persisted/public error payload for a provider failure."""
    category = category if category in CATEGORIES else UPSTREAM
    return {
        'category': category,
        'status': status,
        'http_status': http_status if http_status is not None else status,
        'upstream_status': upstream_status,
        'retryable': retryable if retryable is not None else category in RETRYABLE_CATEGORIES,
        'at': int(time.time()),
        'hint': hint or _DEFAULT_HINTS[category],
    }


def error_from_exception(exc) -> dict:
    """Translate any provider failure into the five-category payload."""
    category = getattr(exc, 'provider_category', None)
    upstream_status = getattr(exc, 'provider_status', None)
    http_status = getattr(exc, 'status_code', None)
    if http_status is None:
        http_status = getattr(exc, 'http_status', None)
    if http_status is None:
        http_status = 502

    if category is None:
        if upstream_status is not None:
            category = category_for_status(upstream_status)
        elif http_status in (401, 403):
            category = AUTHENTICATION
        elif http_status in (402, 429):
            category = RATE_LIMIT
        elif upstream_status is not None:
            category = UPSTREAM
        else:
            category = CONNECTION

    status = upstream_status or http_status
    hint = getattr(exc, 'provider_hint', None)
    return build_error(
        category,
        status,
        http_status=http_status,
        upstream_status=upstream_status,
        hint=hint,
    )
