"""Provider error taxonomy.

Five categories, matching the official documentation: a failure to reach the
provider at all, a network path blocked between this server and the provider,
rejected credentials, exhausted quota, and the provider's own service fault.

The category, the HTTP status and the Chinese `hint` are produced here so the
backend and the frontend never grow two different vocabularies for the same
failure. `hint` is display-ready: the frontend renders it verbatim.

Three optional fields refine a category without replacing it:

- `stage` says where the failure happened: `catalog`, `oauth_refresh` or
  `inference`. A refresh that fails inside a catalog call is an
  `oauth_refresh` failure, not a `catalog` one.
- `reason` says why, in the provider's own terms where that is knowable
  (`invalid_grant`, `token_expired`, `address_blocked`, ...). It stays
  `unknown` whenever the evidence is not enough to claim more.
- `suggested_action` says what the operator can do next: `reconnect`,
  `replace_key`, `check_permissions`, `check_server_network`, `retry_later`
  or `inspect_connection`.

The fields are additive. A category on its own still says everything the old
payload said, so a client that predates them keeps working.
"""

import time

CONNECTION = 'connection'
PROVIDER_NETWORK = 'provider_network'
AUTHENTICATION = 'authentication'
RATE_LIMIT = 'rate_limit'
UPSTREAM = 'upstream'

CATEGORIES = (CONNECTION, PROVIDER_NETWORK, AUTHENTICATION, RATE_LIMIT, UPSTREAM)

RETRYABLE_CATEGORIES = (CONNECTION, PROVIDER_NETWORK, UPSTREAM)

STAGE_CATALOG = 'catalog'
STAGE_OAUTH_REFRESH = 'oauth_refresh'
STAGE_INFERENCE = 'inference'

STAGES = (STAGE_CATALOG, STAGE_OAUTH_REFRESH, STAGE_INFERENCE)

REASON_INVALID_CREDENTIALS = 'invalid_credentials'
REASON_TOKEN_EXPIRED = 'token_expired'
REASON_INSUFFICIENT_PERMISSIONS = 'insufficient_permissions'
REASON_RATE_LIMITED = 'rate_limited'
REASON_QUOTA_EXHAUSTED = 'quota_exhausted'
REASON_ADDRESS_BLOCKED = 'address_blocked'
REASON_DNS_FAILURE = 'dns_failure'
REASON_TLS_FAILURE = 'tls_failure'
REASON_TIMEOUT = 'timeout'
REASON_UPSTREAM_FAILURE = 'upstream_failure'
REASON_UNKNOWN = 'unknown'

REASONS = (
    REASON_INVALID_CREDENTIALS,
    REASON_TOKEN_EXPIRED,
    REASON_INSUFFICIENT_PERMISSIONS,
    REASON_RATE_LIMITED,
    REASON_QUOTA_EXHAUSTED,
    REASON_ADDRESS_BLOCKED,
    REASON_DNS_FAILURE,
    REASON_TLS_FAILURE,
    REASON_TIMEOUT,
    REASON_UPSTREAM_FAILURE,
    REASON_UNKNOWN,
)

ACTION_RECONNECT = 'reconnect'
ACTION_REPLACE_KEY = 'replace_key'
ACTION_CHECK_PERMISSIONS = 'check_permissions'
ACTION_CHECK_SERVER_NETWORK = 'check_server_network'
ACTION_RETRY_LATER = 'retry_later'
ACTION_INSPECT_CONNECTION = 'inspect_connection'

ACTIONS = (
    ACTION_RECONNECT,
    ACTION_REPLACE_KEY,
    ACTION_CHECK_PERMISSIONS,
    ACTION_CHECK_SERVER_NETWORK,
    ACTION_RETRY_LATER,
    ACTION_INSPECT_CONNECTION,
)

_DEFAULT_HINTS = {
    CONNECTION: '无法连接到厂商接口，请检查服务器网络、DNS 与代理设置。',
    PROVIDER_NETWORK: '连接被服务器的安全策略拦截，请检查地址解析结果与代理配置。',
    AUTHENTICATION: '厂商拒绝了当前凭据，请重新填写 API Key 或重新登录账号。',
    RATE_LIMIT: '厂商限流或额度不足，请等待片刻或检查账户套餐与余额。',
    UPSTREAM: '厂商服务自身返回异常，请稍后重试或联系厂商。',
}

# A category alone rarely identifies the cause. These defaults are the
# conservative reading of the category; a caller that knows more overrides them.
_DEFAULT_REASONS = {
    CONNECTION: REASON_UNKNOWN,
    PROVIDER_NETWORK: REASON_ADDRESS_BLOCKED,
    AUTHENTICATION: REASON_INVALID_CREDENTIALS,
    RATE_LIMIT: REASON_RATE_LIMITED,
    UPSTREAM: REASON_UPSTREAM_FAILURE,
}

