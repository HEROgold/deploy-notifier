# Container and Traefik

If your reverse proxy only routes to containers, as Traefik does with its Docker provider,
a verifier on `127.0.0.1:9000` can't be reached without changing the shared proxy config.
Run the verifier as a container instead.

```text
GitHub Actions --HTTPS--> Traefik --> verifier container --docker.sock--> host daemon
                                        └─ runs the app's command, e.g. docker compose up
```

## Run it

```sh
git clone https://github.com/HEROgold/deploy-notifier ~/deploy-notifier
cd ~/deploy-notifier/verifier
cp config.example.toml config.toml   # listen = "0.0.0.0:9000"; set the app's command
DOCKER_GID=$(getent group docker | cut -d: -f3) PROXY_NETWORK=traefik_network \
  DEPLOY_HOST=deploy.example.com CERT_RESOLVER=myresolver \
  docker compose -f compose.example.yml up -d --build
```

| Variable | Default | Meaning |
|---|---|---|
| `DOCKER_GID` | *required* | The host's `docker` group id, so the container can use the socket. |
| `PROXY_NETWORK` | *required* | The external Docker network Traefik watches. |
| `DEPLOY_HOST` | *required* | The host name Traefik routes `/hooks/` on. |
| `CERT_RESOLVER` | *required* | The Traefik certificate resolver for that host. |
| `DEPLOY_UID` | `1000` | The host user that owns the app directories. The container runs as this user. |
| `DEPLOY_GID` | `1000` | That user's group. |

Inside the container, set `listen = "0.0.0.0:9000"` in `config.toml`. With the default
`127.0.0.1`, the verifier only accepts connections from inside its own container, so
Traefik can't reach it.

## Mount the apps

Mount each app directory at the same path inside the container as on the host. Compose
project names and relative paths then match a manual `docker compose` run on the host.
Every path in `config.toml` must exist inside the container as well, including the
`command` script and any `hmac_secret_file`.

Keep these mounts out of `compose.example.yml`, so `git pull` stays clean. Put them in a
`compose.override.yml` next to it:

```yaml
services:
  verifier:
    volumes:
      - /home/me/myapp:/home/me/myapp
      # registry login for private images; set DOCKER_CONFIG to it in the deploy command
      - /home/me/.docker:/home/me/.docker:ro
```

Then pass both files:

```sh
docker compose -f compose.example.yml -f compose.override.yml up -d --build
```

## Things to know

- The container talks to the host's Docker daemon through `/var/run/docker.sock`. That is
  root-equivalent, as the `docker` group is for the systemd setup. Keep the per-app OIDC
  pins (`repository_id`, `environment` or `ref`) strict.
- Deploy commands get `PATH=/bin:/usr/bin` and no `HOME`. That's why the image installs
  `docker` and the compose plugin under `/usr`. A command that pulls private images must
  set `DOCKER_CONFIG` to a mounted directory with the registry login.
- The state database lives in the named volume `state`, so it survives a rebuild.

## The files

[`verifier/compose.example.yml`](https://github.com/HEROgold/deploy-notifier/blob/main/verifier/compose.example.yml):

```yaml
--8<-- "verifier/compose.example.yml"
```

[`verifier/Dockerfile`](https://github.com/HEROgold/deploy-notifier/blob/main/verifier/Dockerfile):

```dockerfile
--8<-- "verifier/Dockerfile"
```

The container runs the same `deploy_verifier.py` as the systemd setup. The image installs
its one dependency, `pyjwt[crypto]`, with pip instead of running the script through uv.
