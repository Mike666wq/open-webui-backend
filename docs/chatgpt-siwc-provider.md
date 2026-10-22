# ChatGPT provider sign-in

Open WebUI connects to ChatGPT plan usage through OpenAI's official Sign in with
ChatGPT (SIWC) open-source flow. The provider is private to the user who signs
in. Each backend runtime stores one stable `ext_agent_host_id`, and the
connection stores its issued client registration and encrypted renewable OAuth
credentials.

## Connect a self-hosted server

1. In Open WebUI, start a ChatGPT sign-in attempt and copy its short-lived
   pairing code.
2. Run the command shown by Open WebUI on a computer with a browser. It
   downloads the stdlib-only helper from the Open WebUI server, then prompts
   for the pairing code.
3. The helper claims the attempt over HTTPS, opens the official OpenAI sign-in
   page, and listens on `http://127.0.0.1:<port>/auth/callback`. It exchanges
   the authorization code locally with PKCE, then sends the short-lived result
   to the backend over HTTPS.
4. Open WebUI validates the ID-token signature against OpenAI's published
   JWKS, checks issuer, audience, expiry, nonce, account identity, and the
   granted `chatgpt.tokens.use.direct`, `resource.invoke`, and `offline_access` scopes. It then
   encrypts and stores the tokens in the owner-only provider connection.

The pairing code expires after ten minutes and can be claimed once. The helper
keeps OAuth tokens in memory, does not print callback URLs or tokens, and never
places credentials in a URL or shell command. The browser callback uses the
loopback machine, which is why it must run on the user's computer rather than
on the remote backend VM.

Reauthorization for a saved connection reuses its issued client ID, stable host
ID, and validated account subject. Disconnect attempts the documented SIWC
refresh-token revocation endpoint and clears local tokens while retaining the
registration mapping for later sign-in. The API response reports whether remote
revocation was confirmed.

## Requests and current limits

Model discovery calls `GET https://api.openai.com/v1/models` with the user's
OAuth access token, then lists only records whose `visibility` is `list` and
uses their `slug` as the selected model ID. Chat uses
`POST https://api.openai.com/v1/responses` with `store: false` and `stream:
true`. System messages become Responses `instructions`; the established
Open WebUI converter handles message history, images, and function-call
history. It omits fields currently rejected by SIWC's preview, including
temperature, top-p, output-token limits, previous response IDs, and unsupported
tools. The backend does not call Codex app-server or ChatGPT `backend-api`
endpoints.

This is a preview integration. Supported models and tool behavior depend on the
signed-in account, workspace policy, and OpenAI's current preview limits. SIWC
plan usage shares the account's ChatGPT usage limits. Check the current official
guides before changing protocol details:

- [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Self-hosted VMs](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms)
- [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Token reference](https://developers.openai.com/siwc/token-sharing-open-source/token-reference)
- [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)

Run the offline SIWC and provider contract tests with
`python scripts/test_provider_runtime.py`. This launcher sets a temporary
database and secret before importing backend modules, so running the runtime
test files directly is unsupported.
