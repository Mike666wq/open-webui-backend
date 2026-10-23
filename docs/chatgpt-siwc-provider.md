# ChatGPT provider sign-in

Open WebUI connects to ChatGPT plan usage through OpenAI's official Sign in with
ChatGPT (SIWC) open-source flow. The provider is private to the user who signs
in. Each backend runtime stores one stable `ext_agent_host_id`, and the
connection stores its issued client registration and encrypted renewable OAuth
credentials.

## Connect a self-hosted server

1. In Open WebUI, select ChatGPT and start browser sign-in.
2. Open the official authorization link from the dialog and authorize the account.
3. Copy the entire final callback URL from the browser address bar, return to
   Open WebUI, and submit it. With a remote backend, the loopback page may show
   a connection error; the address still contains the authorization result.
4. The backend checks the exact callback origin/path, state, issued client ID,
   attempt ownership and expiry. It atomically consumes the encrypted PKCE
   verifier, exchanges the code, validates the ID token and required scopes,
   then saves an owner-only encrypted session. Codes and tokens are not returned
   in the status response. Cancellation discards late token responses.

No local script or Python installation is required. This follows Pi's manual
browser callback interaction. It does not claim automatic callback capture in
web browsers or a SIWC device-code grant. Pi's separate Codex device-code flow
uses a different client and inference API and is not interchangeable with SIWC.

The backend generates state, nonce and PKCE for each attempt. The verifier is
encrypted at rest, removed on consumption/cancellation/status expiry, and never
returned to the browser. Attempts last ten minutes; submission is one-time.
Keep the pasted URL private. Older clients can still use the helper pairing
API during rolling upgrades; the new UI never requires or downloads a helper.

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

The transport assembles complete SSE events before passing them to the chat
consumer. Network chunks may contain multiple events or split a JSON value or
UTF-8 character. A successful sign-in/model listing and an HTTP 200 alone do
not confirm a successful generation: Responses requests must reach
`response.completed`. In-stream failures (including plan usage limits),
incomplete responses and interrupted streams appear as chat errors. Look for
`Provider stream` warnings and the corresponding chat error when diagnosing
an empty reply; routine GET/access-polling logs do not show generation success.

SIWC plan OAuth does not authorize image generation tools, audio/video input,
transcription endpoints, speech synthesis, or Realtime. Supported image/file
**input** to a Responses model is distinct from generating a new image.
Configure a separate API-key connection for images, speech, and transcription
in the media settings. Realtime retains its dedicated API-key configuration.
See the [provider guide](provider-connections.md) for setup.

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
