# deploy-notifier

A GitHub Action that tells your server "a new version is ready" and waits for the server to
say it's done. The server decides what deploying means, for example pulling an image and
running `docker compose up`. The action only sends an authenticated notification.

- **HTTPS only.** The action always verifies certificates and never follows redirects.
- **No long-lived secrets by default.** The action authenticates with a short-lived GitHub
  OIDC token, and the server checks it against GitHub's keys. HMAC is available as a
  fallback.
- **Replay-proof and idempotent.** The server rejects reused tokens and signatures, and it
  dedupes retries on a deploy id.
- **Synchronous.** The step passes only when the server answers `2xx`.

The repo also holds a small reference verifier in
[`verifier/`](https://github.com/HEROgold/deploy-notifier/tree/main/verifier). Run it on
your server as-is, or treat it as the spec for your own receiver.

```yaml
- uses: HEROgold/deploy-notifier@v1
  with:
    url: https://deploy.example.com/hooks/myapp
    image: ghcr.io/owner/app@${{ steps.push.outputs.digest }}
```

## Where to go next

| Page | Read it when |
|---|---|
| [Getting started](getting-started.md) | You are setting up your first deploy. |
| [Action](action.md) | You need the inputs, outputs, or retry behaviour. |
| [Verifier](verifier.md) | You are installing or configuring the server side. |
| [Examples](examples/index.md) | You want complete setups to copy. |
| [Protocol](protocol.md) | You are writing your own receiver. |
| [How it works](how-it-works.md) | You want to know what happens during one deploy. |
| [Security](security.md) | You are deciding what to pin and how much access to give. |
| [Troubleshooting](troubleshooting.md) | A deploy fails and you need to know why. |
| [Development](development.md) | You are changing this repo, or a CI check failed. |
