# deploy-notifier — design

Outcome of the design grilling (2026-10-04). The README is the user-facing doc; this
file records *why* things are the way they are.

## Scope

- The **action** only notifies: it POSTs "a new version is available" to one configurable
  URL and waits for the server's verdict. It never ships the image itself.
- A small **reference verifier** (`verifier/deploy_verifier.py`) is included so the
  receiving side verifies correctly by default. What "deploying" means is a per-app
  command on the server (e.g. `docker compose up -d`), not this repo's concern.

## Decisions

| # | Decision |
|---|----------|
| Transport | HTTPS only. `http://` requires `allow_insecure: true`. TLS certs are always verified; redirects are never followed. |
| Payload | Generic: optional `image`, `version`, `extra` (JSON object), plus GitHub context added automatically. Recommended convention: digest-pinned GHCR refs (`ghcr.io/o/app@sha256:…`); the verifier enforces digests by default. |
| Auth | GitHub **OIDC** by default (`Authorization: Bearer <id-token>`, `aud` = URL origin unless overridden). **HMAC** fallback: `X-Signature-256: sha256=HMAC(secret, "<ts>.<body>")` + `X-Timestamp`. |
| Authorisation | Verifier pins `repository_id` (+ optional `repository_owner_id`) and `environment` and/or `ref` per app. IDs, not names, because names can be re-registered. |
| Replay | OIDC: `jti` recorded until `exp`, tokens older than `max_token_age` rejected. HMAC: ±5 min timestamp window + signature recorded. |
| Response contract | Server answers only when done. `2xx` = deployed, `202` = accepted/deferred, anything else fails the job. Outputs: `status`, `http_code`, `response` (≤4 KB). |
| Timeouts | `timeout` input (default 600 s) for the whole request; connect timeout fixed at 10 s. |
| Retries | Max 3 attempts, only when the request evidently never reached the app: DNS/connect/TLS-handshake failure, HTTP 502/503. **Never** on client timeout or 504 (the deploy may still be running). |
| Idempotency | `X-Deploy-Id` / `deploy_id` = `<run_id>-<run_attempt>-<sha256(url)[:12]>`. Verifier dedupes per app: a repeat of a finished deploy returns the cached verdict; a repeat of a running one gets 409. Re-running the workflow (new attempt) deploys again. |
| Targets | One URL per step; fan out with a matrix. |
| Action impl. | Composite bash + curl + jq. All inputs go through `env:` (no `${{ }}` in `run:`). Secrets/tokens go to curl via a 0600 header file, not argv. Server output is printed inside `::stop-commands::` so it can't inject workflow commands. |
| Verifier impl. | Single-file Python (PEP 723, `uv run --script`), stdlib HTTP server on localhost behind Caddy/nginx. Runs the app's command via argv (no shell) with a minimal env; exit 0 → 200, exit 75 → 202, timeout → 504, else 500. One deploy at a time per app. Runs as a dedicated non-login `deploy` user. |

## Deviation noted during implementation

- Earlier we said "refuse bare IPs unless `allow_insecure`". Dropped: the original
  requirement is "hostname/IP", and TLS verification is enforced anyway, so an IP
  target only works if it has a valid IP certificate (e.g. Let's Encrypt IP certs).