_DEFAULT_ACTIONS = {
    CONNECTION: ACTION_CHECK_SERVER_NETWORK,
    PROVIDER_NETWORK: ACTION_CHECK_SERVER_NETWORK,
    AUTHENTICATION: ACTION_REPLACE_KEY,
    RATE_LIMIT: ACTION_RETRY_LATER,
    UPSTREAM: ACTION_RETRY_LATER,
}

# A reason refines the category, and the wording has to follow it. A 403 is a
# permission the account does not have; telling the operator to replace the key
# would send them down the wrong road.
_REASON_HINTS = {
    REASON_INVALID_CREDENTIALS: '厂商拒绝了当前凭据，请重新填写 API Key 或重新登录账号。',
    REASON_INSUFFICIENT_PERMISSIONS: '厂商拒绝了这次请求的权限，这通常不是 API Key 填错，请检查该连接的模型范围与账号权限。',
    REASON_TOKEN_EXPIRED: '登录状态已失效，请重新登录该账号。',
    REASON_QUOTA_EXHAUSTED: '厂商额度或余额不足，请检查账户套餐与余额。',
    REASON_RATE_LIMITED: '厂商限流，请等待片刻后重试。',
    REASON_ADDRESS_BLOCKED: '连接被服务器的安全策略拦截，请检查地址解析结果与代理配置。',
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
        stage: str | None = None,
        reason: str | None = None,
        suggested_action: str | None = None,
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
        self.stage = stage if stage in STAGES else None
        self.reason = reason if reason in REASONS else _DEFAULT_REASONS[self.category]
        self.suggested_action = (
            suggested_action if suggested_action in ACTIONS else _DEFAULT_ACTIONS[self.category]
        )
        super().__init__(self.detail)


def category_for_status(status: int) -> str:
    """Map an upstream HTTP status onto one of the five categories."""
    if status in (401, 403):
        return AUTHENTICATION
    if status in (402, 429):
        return RATE_LIMIT
    return UPSTREAM


def reason_for_status(status: int) -> str:
    """Map an upstream HTTP status onto a reason, without guessing further."""
    if status == 401:
        return REASON_INVALID_CREDENTIALS
    if status == 403:
        return REASON_INSUFFICIENT_PERMISSIONS
    if status == 402:
        return REASON_QUOTA_EXHAUSTED
    if status == 429:
        return REASON_RATE_LIMITED
    return REASON_UPSTREAM_FAILURE


def action_for_status(status: int) -> str:
    """The default operator action for an upstream HTTP status."""
    if status == 401:
        return ACTION_REPLACE_KEY
    if status == 403:
        return ACTION_CHECK_PERMISSIONS
    if status in (402, 429):
        return ACTION_RETRY_LATER
    return ACTION_RETRY_LATER


def build_error(
    category: str,
    status: int,
    *,
    http_status: int | None = None,
    upstream_status: int | None = None,
    hint: str | None = None,
    retryable: bool | None = None,
    stage: str | None = None,
    reason: str | None = None,
    suggested_action: str | None = None,
) -> dict:
    """Build the persisted/public error payload for a provider failure."""
    category = category if category in CATEGORIES else UPSTREAM
    resolved_reason = reason if reason in REASONS else _DEFAULT_REASONS[category]
    resolved_action = (
        suggested_action if suggested_action in ACTIONS else _DEFAULT_ACTIONS[category]
    )
    return {
        'category': category,
        'status': status,
        'http_status': http_status if http_status is not None else status,
        'upstream_status': upstream_status,
        'retryable': retryable if retryable is not None else category in RETRYABLE_CATEGORIES,
        'at': int(time.time()),
        'hint': hint or _REASON_HINTS.get(resolved_reason) or _DEFAULT_HINTS[category],
        'stage': stage if stage in STAGES else None,
        'reason': resolved_reason,
        'suggested_action': resolved_action,
    }


def error_from_exception(exc) -> dict:
    """Translate any provider failure into the five-category payload.

    Two shapes reach this function: the `HTTPException` the routers raise, and
    the `ProviderRequestError` the transport raises. Both are understood, so
    a caller does not have to convert one into the other first.
    """
    category = getattr(exc, 'provider_category', None) or getattr(exc, 'category', None)
    upstream_status = getattr(exc, 'provider_status', None)
    if upstream_status is None:
        upstream_status = getattr(exc, 'upstream_status', None)
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
    hint = getattr(exc, 'provider_hint', None) or getattr(exc, 'hint', None)
    reason = getattr(exc, 'provider_reason', None) or getattr(exc, 'reason', None)
    suggested_action = getattr(exc, 'provider_suggested_action', None) or getattr(
        exc, 'suggested_action', None
    )
    stage = getattr(exc, 'provider_stage', None) or getattr(exc, 'stage', None)
    if reason is None and upstream_status is not None:
        reason = reason_for_status(upstream_status)
    if suggested_action is None and upstream_status is not None:
        suggested_action = action_for_status(upstream_status)
    return build_error(
        category,
        status,
        http_status=http_status,
        upstream_status=upstream_status,
        hint=hint,
        stage=stage,
        reason=reason,
        suggested_action=suggested_action,
    )
