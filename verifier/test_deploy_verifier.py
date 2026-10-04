"""Tests for the reference verifier. Run: uv run --with pytest --with "pyjwt[crypto]" pytest verifier"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import http.client
import json
import sys
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import deploy_verifier as dv

AUDIENCE = "https://deploy.example.com"
IMAGE = "ghcr.io/owner/app@sha256:" + "ab" * 32
SHA = "a" * 40
SECRET = b"s" * 32

SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

# Hook: append env + stdin to a log file, then exit with argv[2].
HOOK = """
import json, os, sys
with open(sys.argv[1], "a") as f:
    f.write(json.dumps({"env": {k: v for k, v in os.environ.items() if k.startswith("DEPLOY_")},
                        "stdin": json.load(sys.stdin)}) + "\\n")
if len(sys.argv) > 3:
    import time; time.sleep(float(sys.argv[3]))
sys.exit(int(sys.argv[2]))
"""


class FakeJWKClient:
    def get_signing_key_from_jwt(self, token: str) -> Any:
        return type("Key", (), {"key": SIGNING_KEY.public_key()})()


def make_claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims = {
        "iss": dv.GITHUB_ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + 300,
        "jti": str(uuid.uuid4()),
        "repository": "owner/app",
        "repository_id": "42",
        "repository_owner_id": "7",
        "environment": "production",
        "ref": "refs/heads/main",
        "sha": SHA,
        "run_id": "100",
        "run_attempt": "1",
        "actor": "octocat",
    }
    claims.update(overrides)
    return claims


def make_token(key: Any = SIGNING_KEY, **overrides: Any) -> str:
    return jwt.encode(make_claims(**overrides), key, algorithm="RS256")


def make_body(**overrides: Any) -> bytes:
    body = {
        "image": IMAGE,
        "version": "1.2.3",
        "extra": {"note": "hi"},
        "repository": "owner/app",
        "repository_id": "42",
        "sha": SHA,
        "ref": "refs/heads/main",
        "run_id": "100",
        "run_attempt": "1",
        "actor": "octocat",
        "deploy_id": "100-1-0123456789ab",
        "sent_at": "2026-10-04T12:00:00Z",
    }
    body.update(overrides)
    return json.dumps(body).encode()


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def signed(
    body: bytes, ts: int | None = None, secret: bytes = SECRET
) -> dict[str, str]:
    ts = int(time.time()) if ts is None else ts
    sig = hmac.new(secret, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {"x-timestamp": str(ts), "x-signature-256": f"sha256={sig}"}


@pytest.fixture
def env(tmp_path: Path) -> Any:
    hook = tmp_path / "hook.py"
    hook.write_text(HOOK)
    calls = tmp_path / "calls.jsonl"

    def app(
        name: str, exit_code: int = 0, sleep: float | None = None, **kw: Any
    ) -> dv.App:
        command = [sys.executable, str(hook), str(calls), str(exit_code)]
        if sleep is not None:
            command.append(str(sleep))
        spec = {
            "command": command,
            "audience": AUDIENCE,
            "repository_id": "42",
            "repository_owner_id": "7",
            "environment": "production",
            **kw,
        }
        return dv._load_app(name, spec)

    secret_file = tmp_path / "secret"
    secret_file.write_bytes(SECRET)
    apps = {
        "ok": app("ok"),
        "later": app("later", exit_code=dv.DEFERRED_EXIT_CODE),
        "broken": app("broken", exit_code=3),
        "slow": app("slow", sleep=5, timeout=1),
        "signed": app("signed", repository_id=None, hmac_secret_file=str(secret_file)),
    }
    verifier = dv.Verifier(apps, tmp_path / "state.sqlite3", jwk_client=FakeJWKClient())

    def read_calls() -> list[dict[str, Any]]:
        if not calls.exists():
            return []
        return [json.loads(line) for line in calls.read_text().splitlines()]

    return type(
        "Env",
        (),
        {"verifier": verifier, "calls": staticmethod(read_calls), "tmp": tmp_path},
    )


# -- OIDC happy paths --------------------------------------------------------


def test_oidc_deploys_and_passes_trusted_context(env: Any) -> None:
    result = env.verifier.handle("ok", bearer(make_token()), make_body())
    assert result.code == 200
    assert result.body["status"] == "deployed"
    [call] = env.calls()
    assert call["env"]["DEPLOY_IMAGE"] == IMAGE
    assert call["env"]["DEPLOY_REPOSITORY"] == "owner/app"
    assert call["env"]["DEPLOY_AUTH"] == "oidc"
    assert call["stdin"]["extra"] == {"note": "hi"}


def test_exit_75_means_deferred(env: Any) -> None:
    result = env.verifier.handle("later", bearer(make_token()), make_body())
    assert (result.code, result.body["status"]) == (202, "deferred")


def test_failed_command_does_not_leak_output(env: Any) -> None:
    result = env.verifier.handle("broken", bearer(make_token()), make_body())
    assert result.code == 500
    assert result.body == {
        "status": "failed",
        "deploy_id": "100-1-0123456789ab",
        "error": "deploy command failed",
        "exit_code": 3,
    }


def test_command_timeout_is_504(env: Any) -> None:
    result = env.verifier.handle("slow", bearer(make_token()), make_body())
    assert result.code == 504


def test_duplicate_deploy_id_returns_cached_verdict_without_rerunning(env: Any) -> None:
    assert env.verifier.handle("ok", bearer(make_token()), make_body()).code == 200
    again = env.verifier.handle("ok", bearer(make_token()), make_body())
    assert again.code == 200
    assert again.body["duplicate"] is True
    assert len(env.calls()) == 1


def test_failed_deploy_id_can_be_retried(env: Any) -> None:
    assert env.verifier.handle("broken", bearer(make_token()), make_body()).code == 500
    assert env.verifier.handle("broken", bearer(make_token()), make_body()).code == 500
    assert len(env.calls()) == 2


# -- OIDC rejections ---------------------------------------------------------


@pytest.mark.parametrize(
    ("claims", "code"),
    [
        ({"aud": "https://evil.example.com"}, 401),
        ({"iss": "https://evil.example.com"}, 401),
        ({"exp": int(time.time()) - 120}, 401),
        ({"iat": int(time.time()) - 3600, "nbf": int(time.time()) - 3600}, 401),
        ({"repository_id": "43"}, 403),
        ({"repository_owner_id": "8"}, 403),
        ({"environment": "staging"}, 403),
        ({"sha": "b" * 40}, 400),
        ({"run_id": "101"}, 400),
    ],
)
def test_oidc_rejections(env: Any, claims: dict[str, Any], code: int) -> None:
    result = env.verifier.handle("ok", bearer(make_token(**claims)), make_body())
    assert result.code == code
    assert env.calls() == []


def test_token_signed_by_other_key_is_rejected(env: Any) -> None:
    assert (
        env.verifier.handle("ok", bearer(make_token(key=OTHER_KEY)), make_body()).code
        == 401
    )


def test_replayed_token_is_rejected(env: Any) -> None:
    token = make_token()
    assert env.verifier.handle("ok", bearer(token), make_body()).code == 200
    assert (
        env.verifier.handle(
            "ok", bearer(token), make_body(deploy_id="100-1-ffffffffffff")
        ).code
        == 401
    )


def test_ref_pin(env: Any) -> None:
    env.verifier.apps["main"] = dataclasses.replace(
        env.verifier.apps["ok"], name="main", environment=None, ref="refs/heads/main"
    )
    env.verifier._app_locks["main"] = threading.Lock()
    assert (
        env.verifier.handle(
            "main", bearer(make_token(ref="refs/heads/feature")), make_body()
        ).code
        == 403
    )
    assert env.verifier.handle("main", bearer(make_token()), make_body()).code == 200


# -- payload validation ------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"image": "ghcr.io/owner/app:latest"},
        {"image": "ghcr.io/owner/app@sha256:" + "ab" * 32 + "; rm -rf /"},
        {"version": "1.0 $(id)"},
        {"deploy_id": "999-1-0123456789ab"},
        {"deploy_id": "not-an-id"},
        {"extra": "nope"},
    ],
)
def test_bad_payload_is_rejected(env: Any, overrides: dict[str, Any]) -> None:
    result = env.verifier.handle("ok", bearer(make_token()), make_body(**overrides))
    assert result.code == 400
    assert env.calls() == []


def test_image_and_version_are_optional(env: Any) -> None:
    assert (
        env.verifier.handle(
            "ok", bearer(make_token()), make_body(image=None, version=None)
        ).code
        == 200
    )


def test_unknown_app_and_missing_credentials(env: Any) -> None:
    assert env.verifier.handle("nope", bearer(make_token()), make_body()).code == 404
    assert env.verifier.handle("ok", {}, make_body()).code == 401
    # OIDC-only app ignores HMAC headers
    assert env.verifier.handle("ok", signed(make_body()), make_body()).code == 401


# -- HMAC ----------------------------------------------------------------------


def test_hmac_deploys(env: Any) -> None:
    body = make_body()
    result = env.verifier.handle("signed", signed(body), body)
    assert result.code == 200
    assert env.calls()[0]["env"]["DEPLOY_AUTH"] == "hmac"


@pytest.mark.parametrize(
    "headers",
    [
        lambda b: signed(b, secret=b"x" * 32),
        lambda b: signed(b, ts=int(time.time()) - 600),
        lambda b: {**signed(b), "x-timestamp": "abc"},
        lambda b: signed(b + b" "),
    ],
)
def test_hmac_rejections(env: Any, headers: Any) -> None:
    body = make_body()
    assert env.verifier.handle("signed", headers(body), body).code == 401


def test_hmac_replay_is_rejected(env: Any) -> None:
    body = make_body()
    headers = signed(body)
    assert env.verifier.handle("signed", headers, body).code == 200
    assert env.verifier.handle("signed", headers, body).code == 401


def test_hmac_validates_untrusted_context(env: Any) -> None:
    body = make_body(ref="refs/heads/x\nINJECT=1")
    assert env.verifier.handle("signed", signed(body), body).code == 400


# -- config ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"command": "deploy.sh"}, "list of strings"),
        ({"command": ["deploy.sh"]}, "absolute"),
        ({"command": [sys.executable]}, "OIDC"),
        ({"command": [sys.executable], "repository_id": "42"}, "audience"),
        (
            {"command": [sys.executable], "repository_id": "42", "audience": AUDIENCE},
            "environment",
        ),
    ],
)
def test_config_validation(spec: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        dv._load_app("app", spec)


def test_load_example_config(tmp_path: Path) -> None:
    example = Path(__file__).with_name("config.example.toml").read_text()
    secret = tmp_path / "secret"
    secret.write_bytes(SECRET)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        example.replace("/etc/deploy-verifier/legacy.secret", secret.as_posix())
    )
    config = dv.load_config(config_path)
    assert set(config.apps) == {"myapp", "legacy"}
    assert (config.listen_host, config.listen_port) == ("127.0.0.1", 9000)


# -- HTTP layer ------------------------------------------------------------------


def test_http_server(env: Any) -> None:
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), dv.make_handler(env.verifier, max_body_bytes=4096)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:

        def post(
            path: str, body: bytes, headers: dict[str, str]
        ) -> tuple[int, dict[str, Any]]:
            conn = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=10
            )
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())

        status, body = post(
            "/hooks/ok", make_body(), {"Authorization": f"Bearer {make_token()}"}
        )
        assert (status, body["status"]) == (200, "deployed")
        assert post("/hooks/ok", b"x" * 5000, {})[0] == 413
        assert post("/other", make_body(), {})[0] == 404
    finally:
        server.shutdown()
