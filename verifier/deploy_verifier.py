#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyjwt[crypto]>=2.8"]
# ///
"""Reference receiver for deploy-notifier.

Listens on localhost (put Caddy/nginx in front for TLS), verifies each notification
(GitHub OIDC or HMAC), checks it against a per-app allowlist and then runs that app's
deploy command. Exit 0 -> 200 deployed, exit 75 -> 202 deferred, timeout -> 504,
anything else -> 500.

    uv run --script deploy_verifier.py --config /etc/deploy-verifier/config.toml
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

import jwt

log = logging.getLogger("deploy-verifier")

GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
DEFERRED_EXIT_CODE = 75
HMAC_MAX_SKEW = 300
DEDUPE_RETENTION = 7 * 24 * 3600
DEFAULT_IMAGE_PATTERN = r"^[a-z0-9.\-]+(:[0-9]+)?/[a-z0-9._/\-]+@sha256:[0-9a-f]{64}$"

APP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEPLOY_ID_RE = re.compile(r"^[0-9]+-[0-9]+-[0-9a-f]{12}$")
VERSION_RE = re.compile(r"^[\w.+\-]{1,128}$")
SHA_RE = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")
REF_RE = re.compile(r"^refs/[\w.\-/]{1,240}$")
REPOSITORY_RE = re.compile(r"^[\w.\-]{1,100}/[\w.\-]{1,100}$")
NUMERIC_RE = re.compile(r"^[0-9]{1,20}$")
ACTOR_RE = re.compile(r"^[\w.\-\[\]]{0,100}$")


class Reject(Exception):
    def __init__(self, code: int, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail  # logged, never sent to the client


@dataclass(frozen=True)
class App:
    name: str
    command: tuple[str, ...]
    timeout: int
    image_pattern: re.Pattern[str]
    audience: str | None
    repository_id: str | None
    repository_owner_id: str | None
    environment: str | None
    ref: str | None
    hmac_secret: bytes | None

    @property
    def oidc_enabled(self) -> bool:
        return self.repository_id is not None


@dataclass(frozen=True)
class Config:
    listen_host: str
    listen_port: int
    state_dir: Path
    max_body_bytes: int
    max_token_age: int
    oidc_issuer: str
    apps: dict[str, App]


@dataclass(frozen=True)
class Result:
    code: int
    body: dict[str, Any]


def load_config(path: Path) -> Config:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    host, _, port = str(raw.get("listen", "127.0.0.1:9000")).rpartition(":")
    apps = {name: _load_app(name, spec) for name, spec in raw.get("apps", {}).items()}
    if not apps:
        raise ValueError("config defines no [apps.<name>] sections")
    return Config(
        listen_host=host or "127.0.0.1",
        listen_port=int(port),
        state_dir=Path(raw.get("state_dir", "/var/lib/deploy-verifier")),
        max_body_bytes=int(raw.get("max_body_bytes", 65536)),
        max_token_age=int(raw.get("max_token_age", 600)),
        oidc_issuer=str(raw.get("oidc_issuer", GITHUB_ISSUER)),
        apps=apps,
    )


def _load_app(name: str, spec: Mapping[str, Any]) -> App:
    def fail(msg: str) -> ValueError:
        return ValueError(f"apps.{name}: {msg}")

    if not APP_NAME_RE.match(name):
        raise fail("name must match [a-z0-9][a-z0-9_-]*")
    command = spec.get("command")
    if not (
        isinstance(command, list)
        and command
        and all(isinstance(c, str) for c in command)
    ):
        raise fail("'command' must be a non-empty list of strings (argv, no shell)")
    if not (command[0].startswith("/") or Path(command[0]).is_absolute()):
        raise fail("'command[0]' must be an absolute path")

    def opt(key: str) -> str | None:
        value = spec.get(key)
        return None if value is None else str(value)

    secret = None
    if secret_file := spec.get("hmac_secret_file"):
        secret = Path(secret_file).read_text(encoding="utf-8").strip().encode()
        if len(secret) < 32:
            raise fail("HMAC secret must be at least 32 bytes")

    app = App(
        name=name,
        command=tuple(command),
        timeout=int(spec.get("timeout", 600)),
        image_pattern=re.compile(spec.get("image_pattern", DEFAULT_IMAGE_PATTERN)),
        audience=opt("audience"),
        repository_id=opt("repository_id"),
        repository_owner_id=opt("repository_owner_id"),
        environment=opt("environment"),
        ref=opt("ref"),
        hmac_secret=secret,
    )
    if app.oidc_enabled:
        if not app.audience:
            raise fail("'audience' is required for OIDC (the notify URL's origin)")
        if not (app.environment or app.ref):
            raise fail("pin 'environment' and/or 'ref' so not every branch can deploy")
    if not app.oidc_enabled and app.hmac_secret is None:
        raise fail("configure OIDC ('repository_id', ...) and/or 'hmac_secret_file'")
    return app


@dataclass(frozen=True)
class Identity:
    mode: str  # "oidc" | "hmac"
    claims: Mapping[str, Any]
    replay_key: str
    replay_expires: float


class Verifier:
    def __init__(
        self,
        apps: Mapping[str, App],
        state_path: Path | str,
        *,
        issuer: str = GITHUB_ISSUER,
        max_token_age: int = 600,
        jwk_client: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.apps = dict(apps)
        self.issuer = issuer
        self.max_token_age = max_token_age
        self.clock = clock
        self.jwk_client = jwk_client or jwt.PyJWKClient(
            f"{issuer}/.well-known/jwks", lifespan=3600
        )
        self._db_lock = threading.Lock()
        self._app_locks = {name: threading.Lock() for name in self.apps}
        self._db = sqlite3.connect(
            str(state_path), check_same_thread=False, isolation_level=None
        )
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS deploys (
                app TEXT NOT NULL, deploy_id TEXT NOT NULL, status TEXT NOT NULL,
                code INTEGER NOT NULL, updated REAL NOT NULL, PRIMARY KEY (app, deploy_id));
            -- a deploy that was running when we died did not finish
            UPDATE deploys SET status = 'failed', code = 500 WHERE status = 'running';
            """
        )

    # -- entry point -------------------------------------------------------

    def handle(self, app_name: str, headers: Mapping[str, str], body: bytes) -> Result:
        """`headers` must have lower-cased keys."""
        app = self.apps.get(app_name)
        if app is None:
            return Result(404, {"error": "unknown app"})
        try:
            identity = self._authenticate(app, headers, body)
            ctx = self._bind(app, identity, body)
            self._prune()
            self._record_once(identity.replay_key, identity.replay_expires)
        except Reject as r:
            log.warning("rejected %s: %s %s %s", app.name, r.code, r.message, r.detail)
            return Result(r.code, {"error": r.message})
        return self._deploy(app, ctx)

    # -- authentication ----------------------------------------------------

    def _authenticate(
        self, app: App, headers: Mapping[str, str], body: bytes
    ) -> Identity:
        authz = headers.get("authorization", "")
        if authz.startswith("Bearer ") and app.oidc_enabled:
            return self._authenticate_oidc(app, authz[len("Bearer ") :].strip())
        if "x-signature-256" in headers and app.hmac_secret is not None:
            return self._authenticate_hmac(app, headers, body)
        raise Reject(401, "missing or unsupported credentials")

    def _authenticate_oidc(self, app: App, token: str) -> Identity:
        try:
            key = self.jwk_client.get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientConnectionError as e:
            raise Reject(503, "cannot fetch signing keys", str(e)) from e
        except jwt.PyJWTError as e:
            raise Reject(401, "invalid token", str(e)) from e
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=app.audience,
                issuer=self.issuer,
                leeway=30,
                options={
                    "require": ["exp", "iat", "jti", "aud", "iss", "repository_id"]
                },
            )
        except jwt.PyJWTError as e:
            raise Reject(401, "invalid token", str(e)) from e

        now = self.clock()
        if now - float(claims["iat"]) > self.max_token_age:
            raise Reject(401, "token too old")
        expected = {
            "repository_id": app.repository_id,
            "repository_owner_id": app.repository_owner_id,
            "environment": app.environment,
            "ref": app.ref,
        }
        for claim, want in expected.items():
            if want is not None and str(claims.get(claim)) != want:
                raise Reject(
                    403, "caller not allowed", f"{claim}={claims.get(claim)!r}"
                )
        return Identity(
            "oidc", claims, f"jti:{claims['jti']}", float(claims["exp"]) + 60
        )

    def _authenticate_hmac(
        self, app: App, headers: Mapping[str, str], body: bytes
    ) -> Identity:
        assert app.hmac_secret is not None
        ts = headers.get("x-timestamp", "")
        if not NUMERIC_RE.match(ts) or abs(self.clock() - int(ts)) > HMAC_MAX_SKEW:
            raise Reject(401, "missing or stale timestamp")
        expected = (
            "sha256="
            + hmac.new(
                app.hmac_secret, ts.encode() + b"." + body, hashlib.sha256
            ).hexdigest()
        )
        signature = headers.get("x-signature-256", "")
        if not hmac.compare_digest(expected.encode(), signature.encode()):
            raise Reject(401, "invalid signature")
        return Identity("hmac", {}, f"sig:{signature}", int(ts) + HMAC_MAX_SKEW + 60)

    # -- payload -----------------------------------------------------------

    def _bind(self, app: App, identity: Identity, body: bytes) -> dict[str, Any]:
        """Validate the payload and return the trusted deploy context."""
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as e:
            raise Reject(400, "body is not valid JSON") from e
        if not isinstance(payload, dict):
            raise Reject(400, "body must be a JSON object")

        def field(
            name: str, pattern: re.Pattern[str], *, optional: bool = False
        ) -> str | None:
            value = payload.get(name)
            if value is None and optional:
                return None
            if not isinstance(value, str) or not pattern.match(value):
                raise Reject(400, f"invalid '{name}'")
            return value

        deploy_id = field("deploy_id", DEPLOY_ID_RE)
        image = field("image", app.image_pattern, optional=True)
        version = field("version", VERSION_RE, optional=True)
        extra = payload.get("extra", {})
        if not isinstance(extra, dict):
            raise Reject(400, "invalid 'extra'")

        if identity.mode == "oidc":
            # The token is the source of truth; the payload must agree with it.
            claims = identity.claims
            for name in ("repository_id", "sha", "run_id", "run_attempt"):
                if str(payload.get(name)) != str(claims.get(name)):
                    raise Reject(400, f"'{name}' does not match the token")
            if not str(deploy_id).startswith(
                f"{claims['run_id']}-{claims['run_attempt']}-"
            ):
                raise Reject(400, "'deploy_id' does not match the token")
            repository, sha, ref, actor = (
                str(claims.get(k, "")) for k in ("repository", "sha", "ref", "actor")
            )
        else:
            repository = field("repository", REPOSITORY_RE)
            sha = field("sha", SHA_RE)
            ref = field("ref", REF_RE)
            actor = field("actor", ACTOR_RE)

        return {
            "app": app.name,
            "deploy_id": deploy_id,
            "image": image,
            "version": version,
            "repository": repository,
            "sha": sha,
            "ref": ref,
            "actor": actor,
            "auth": identity.mode,
            "extra": extra,
        }

    # -- state -------------------------------------------------------------

    def _prune(self) -> None:
        now = self.clock()
        with self._db_lock:
            self._db.execute("DELETE FROM seen WHERE expires < ?", (now,))
            self._db.execute(
                "DELETE FROM deploys WHERE updated < ?", (now - DEDUPE_RETENTION,)
            )

    def _record_once(self, key: str, expires: float) -> None:
        with self._db_lock:
            try:
                self._db.execute(
                    "INSERT INTO seen (key, expires) VALUES (?, ?)", (key, expires)
                )
            except sqlite3.IntegrityError:
                raise Reject(401, "replayed credentials") from None

    def _set_deploy(self, app: str, deploy_id: str, status: str, code: int) -> None:
        with self._db_lock:
            self._db.execute(
                "INSERT OR REPLACE INTO deploys (app, deploy_id, status, code, updated) VALUES (?, ?, ?, ?, ?)",
                (app, deploy_id, status, code, self.clock()),
            )

    # -- deploy ------------------------------------------------------------

    def _deploy(self, app: App, ctx: dict[str, Any]) -> Result:
        deploy_id = ctx["deploy_id"]
        with self._db_lock:
            row = self._db.execute(
                "SELECT status, code FROM deploys WHERE app = ? AND deploy_id = ?",
                (app.name, deploy_id),
            ).fetchone()
            if row and row[0] == "running":
                return Result(
                    409, {"error": "deploy already in progress", "deploy_id": deploy_id}
                )
            if row and row[0] in ("deployed", "deferred"):
                return Result(
                    row[1],
                    {"status": row[0], "deploy_id": deploy_id, "duplicate": True},
                )
            self._db.execute(
                "INSERT OR REPLACE INTO deploys (app, deploy_id, status, code, updated) VALUES (?, ?, 'running', 0, ?)",
                (app.name, deploy_id, self.clock()),
            )

        env = {"PATH": os.defpath, "LANG": "C.UTF-8"}
        if "SYSTEMROOT" in os.environ:  # Windows needs this to start processes
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        for key in (
            "app",
            "image",
            "version",
            "repository",
            "sha",
            "ref",
            "actor",
            "auth",
        ):
            env[f"DEPLOY_{key.upper()}"] = ctx[key] or ""
        env["DEPLOY_ID"] = deploy_id

        log.info(
            "deploying %s %s (%s %s by %s)",
            app.name,
            deploy_id,
            ctx["repository"],
            ctx["sha"],
            ctx["actor"],
        )
        with self._app_locks[app.name]:
            try:
                proc = subprocess.run(
                    list(app.command),
                    input=json.dumps(ctx).encode(),
                    env=env,
                    capture_output=True,
                    timeout=app.timeout,
                    shell=False,
                )
            except subprocess.TimeoutExpired:
                log.error(
                    "deploy %s %s timed out after %ss", app.name, deploy_id, app.timeout
                )
                self._set_deploy(app.name, deploy_id, "failed", 504)
                return Result(
                    504,
                    {
                        "status": "failed",
                        "error": "deploy command timed out",
                        "deploy_id": deploy_id,
                    },
                )
            except OSError as e:
                log.error("deploy %s %s could not start: %s", app.name, deploy_id, e)
                self._set_deploy(app.name, deploy_id, "failed", 500)
                return Result(
                    500,
                    {
                        "status": "failed",
                        "error": "deploy command could not start",
                        "deploy_id": deploy_id,
                    },
                )

        output = (proc.stdout + proc.stderr).decode(errors="replace")[-4000:]
        if proc.returncode == 0:
            status, code = "deployed", 200
        elif proc.returncode == DEFERRED_EXIT_CODE:
            status, code = "deferred", 202
        else:
            status, code = "failed", 500
        log.log(
            logging.INFO if code < 300 else logging.ERROR,
            "deploy %s %s %s (exit %s)\n%s",
            app.name,
            deploy_id,
            status,
            proc.returncode,
            output,
        )
        self._set_deploy(app.name, deploy_id, status, code)
        body: dict[str, Any] = {"status": status, "deploy_id": deploy_id}
        if code == 500:
            body.update(error="deploy command failed", exit_code=proc.returncode)
        return Result(code, body)


