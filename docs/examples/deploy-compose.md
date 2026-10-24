# Deploy command for docker compose

[`verifier/examples/deploy-compose.sh`](https://github.com/HEROgold/deploy-notifier/blob/main/verifier/examples/deploy-compose.sh)
pins one compose service to the notified image digest, and rolls back if the new container
doesn't come up healthy.

```bash
--8<-- "verifier/examples/deploy-compose.sh"
```

## What it expects

The app's `compose.yaml` takes its image from a variable:

```yaml
services:
  app:
    image: ${APP_IMAGE}
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/health"]
```

`docker compose up --wait` waits for the healthcheck. Without one, it only waits for the
container to start.

## Step by step

1. It changes to `project_dir`. Set `project_dir` and `service` at the top for your app.
   Under systemd, the directory must be in the unit's `ReadWritePaths`.
2. It exits 1 if the notification carried no image. The verifier has already checked
   `DEPLOY_IMAGE` against `image_pattern`, so the script doesn't validate it again.
3. `docker pull` fetches the image before anything changes. If the pull fails, the running
   version stays.
4. It saves the current `.env.deploy` as `.env.deploy.prev`, then writes the new
   `APP_IMAGE=` line to a temporary file and moves it into place. A crash halfway never
   leaves a broken `.env.deploy`.
5. `docker compose --env-file .env.deploy up -d --wait` replaces the container.
6. If that fails, it restores `.env.deploy.prev`, starts the old version again, and exits
   1. The workflow step fails, and the old version keeps running.
7. On success it prints the image, repository and SHA to the log and exits 0.

Install it as the app's command:

```sh
sudo install -m 0755 /opt/deploy-notifier/verifier/examples/deploy-compose.sh /usr/local/bin/deploy-myapp
```

```toml
[apps.myapp]
command = ["/usr/local/bin/deploy-myapp"]
timeout = 600
```

Set the app's `timeout` above the time a pull plus a healthcheck takes. Set the action's
`timeout` input at or above the app's `timeout`.

## When to exit 75

Exit 75 means "notification accepted, not deploying now". The workflow step passes with
`status: deferred`. Use it when skipping the deploy is the correct outcome, not an error:

- Outside a maintenance window. Record the image and let a timer deploy it later.
- When the server already runs a newer version than the one notified.
- When an operator froze deploys, for example with a marker file:

  ```bash
  if [[ -e /srv/myapp/FREEZE ]]; then
    echo "deploys frozen, not deploying $DEPLOY_IMAGE" >&2
    exit 75
  fi
  ```

If the deploy should have happened and didn't, exit with anything other than 0 or 75. The
workflow then fails and someone looks at it.
