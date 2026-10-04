#!/usr/bin/env bash
# Example deploy command: pin a docker compose service to the notified image digest.
# compose.yaml uses `image: ${APP_IMAGE}`; the verifier has already validated DEPLOY_IMAGE.
# Exit 0 = deployed, 75 = deferred (e.g. outside a maintenance window), anything else = failed.
set -euo pipefail

project_dir=/srv/myapp
service=app

cd "$project_dir"
[[ -n "${DEPLOY_IMAGE:-}" ]] || { echo "no image in notification" >&2; exit 1; }

docker pull "$DEPLOY_IMAGE"

[[ -f .env.deploy ]] && cp .env.deploy .env.deploy.prev
printf 'APP_IMAGE=%s\n' "$DEPLOY_IMAGE" >.env.deploy.tmp
mv .env.deploy.tmp .env.deploy

if ! docker compose --env-file .env.deploy up -d --wait "$service"; then
  echo "deploy of $DEPLOY_IMAGE failed, rolling back" >&2
  if [[ -f .env.deploy.prev ]]; then
    mv .env.deploy.prev .env.deploy
    docker compose --env-file .env.deploy up -d --wait "$service" || true
  fi
  exit 1
fi
echo "deployed $DEPLOY_IMAGE ($DEPLOY_REPOSITORY@$DEPLOY_SHA)"
