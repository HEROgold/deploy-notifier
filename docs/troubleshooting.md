# Troubleshooting

Start with the step's response body and HTTP code. For the full reason, read the verifier's
log on the server:

```sh
journalctl -u deploy-verifier -n 50          # systemd
docker compose logs --tail 50 verifier      # container
```

A rejected request logs a line like `rejected myapp: 403 caller not allowed environment='staging'`.

## The step fails before sending

**"no OIDC token available: add 'permissions: id-token: write' to the job"**

The job can't request an OIDC token. Add the permission to the job, not only the workflow:

```yaml
permissions:
  contents: read
  id-token: write
```

Pull requests from forks never get `id-token: write`. Deploy from a push or a release
instead.

**"refusing plain http:// target"**

The `url` starts with `http://`. Use `https://`. `allow_insecure: true` exists for local
testing only.

## 401 invalid token

Usually an audience mismatch. The log shows `Audience doesn't match` or `Invalid audience`.
The token's `aud` is the origin of `url` (`https://deploy.example.com`, no path) unless you
set the `audience` input. The app's `audience` in `config.toml` must be the same string.
Look for a trailing slash, a different port, or `http` versus `https`.

## 401 token too old

The token was issued more than `max_token_age` seconds before it reached the verifier. The
action requests a new token for each attempt, so this points at a server clock that's off,
or a proxy that held the request. Check the server's clock with `timedatectl`.

## 401 missing or stale timestamp

HMAC only. The server's clock and GitHub's differ by more than 5 minutes. Sync the server's
clock.

## 401 invalid signature

HMAC only. The secret in GitHub and the secret file differ. The verifier strips whitespace
from the file, but the action signs with the GitHub secret exactly as stored. Check that the
GitHub secret has no trailing newline.

## 403 caller not allowed

A pinned claim didn't match. The log names it:

- `repository_id=…`: the workflow runs in a different repository than pinned. Get the ID
  again with `gh api repos/OWNER/REPO --jq .id`.
- `environment=None`: the job has no `environment:`. Add it, and use the same name as in
  `config.toml`.
- `environment='staging'`: the job runs in another environment.
- `ref='refs/heads/feature'`: the app pins `ref` and the run is on another branch.

## 400 invalid 'image'

The image doesn't match `image_pattern`. The default accepts only digest-pinned refs like
`ghcr.io/owner/app@sha256:<64 hex>`. A tag such as `:latest` or `:1.2.3` is rejected. Pass
the digest from the push step. If you set your own `image_pattern`, check it against the
exact string the workflow sends. The image ref must be lower-case.

## 504, or "no response within Ns"

Something stopped waiting before the deploy finished.

- `504` with `deploy command timed out`: the command ran past the app's `timeout`. Raise
  it, or speed up the command.
- `504` without a JSON body: the reverse proxy timed out. nginx defaults to 60 seconds.
  Raise `proxy_read_timeout` above the app's `timeout`.
- "no response within Ns": the action's `timeout` input ran out. Keep it at or above the
  app's `timeout`.

The action doesn't retry any of these. The deploy may have finished anyway, so check the
log.

## 500 deploy command failed

The command exited with a code other than 0 or 75. The body has `exit_code`, and the log
has the last 4000 characters of its output. Common causes:

- The command can't find a program. It runs with `PATH=/bin:/usr/bin`, so tools in
  `/usr/local/bin` need a full path.
- Under systemd, it can't write the app directory. Add the directory to `ReadWritePaths`.
- It can't pull a private image. Without `HOME`, Docker can't find the registry login.
  Set `DOCKER_CONFIG` in the command.

## 500 deploy command could not start

`command[0]` doesn't exist or isn't executable for the verifier's user. In a container, the
path must exist inside the container. Mount the script.

## 409 deploy already in progress

A deploy with the same deploy id is still running. That happens when the proxy answered an
earlier attempt with `502` or `503` but the request still reached the verifier, so the
action retried while the command was running. Wait for it to finish and read the log. If it
keeps happening, check why the proxy returns `502` or `503` for requests it forwards.

## The verifier won't start

The config check failed. The log names the app and the key:

| Message | Fix |
|---|---|
| `pin 'environment' and/or 'ref' so not every branch can deploy` | Add `environment` or `ref` to the OIDC app. |
| `'audience' is required for OIDC` | Add `audience`, the notify URL's origin. |
| `configure OIDC ('repository_id', ...) and/or 'hmac_secret_file'` | The app has no way to authenticate. Add one. |
| `'command[0]' must be an absolute path` | Use `/usr/local/bin/deploy-myapp`, not `deploy-myapp`. |
| `HMAC secret must be at least 32 bytes` | Generate a new one with `openssl rand -hex 32`. |
| `name must match [a-z0-9][a-z0-9_-]*` | Rename the `[apps.<name>]` table to lower-case letters, digits, `-` and `_`. |
| `config defines no [apps.<name>] sections` | Add at least one app. |
