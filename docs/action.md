# Action

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: production        # pinned by the server; gives you protection rules
    concurrency: deploy-production # one deploy at a time
    permissions:
      contents: read
      id-token: write              # required for auth: oidc
    steps:
      # ... build and push ghcr.io/owner/app, exposing the digest as steps.push.outputs.digest
      - uses: HEROgold/deploy-notifier@v1
        with:
          url: https://deploy.example.com/hooks/myapp
          image: ghcr.io/owner/app@${{ steps.push.outputs.digest }}
          version: ${{ github.ref_name }}
          timeout: 900
```

Pass the image by digest (`@sha256:…`), not by tag. Tags are mutable, and the reference
verifier rejects them by default.

## Inputs

| Input | Default | Description |
|---|---|---|
| `url` | *required* | Endpoint to notify. Must be `https://` unless `allow_insecure` is set. Credentials in the URL (`user@host`) are refused. |
| `auth` | `oidc` | `oidc` or `hmac`. `oidc` needs `permissions: id-token: write` on the job. |
| `audience` | origin of `url` | OIDC `aud` claim the server expects, for example `https://deploy.example.com`. |
| `hmac_secret` | | Required for `auth: hmac`. Use an environment secret of 32 or more random bytes. See [HMAC](verifier.md#hmac). |
| `image` | | Image reference, ideally digest-pinned. Sent as `null` when empty. |
| `version` | | Free-form version string. Sent as `null` when empty. |
| `extra` | `{}` | JSON object forwarded under `extra`. Anything other than an object fails the step. |
| `timeout` | `600` | Seconds to wait for the server to finish. Must be a positive integer. |
| `allow_insecure` | `false` | Allow `http://` targets. Only for local testing. |

## Outputs

| Output | Description |
|---|---|
| `status` | `deployed`, `deferred` or `failed`. |
| `http_code` | The server's HTTP status, or `000` if there was no response. |
| `response` | The response body, at most 4 KB. |

The step also writes one line to the job summary with the origin, status, HTTP code and
deploy id.

## Status and exit

| Server answer | `status` | Step |
|---|---|---|
| `202` | `deferred` | passes |
| any other `2xx` | `deployed` | passes |
| anything else | `failed` | fails |
| no answer within `timeout` | `failed` | fails |

## Retries

The action makes at most 3 attempts. It retries only when the request never reached the
deploy logic:

- a DNS, connect or TLS-handshake failure (curl exit 6, 7 or 35)
- HTTP 502 or 503

It waits 5 seconds before the second attempt and 10 seconds before the third. A timeout or a
504 is not retried, because the deploy may still be running. The connect timeout is fixed
at 10 seconds.

Each attempt gets a new OIDC token or HMAC signature, so retries pass the server's replay
check. The deploy id stays the same, so a retry never deploys twice. See
[How it works](how-it-works.md#retries-and-dedupe).

## Server output

The action prints the server's response between `::stop-commands::` markers. A compromised
server can't inject `::set-output`-style workflow commands into your run.

## Several targets

One step notifies one URL. To deploy to several servers, use a matrix:

```yaml
strategy:
  matrix:
    url:
      - https://eu.example.com/hooks/myapp
      - https://us.example.com/hooks/myapp
steps:
  - uses: HEROgold/deploy-notifier@v1
    with:
      url: ${{ matrix.url }}
```

Each URL gets its own deploy id, because the id includes a hash of the URL.
