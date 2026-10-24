# Protocol

This is the contract for writing your own receiver. The reference verifier implements it.

## Request

`POST <url>` with `Content-Type: application/json`:

```json
{
  "image": "ghcr.io/owner/app@sha256:…",  "version": "1.2.3",  "extra": {},
  "repository": "owner/app", "repository_id": "123", "sha": "…", "ref": "refs/heads/main",
  "run_id": "456", "run_attempt": "1", "actor": "octocat",
  "deploy_id": "456-1-0123456789ab", "sent_at": "2026-10-04T12:00:00Z"
}
```

`image` and `version` may be `null`. `extra` is always an object.

## Headers

| Header | When |
|---|---|
| `X-Deploy-Id` | Always. `<run_id>-<run_attempt>-<sha256(url)[:12]>`. Dedupe on it. |
| `Authorization: Bearer <jwt>` | `auth: oidc`. Verify it as a GitHub Actions OIDC token: RS256, `iss` = `https://token.actions.githubusercontent.com`, `aud`, `exp`. Then pin `repository_id`, `environment` or `ref`, reject a reused `jti`, and check that the body's `repository_id`, `sha`, `run_id` and `run_attempt` match the token. |
| `X-Timestamp`, `X-Signature-256: sha256=<hex>` | `auth: hmac`. HMAC-SHA256 over `"<timestamp>.<raw body>"`. Reject a timestamp more than 5 minutes off, and reject a reused signature. |
| `User-Agent: deploy-notifier/1` | Always. |

GitHub publishes the signing keys at
`https://token.actions.githubusercontent.com/.well-known/jwks`.

## Response

The receiver answers only when the deploy is done:

| Code | Meaning |
|---|---|
| `200` (or any `2xx` but `202`) | Deployed. The step passes. |
| `202` | Accepted but deferred. The step passes. |
| anything else | The step fails. |

The action shows at most 4 KB of the body. A JSON body is conventional but not required.

## Retries and replay

The action retries on a connection failure and on `502` and `503`. Each attempt carries a
new token or signature, so a retry passes the replay check. The deploy id stays the same
across attempts, so dedupe on it:

- A repeat of a finished deploy should return the stored verdict without deploying again.
- A repeat of a running deploy should return `409`.
- A new workflow attempt (re-run) has a new `run_attempt`, so it gets a new deploy id and
  deploys again.

Answer `503` only when nothing has started, for example when you can't fetch the JWKS.
The action retries it. Never answer `502` or `503` once the deploy has begun.

## Reference verifier answers

| Code | Body `error` | Cause |
|---|---|---|
| `200` | | Command exited 0. Body: `{"status": "deployed", "deploy_id": …}`. |
| `202` | | Command exited 75. Body: `{"status": "deferred", "deploy_id": …}`. |
| `200`/`202` | | Repeat of a finished deploy. The body adds `"duplicate": true`. |
| `400` | `invalid '<field>'`, `'<field>' does not match the token`, `body is not valid JSON` | The body failed validation. |
| `401` | `missing or unsupported credentials` | No credential the app accepts. |
| `401` | `invalid token`, `token too old` | OIDC check failed. |
| `401` | `missing or stale timestamp`, `invalid signature` | HMAC check failed. |
| `401` | `replayed credentials` | The token's `jti` or the signature was seen before. |
| `403` | `caller not allowed` | A pinned claim didn't match. The log names the claim. |
| `404` | `unknown app`, `not found` | No such app, or a path other than `/hooks/<name>`. |
| `409` | `deploy already in progress` | Same deploy id is still running. |
| `411` | `Content-Length required` | |
| `413` | `body too large` | Over `max_body_bytes`. |
| `500` | `deploy command failed` | Non-zero exit other than 75. The body adds `exit_code`. |
| `500` | `deploy command could not start` | `command[0]` is missing or not executable. |
| `503` | `cannot fetch signing keys` | GitHub's JWKS was unreachable. The action retries. |
| `504` | `deploy command timed out` | The command ran past the app's `timeout` and was killed. |
