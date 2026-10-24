# systemd and Caddy

The verifier runs as a systemd service under a dedicated `deploy` user and listens on
`127.0.0.1:9000`. Caddy terminates TLS and forwards `/hooks/*` to it.
[Getting started](../getting-started.md) walks through these steps in order.

## Install

```sh
sudo useradd --system --shell /usr/sbin/nologin --groups docker deploy
sudo git clone https://github.com/HEROgold/deploy-notifier /opt/deploy-notifier
sudo install -d -m 0750 -g deploy /etc/deploy-verifier
sudo install -m 0640 -g deploy /opt/deploy-notifier/verifier/config.example.toml /etc/deploy-verifier/config.toml
sudo install -m 0755 /opt/deploy-notifier/verifier/examples/deploy-compose.sh /usr/local/bin/deploy-myapp
# edit config.toml and deploy-myapp, then:
sudo cp /opt/deploy-notifier/verifier/deploy-verifier.service /etc/systemd/system/
sudo systemctl enable --now deploy-verifier
```

The unit runs `/usr/local/bin/uv`. If uv lives somewhere else, change `ExecStart`.

## The unit file

[`verifier/deploy-verifier.service`](https://github.com/HEROgold/deploy-notifier/blob/main/verifier/deploy-verifier.service):

```ini
--8<-- "verifier/deploy-verifier.service"
```

`ProtectSystem=strict` makes the whole file system read-only for the verifier and its
commands, apart from `StateDirectory` and `ReadWritePaths`. Add every directory your deploy
commands write to under `ReadWritePaths`. `ProtectHome=yes` hides `/home`, so keep app
directories under `/srv` or another path outside it.

## Caddy

```
deploy.example.com {
    reverse_proxy /hooks/* 127.0.0.1:9000
}
```

Caddy sets no read timeout on proxied requests by default, so long deploys work as they are.

## nginx

```nginx
location /hooks/ {
    proxy_pass http://127.0.0.1:9000;
    proxy_read_timeout 900s;  # above the longest app timeout
}
```

nginx closes a proxied request after 60 seconds by default. Set `proxy_read_timeout` above
your longest deploy, or the action gets a `504` while the deploy keeps running.
