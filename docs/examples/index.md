# Examples

Complete setups you can copy. Each page includes the files straight from the repo, so what
you read here is what `git clone` gives you.

| Example | Use it when |
|---|---|
| [systemd and Caddy](systemd-caddy.md) | The verifier runs on the host and a reverse proxy on the host forwards to `127.0.0.1:9000`. |
| [Container and Traefik](container-traefik.md) | Your reverse proxy only routes to containers, as Traefik does with its Docker provider. |
| [Deploy command for docker compose](deploy-compose.md) | You need a deploy command that pins a compose service to the notified image. |

All three use the same `config.toml` keys. See the
[configuration reference](../verifier.md#configuration).
