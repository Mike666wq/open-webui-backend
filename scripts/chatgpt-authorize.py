#!/usr/bin/env python3
"""Complete a local ChatGPT SIWC login and pair it with a self-hosted WebUI.

Only Python's standard library is required. This helper keeps OAuth credentials
in memory and sends them over HTTPS to the backend that issued the pairing code.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

AUTHORIZATION_ENDPOINT = 'https://auth.openai.com/api/accounts/authorize'
TOKEN_ENDPOINT = 'https://auth.openai.com/api/accounts/oauth/token'
RESOURCE = 'https://api.openai.com/v1'
CALLBACK_PATH = '/auth/callback'


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT = urllib.request.build_opener(NoRedirectHandler)


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict | None = None
    expected_state: str = ''

    def log_message(self, _format, *args):
        # Default access logs would print the query string, including the
        # short-lived authorization code.
        return

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_error(404)
            return
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        state = (query.get('state') or [''])[0]
        if not secrets.compare_digest(state, self.expected_state):
            self.send_error(400, 'Authorization state did not match')
            return
        self.__class__.result = {key: values[0] for key, values in query.items() if values}
        page = b'<!doctype html><title>ChatGPT connected</title><p>Authorization received. You can close this tab and return to the terminal.</p>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(page)))
        self.end_headers()
        self.wfile.write(page)


def post_form(url: str, values: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(values).encode()
    request = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'})
    try:
        with _NO_REDIRECT.open(request, timeout=30) as response:
            payload = json.loads(response.read(256_000))
        if not isinstance(payload, dict):
            raise RuntimeError()
        return payload
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        raise RuntimeError('Could not complete ChatGPT authorization. Start a new attempt in Open WebUI.') from None


def post_json(url: str, payload: dict, helper_token: str | None = None) -> dict:
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    if helper_token:
        headers['Authorization'] = f'Bearer {helper_token}'
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    try:
        with _NO_REDIRECT.open(request, timeout=30) as response:
            value = json.loads(response.read(256_000))
        if not isinstance(value, dict):
            raise RuntimeError()
        return value
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        raise RuntimeError('Could not pair with Open WebUI. Start a new attempt and try again.') from None


def validate_server_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != 'https' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('The Open WebUI server URL must use HTTPS.')
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Enter the base URL of your Open WebUI server.')
    return value.rstrip('/')


def run(server_url: str, pairing_code: str) -> None:
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = b64url(hashlib.sha256(verifier.encode('ascii')).digest())
    CallbackHandler.result = None
    CallbackHandler.expected_state = state
    server = http.server.HTTPServer(('127.0.0.1', 0), CallbackHandler)
    server.timeout = 1
    port = server.server_address[1]
    redirect_uri = f'http://127.0.0.1:{port}{CALLBACK_PATH}'
    pair = post_json(f'{server_url}/api/v1/providers/auth/chatgpt/pair', {
        'pairing_code': pairing_code,
        'state': state,
        'nonce': nonce,
        'code_challenge': challenge,
        'redirect_uri': redirect_uri,
    })
    helper_token = pair.get('helper_token')
    attempt_id = pair.get('attempt_id')
    client_id = pair.get('client_id')
    host_id = pair.get('host_id')
    if not all(isinstance(value, str) and value for value in (helper_token, attempt_id, client_id, host_id)):
        raise RuntimeError('The server returned an incomplete pairing response.')

    params = {
        'client_id': client_id,
        'ext_agent_host_id': host_id,
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'scope': 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct',
        'resource': RESOURCE,
        'state': state,
        'nonce': nonce,
        'code_challenge_method': 'S256',
        'code_challenge': challenge,
    }
    if client_id == 'dynamic_agent_client':
        params['agent_name_hint'] = pair.get('agent_name') or 'Open WebUI'
    if pair.get('id_token_hint'):
        params['id_token_hint'] = pair['id_token_hint']
    auth_url = f'{AUTHORIZATION_ENDPOINT}?{urllib.parse.urlencode(params)}'
    def wait_for_callback():
        deadline = time.monotonic() + 300
        while CallbackHandler.result is None and time.monotonic() < deadline:
            server.handle_request()

    worker = threading.Thread(target=wait_for_callback, daemon=True)
    worker.start()
    print('Opening the official OpenAI sign-in page in your browser...')
    if not webbrowser.open(auth_url):
        # The URL can contain an id_token_hint on reauthorization; never print
        # it into a terminal log.
        worker.join(timeout=2)
        server.server_close()
        raise RuntimeError('Could not open your browser. Run this helper in a desktop session with a default browser.')
    worker.join(timeout=305)
    server.server_close()
    callback = CallbackHandler.result
    if not callback:
        raise RuntimeError('The local callback timed out. Start a new attempt in Open WebUI.')
    if callback.get('error'):
        raise RuntimeError('ChatGPT sign-in was cancelled or denied.')
    code = callback.get('code')
    issued_client_id = callback.get('client_id') or client_id
    if not code:
        raise RuntimeError('ChatGPT returned an unexpected authorization result.')
    if client_id == 'dynamic_agent_client':
        if not issued_client_id.startswith('oaiapp_'):
            raise RuntimeError('ChatGPT did not complete the client registration.')
    elif issued_client_id != client_id:
        raise RuntimeError('ChatGPT returned an unexpected client registration.')
    token = post_form(TOKEN_ENDPOINT, {
        'grant_type': 'authorization_code',
        'code': code,
        'client_id': issued_client_id,
        'code_verifier': verifier,
        'redirect_uri': redirect_uri,
        'resource': RESOURCE,
    })
    scope = token.get('scope') or callback.get('scope') or ''
    post_json(f'{server_url}/api/v1/providers/auth/chatgpt/complete', {
        'state': state,
        'client_id': issued_client_id,
        'token': token,
        'granted_scope': scope,
    }, helper_token=helper_token)
    print('ChatGPT is connected. Return to Open WebUI.')


def main() -> int:
    parser = argparse.ArgumentParser(description='Authorize ChatGPT for your Open WebUI account.')
    parser.add_argument('--server-url', required=True, help='Base URL of the Open WebUI server.')
    args = parser.parse_args()
    try:
        server_url = validate_server_url(args.server_url)
        pairing_code = input('Enter the one-time pairing code shown in Open WebUI: ').strip().upper()
        if len(pairing_code) < 8:
            raise ValueError('The pairing code is too short.')
        run(server_url, pairing_code)
        return 0
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
