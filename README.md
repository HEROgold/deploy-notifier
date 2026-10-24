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

**Documentation: <https://herogold.github.io/deploy-notifier/>**

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
      - uses: HEROgold/deploy-notifier@v1
        with:
          url: https://deploy.example.com/hooks/myapp
          image: ghcr.io/owner/app@${{ steps.push.outputs.digest }}
          version: ${{ github.ref_name }}
          timeout: 900
```

Pass the image **by digest** (`@sha256:…`), not by tag. Tags are mutable, and the reference
verifier rejects them by default.

## Docs

| Page | Covers |
|---|---|
| [Getting started](https://herogold.github.io/deploy-notifier/getting-started/) | A first deploy, from an empty server to a green run. |
| [Action](https://herogold.github.io/deploy-notifier/action/) | Inputs, outputs, status and retries. |
| [Verifier](https://herogold.github.io/deploy-notifier/verifier/) | Install, config reference, deploy commands, HMAC. |
| [Examples](https://herogold.github.io/deploy-notifier/examples/) | systemd and Caddy, container and Traefik, a docker compose deploy command. |
| [Protocol](https://herogold.github.io/deploy-notifier/protocol/) | The request and response contract for your own receiver. |
| [How it works](https://herogold.github.io/deploy-notifier/how-it-works/) | One deploy step by step: auth, replay, dedupe, timeouts. |
| [Security](https://herogold.github.io/deploy-notifier/security/) | Pinning, the docker group, input validation. |
| [Troubleshooting](https://herogold.github.io/deploy-notifier/troubleshooting/) | Common failures and their fixes. |

The sources are in [`docs/`](docs/). [DESIGN.md](DESIGN.md) records the reasoning behind the
choices.

## Development

```sh
uv run prek install                                           # gitleaks on every commit
uv run --group verifier pytest verifier                       # verifier tests
shellcheck notify.sh                                          # lint the action
uv run --group docs zensical serve                            # preview the docs
```

[`.github/workflows/test.yml`](.github/workflows/test.yml) also runs an end-to-end test on
GitHub. It uses a real OIDC token, the action and a verifier running on the runner.
