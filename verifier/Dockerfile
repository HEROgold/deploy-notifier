# syntax=docker/dockerfile:1
# The reference verifier as a container, for hosts whose reverse proxy only
# routes to containers (e.g. Traefik with the Docker provider).
#
# Deploy commands run with PATH=/bin:/usr/bin and no HOME, so the docker CLI
# and compose plugin are installed under /usr. A deploy command that pulls
# private images must set DOCKER_CONFIG itself.

FROM docker:29-cli AS docker-cli

FROM python:3.13-slim

RUN pip install --no-cache-dir "pyjwt[crypto]>=2.8"

COPY --from=docker-cli /usr/local/bin/docker /usr/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/libexec/docker/cli-plugins/docker-compose

COPY deploy_verifier.py /app/deploy_verifier.py

ENV PYTHONUNBUFFERED=1
EXPOSE 9000
ENTRYPOINT ["python", "/app/deploy_verifier.py"]
CMD ["--config", "/etc/deploy-verifier/config.toml"]
