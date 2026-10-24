# Getting started

This guide goes from nothing to a green workflow run. It uses the default setup: GitHub
OIDC, the reference verifier under systemd, and Caddy for TLS. If your reverse proxy only
routes to containers, follow [Container and Traefik](examples/container-traefik.md) for
steps 2, 3 and 7, and the rest stays the same.

You need:

- A Linux server with Docker and a public host name, for example `deploy.example.com`.
- A GitHub repository whose workflow pushes an image, for example to `ghcr.io`.
- The app running from a `compose.yaml` on the server, with `image: ${APP_IMAGE}`. See
  [Deploy command for docker compose](examples/deploy-compose.md#what-it-expects).

## 1. Install uv

The systemd unit runs the verifier with `/usr/local/bin/uv`:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
```

## 2. Create the deploy user

```sh
sudo useradd --system --shell /usr/sbin/nologin --groups docker deploy
```

Membership of the `docker` group is effectively root. See [Security](security.md#the-docker-group)
for a narrower option.

## 3. Install the verifier

```sh
sudo git clone https://github.com/HEROgold/deploy-notifier /opt/deploy-notifier
sudo install -d -m 0750 -g deploy /etc/deploy-verifier
sudo install -m 0640 -g deploy /opt/deploy-notifier/verifier/config.example.toml /etc/deploy-verifier/config.toml
sudo install -m 0755 /opt/deploy-notifier/verifier/examples/deploy-compose.sh /usr/local/bin/deploy-myapp
```

## 4. Get the repository IDs

The verifier pins IDs rather than names, because names can be re-registered. Run this
anywhere `gh` is logged in:

```sh
gh api repos/OWNER/REPO --jq '{repository_id: .id, repository_owner_id: .owner.id}'
```

## 5. Write config.toml

Edit `/etc/deploy-verifier/config.toml`. Delete the `[apps.legacy]` example and fill in
`[apps.myapp]`:

```toml
listen = "127.0.0.1:9000"
state_dir = "/var/lib/deploy-verifier"

[apps.myapp]
command = ["/usr/local/bin/deploy-myapp"]
timeout = 600
audience = "https://deploy.example.com"
repository_id = "123456789"
repository_owner_id = "1234567"
environment = "production"
```

The app name `myapp` becomes the URL path `/hooks/myapp`. The `audience` is the origin of
the URL the action will call. `environment` means only jobs that run in the `production`
environment can deploy. Every key is in the [configuration reference](verifier.md#configuration).

## 6. Write the deploy command

Edit `/usr/local/bin/deploy-myapp` and set `project_dir` and `service` at the top. The
script and when to change it are explained in
[Deploy command for docker compose](examples/deploy-compose.md).

## 7. Start the verifier

```sh
sudo cp /opt/deploy-notifier/verifier/deploy-verifier.service /etc/systemd/system/
```

If your app directory isn't under `/srv`, change `ReadWritePaths` in the copied unit first.
Then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now deploy-verifier
journalctl -u deploy-verifier -n 20
```

The log should end with `listening on 127.0.0.1:9000 for apps: myapp`. If the config has a
mistake, the verifier exits and the log names the key.

## 8. Put TLS in front

With Caddy, add to the Caddyfile and reload:

```
deploy.example.com {
    reverse_proxy /hooks/* 127.0.0.1:9000
}
```

For nginx, see [systemd and Caddy](examples/systemd-caddy.md#nginx). Raise
`proxy_read_timeout` above your deploy timeout.

Check that the verifier answers through the proxy:

```sh
curl -si -X POST https://deploy.example.com/hooks/myapp -H 'Content-Type: application/json' -d '{}'
```

You should get `401` with `{"error": "missing or unsupported credentials"}`. That's the
verifier refusing an unauthenticated request, which is correct.

## 9. Create the GitHub environment

In the repository, open Settings, then Environments, and create `production`. Add required
reviewers or a deployment branch rule if you want them. The environment name must match
`environment` in `config.toml`.

## 10. Add the workflow step

```yaml
jobs:
  deploy:
    needs: publish
    runs-on: ubuntu-latest
    environment: production
    concurrency: deploy-production
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: HEROgold/deploy-notifier@v1
        with:
          url: https://deploy.example.com/hooks/myapp
          image: ghcr.io/owner/app@${{ needs.publish.outputs.digest }}
          version: ${{ github.ref_name }}
          timeout: 600
```

Pass the image by digest. The `publish` job must expose the digest that `docker push` or
`docker/build-push-action` returned.

## 11. Check the first run

Push and watch the run. A passing deploy shows a summary line like:

```text
deploy-notifier → `https://deploy.example.com`: **deployed** (HTTP 200, deploy id `456-1-0123456789ab`)
```

On the server, the journal shows the deploy and the command's output:

```sh
journalctl -u deploy-verifier -f
```

If the step fails, the response body and HTTP code tell you which check failed. Look it up
in [Troubleshooting](troubleshooting.md).
