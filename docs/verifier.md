# Verifier

The reference verifier is one Python file,
[`verifier/deploy_verifier.py`](https://github.com/HEROgold/deploy-notifier/blob/main/verifier/deploy_verifier.py),
run with [uv](https://docs.astral.sh/uv/). It listens on a local port, checks each
notification, and runs one command per app. It passes the command as argv and never
through a shell.

| Command result | HTTP answer | Action `status` |
|---|---|---|
| exit `0` | `200` | `deployed` |
| exit `75` | `202` | `deferred` |
| any other exit | `500` | `failed` |
| still running after the app's `timeout` | `504` | `failed` |
| could not start | `500` | `failed` |

Each app runs one deploy at a time. A second notification for the same app waits for the
first to finish.

## Install

You can run the verifier in two ways:

- **systemd**, listening on `127.0.0.1:9000` behind Caddy or nginx. This is the default.
  [Example: systemd and Caddy](examples/systemd-caddy.md).
- **A container**, for hosts whose reverse proxy only routes to containers, as Traefik does
  with its Docker provider. [Example: container and Traefik](examples/container-traefik.md).

[Getting started](getting-started.md) walks through the systemd setup from start to finish.

## Configuration

The verifier reads one TOML file, `/etc/deploy-verifier/config.toml` unless you pass
`--config <path>`. Start from
[`verifier/config.example.toml`](https://github.com/HEROgold/deploy-notifier/blob/main/verifier/config.example.toml):

```toml
--8<-- "verifier/config.example.toml"
```

The verifier checks the file at startup and refuses to start if something is wrong. The
error names the app and the key, for example `apps.myapp: pin 'environment' and/or 'ref'
so not every branch can deploy`.

### Global keys

| Key | Type | Default | Description |
|---|---|---|---|
| `listen` | string | `"127.0.0.1:9000"` | `host:port` to listen on. Keep it on localhost and let the reverse proxy handle TLS. In a container, use `"0.0.0.0:9000"`. |
| `state_dir` | string | `"/var/lib/deploy-verifier"` | Directory for `state.sqlite3`, the replay and dedupe database. Created if missing. |
| `max_body_bytes` | integer | `65536` | Larger requests get `413`. |
| `max_token_age` | integer | `600` | Seconds. OIDC tokens issued longer ago get `401 token too old`. |
| `oidc_issuer` | string | `"https://token.actions.githubusercontent.com"` | Change it only for GitHub Enterprise Server. |

### Per-app keys

Each app is a `[apps.<name>]` table. The name must match `[a-z0-9][a-z0-9_-]{0,63}`, and
the action notifies it at `https://<host>/hooks/<name>`. The file needs at least one app.

| Key | Type | Default | Required | Description |
|---|---|---|---|---|
| `command` | list of strings | | yes | Argv to run. `command[0]` must be an absolute path. |
| `timeout` | integer | `600` | no | Seconds before the verifier kills the command and answers `504`. Keep it at or below the action's `timeout` input. |
| `image_pattern` | string (regex) | any digest-pinned ref | no | The `image` field must match it. The default is `^[a-z0-9.\-]+(:[0-9]+)?/[a-z0-9._/\-]+@sha256:[0-9a-f]{64}$`. |
| `repository_id` | string | | for OIDC | Setting it turns on OIDC for the app. The token's `repository_id` must match. |
| `audience` | string | | with OIDC | The token's `aud` must match. Use the notify URL's origin, or the action's `audience` input. |
| `repository_owner_id` | string | | no | The token's `repository_owner_id` must match. |
| `environment` | string | | with OIDC, this or `ref` | The token's `environment` must match. |
| `ref` | string | | with OIDC, this or `environment` | The token's `ref` must match, for example `refs/heads/main`. |
| `hmac_secret_file` | string | | for HMAC | Path to the shared secret. The verifier strips surrounding whitespace, and the rest must be at least 32 bytes. |

An app needs OIDC, HMAC, or both. With both, the verifier uses whichever credential the
request carries.

Get the IDs to pin with:

```sh
gh api repos/OWNER/REPO --jq '{repository_id: .id, repository_owner_id: .owner.id}'
```

## Deploy commands

The verifier runs `command` with a minimal environment. `PATH` is `/bin:/usr/bin`, `LANG`
is `C.UTF-8`, and there is no `HOME`. It adds these variables:

| Variable | Value |
|---|---|
| `DEPLOY_APP` | The app name from the URL. |
| `DEPLOY_ID` | The deploy id, `<run_id>-<run_attempt>-<url hash>`. |
| `DEPLOY_IMAGE` | The image ref, already checked against `image_pattern`. Empty if none was sent. |
| `DEPLOY_VERSION` | The version, matching `[\w.+-]{1,128}`. Empty if none was sent. |
| `DEPLOY_REPOSITORY` | `owner/repo`. |
| `DEPLOY_SHA` | The commit SHA. |
| `DEPLOY_REF` | The git ref, for example `refs/heads/main`. |
| `DEPLOY_ACTOR` | The user who triggered the run. |
| `DEPLOY_AUTH` | `oidc` or `hmac`. |

In OIDC mode, the repository, SHA, ref and actor come from the signed token, not from the
request body.

Stdin holds the same values as one JSON object, plus `extra`:

```json
{"app": "myapp", "deploy_id": "456-1-0123456789ab", "image": "ghcr.io/owner/app@sha256:…",
 "version": "1.2.3", "repository": "owner/app", "sha": "…", "ref": "refs/heads/main",
 "actor": "octocat", "auth": "oidc", "extra": {}}
```

`extra` is not validated. Treat it as untrusted input.

Exit `0` when the new version is running and `75` when you accepted the notification but
chose not to deploy now, for example outside a maintenance window. The workflow step passes
in both cases, with `status` set to `deferred` for `75`. Any other exit fails the step.

The verifier logs the last 4000 characters of the command's output and never sends it to
the client. [Example: deploy command for docker compose](examples/deploy-compose.md)
walks through a complete script.

## HMAC

Use HMAC when the job can't request an OIDC token, for example in workflows triggered by a
pull request from a fork, or when your own receiver can't verify JWTs. OIDC is better where it works:
there's no secret to store or rotate, and the verifier pins the repository, environment and
ref from a token GitHub signed. HMAC only proves the caller knows the secret.

1. Generate a secret on the server and store it next to the config:

    ```sh
    openssl rand -hex 32 | sudo tee /etc/deploy-verifier/myapp.secret >/dev/null
    sudo chown root:deploy /etc/deploy-verifier/myapp.secret
    sudo chmod 0640 /etc/deploy-verifier/myapp.secret
    ```

2. Point the app at it:

    ```toml
    [apps.myapp]
    command = ["/usr/local/bin/deploy-myapp"]
    hmac_secret_file = "/etc/deploy-verifier/myapp.secret"
    ```

3. In GitHub, add the same value as an environment secret, for example `DEPLOY_HMAC_SECRET`
   in the `production` environment. Paste it without a trailing newline: the action signs
   with the secret exactly as stored, while the verifier strips whitespace from the file.

4. Use it in the workflow:

    ```yaml
    - uses: HEROgold/deploy-notifier@v1
      with:
        url: https://deploy.example.com/hooks/myapp
        auth: hmac
        hmac_secret: ${{ secrets.DEPLOY_HMAC_SECRET }}
    ```

The verifier rejects a timestamp more than 5 minutes off its own clock, so keep the server's
clock synced.

## Logs

With systemd, the verifier logs to the journal:

```sh
journalctl -u deploy-verifier -f
```

In a container, use `docker compose logs -f verifier`. Rejected requests log the reason,
for example `rejected myapp: 403 caller not allowed environment='staging'`. The client only
gets the short message, `caller not allowed`.