# -- HTTP glue -------------------------------------------------------------


def make_handler(
    verifier: Verifier, max_body_bytes: int
) -> type[BaseHTTPRequestHandler]:
    path_re = re.compile(r"^/hooks/([a-z0-9][a-z0-9_-]{0,63})$")

    class Handler(BaseHTTPRequestHandler):
        server_version = "deploy-verifier"
        sys_version = ""
        timeout = 15  # per socket operation; guards against slow clients

        def do_POST(self) -> None:
            match = path_re.match(self.path)
            if not match:
                return self._send(Result(404, {"error": "not found"}))
            length = self.headers.get("Content-Length")
            if length is None or not length.isdigit():
                return self._send(Result(411, {"error": "Content-Length required"}))
            if int(length) > max_body_bytes:
                return self._send(Result(413, {"error": "body too large"}))
            body = self.rfile.read(int(length))
            headers = {k.lower(): v for k, v in self.headers.items()}
            try:
                result = verifier.handle(match.group(1), headers, body)
            except Exception:
                log.exception("unhandled error")
                result = Result(500, {"error": "internal error"})
            self._send(result)

        def _send(self, result: Result) -> None:
            data = json.dumps(result.body).encode()
            self.send_response(result.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            log.info("%s %s", self.address_string(), format % args)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--config", type=Path, default=Path("/etc/deploy-verifier/config.toml")
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    config = load_config(args.config)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    verifier = Verifier(
        config.apps,
        config.state_dir / "state.sqlite3",
        issuer=config.oidc_issuer,
        max_token_age=config.max_token_age,
    )
    server = ThreadingHTTPServer(
        (config.listen_host, config.listen_port),
        make_handler(verifier, config.max_body_bytes),
    )
    log.info(
        "listening on %s:%s for apps: %s",
        config.listen_host,
        config.listen_port,
        ", ".join(config.apps),
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
