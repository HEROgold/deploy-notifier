# deploy-notifier

A GitHub Action that tells your server "a new version is ready" and waits for the server to
say it's done. The server decides what deploying means (pull an image, `docker compose up`,
…). The action only sends an authenticated notification:

- **HTTPS only.** Certificates are always verified and redirects are never followed.
- **No long-lived secrets by default.** Requests are authenticated with a short-lived
  GitHub OIDC token that the server verifies against GitHub's keys. HMAC is available as a
  fallback.
- **Replay-proof and idempotent.** The server rejects reused tokens or signatures, and it
  dedupes retries on a deploy id.
- **Synchronous.** The step passes only when the server answers `2xx`.

The repo also contains a small **reference verifier** ([`verifier/`](verifier/)). You can
run it on the server as-is or use it as the spec for your own receiver.

## Usage

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
      - uses: OWNER/deploy-notifier@v1
        with:
          url: https://deploy.example.com/hooks/myapp
          image: ghcr.io/owner/app@${{ steps.push.outputs.digest }}
          version: ${{ github.ref_name }}
          timeout: 900
```

Pass the image **by digest** (`@sha256:…`), not by tag. Tags are mutable, and the reference
verifier rejects them by default.

### Inputs

| Input | Default | Description |
|---|---|---|
| `url` | *required* | Endpoint to notify. Must be `https://` unless `allow_insecure`. |
| `auth` | `oidc` | `oidc` or `hmac`. |
| `audience` | origin of `url` | OIDC `aud` claim the server expects. |
| `hmac_secret` | | Required for `auth: hmac`. Use an environment secret, 32+ random bytes. |
| `image` | | Image reference, ideally digest-pinned. |
| `version` | | Free-form version string. |
| `extra` | `{}` | Extra JSON object forwarded under `extra`. |
| `timeout` | `600` | Seconds to wait for the server to finish. |
| `allow_insecure` | `false` | Allow `http://` (local testing only). |

### Outputs

| Output | Description |
|---|---|
| `status` | `deployed`, `deferred` or `failed` |
| `http_code` | Server's HTTP status (`000` if there was no response) |
| `response` | Response body, at most 4 KB |

### Behaviour

- `2xx` → `deployed`, `202` → `deferred`. Both pass. Anything else fails the step.
- **Retries:** at most 3 attempts, and only when the request never reached the server
  (DNS, connect or TLS-handshake failure, HTTP 502/503). A timeout or 504 is **not**
  retried, because the deploy may still be running.
- The server's response is printed with workflow commands disabled, so a compromised server
  can't inject `::set-output`-style commands into your run.

## Protocol

This is the contract for writing your own receiver.

`POST <url>` with `Content-Type: application/json`:

```json
{
  "image": "ghcr.io/owner/app@sha256:…",  "version": "1.2.3",  "extra": {},
  "repository": "owner/app", "repository_id": "123", "sha": "…", "ref": "refs/heads/main",
  "run_id": "456", "run_attempt": "1", "actor": "octocat",
  "deploy_id": "456-1-0123456789ab", "sent_at": "2026-10-04T12:00:00Z"
}
```

`image` and `version` may be `null`. Headers:

| Header | When |
|---|---|
| `X-Deploy-Id` | Always. `<run_id>-<run_attempt>-<sha256(url)[:12]>`. Dedupe on it. |
| `Authorization: Bearer <jwt>` | `auth: oidc`. Verify it as a GitHub Actions OIDC token: RS256, `iss` = `https://token.actions.githubusercontent.com`, `aud`, `exp`. Then pin `repository_id`/`environment`/`ref`, reject reused `jti`, and check that the body's `repository_id`, `sha`, `run_id` and `run_attempt` match the token. |
| `X-Timestamp`, `X-Signature-256: sha256=<hex>` | `auth: hmac`. HMAC-SHA256 over `"<timestamp>.<raw body>"`. Reject a timestamp more than 5 min off, and reject reused signatures. |

The receiver responds **only when done**: `200` deployed, `202` accepted but deferred, any
other code fails the workflow. A new credential (token or signature) is created for each
attempt, so retries pass replay checks. The deploy id stays the same, so retries don't
deploy twice.

## Server setup (reference verifier)

The verifier is one Python file run with [uv](https://docs.astral.sh/uv/). It listens on
localhost, verifies requests and runs one command per app (argv only, never a shell). Exit
`0` → 200, exit `75` → 202, any other exit or a timeout → 500/504. The command receives
`DEPLOY_APP`, `DEPLOY_ID`, `DEPLOY_IMAGE`, `DEPLOY_VERSION`, `DEPLOY_REPOSITORY`,
`DEPLOY_SHA`, `DEPLOY_REF`, `DEPLOY_ACTOR` and `DEPLOY_AUTH`, plus the full context as JSON
on stdin.

```sh
sudo useradd --system --shell /usr/sbin/nologin --groups docker deploy
sudo git clone https://github.com/OWNER/deploy-notifier /opt/deploy-notifier
sudo install -d -m 0750 -g deploy /etc/deploy-verifier
sudo install -m 0640 -g deploy /opt/deploy-notifier/verifier/config.example.toml /etc/deploy-verifier/config.toml
sudo install -m 0755 /opt/deploy-notifier/verifier/examples/deploy-compose.sh /usr/local/bin/deploy-myapp
# edit config.toml and deploy-myapp, then:
sudo cp /opt/deploy-notifier/verifier/deploy-verifier.service /etc/systemd/system/
sudo systemctl enable --now deploy-verifier
```

Get the IDs to pin with
`gh api repos/OWNER/REPO --jq '{repository_id: .id, repository_owner_id: .owner.id}'`.

Put TLS in front of it, for example with Caddy:

```
deploy.example.com {
    reverse_proxy /hooks/* 127.0.0.1:9000
}
```

With nginx, raise `proxy_read_timeout` above your deploy timeout (the default is 60 s).

### Security notes

- **Pin IDs, not names.** `repository_id` and `repository_owner_id` survive renames, and
  someone else can't register the same name later. Pin an `environment` with required
  reviewers or branch rules, or at least a `ref`. The verifier refuses to start without
  one of them.
- **`docker` group = root.** If that's too much, drop the group and give `deploy` a single
  `sudo` rule for the deploy script.
- The command only receives validated values. Image refs must match `image_pattern`
  (digest-pinned by default) and versions match `[\w.+-]{1,128}`. In OIDC mode,
  repository, sha and ref come from the signed token, not the body.
- Failed deploys return only the exit code. Command output goes to the journal
  (`journalctl -u deploy-verifier`).

## Development

```sh
uv run --with pytest --with "pyjwt[crypto]" pytest verifier   # verifier tests
shellcheck notify.sh                                          # lint the action
```

[`.github/workflows/test.yml`](.github/workflows/test.yml) also runs an end-to-end test on
GitHub. It uses a real OIDC token, the action and a verifier running on the runner. See
[DESIGN.md](DESIGN.md) for the reasoning behind the choices.
