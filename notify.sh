#!/usr/bin/env bash
# deploy-notifier: POST an authenticated deploy notification and wait for the server's verdict.
# All inputs arrive as INPUT_* environment variables (see action.yml); never interpolate them.
set -euo pipefail

die() { echo "::error title=deploy-notifier::$*" >&2; exit 1; }
rand_hex() { od -An -tx1 -N16 /dev/urandom | tr -d ' \n'; }

url="${INPUT_URL:-}"
auth="${INPUT_AUTH:-oidc}"
audience="${INPUT_AUDIENCE:-}"
timeout="${INPUT_TIMEOUT:-600}"
allow_insecure="${INPUT_ALLOW_INSECURE:-false}"
extra="${INPUT_EXTRA:-}"
[[ -n "$extra" ]] || extra='{}'

# --- validate inputs --------------------------------------------------------

[[ -n "$url" ]] || die "input 'url' is required"
[[ "$timeout" =~ ^[1-9][0-9]*$ ]] || die "input 'timeout' must be a positive number of seconds"
case "$allow_insecure" in true | false) ;; *) die "input 'allow_insecure' must be 'true' or 'false'" ;; esac
case "$auth" in oidc | hmac) ;; *) die "input 'auth' must be 'oidc' or 'hmac'" ;; esac

[[ "$url" =~ ^(https?)://([^/?#]+) ]] || die "input 'url' must be an absolute https:// URL"
scheme="${BASH_REMATCH[1]}"
authority="${BASH_REMATCH[2]}"
origin="$scheme://$authority"
[[ "$authority" != *@* ]] || die "credentials in 'url' are not allowed"

if [[ "$scheme" == https ]]; then
  proto='=https'
elif [[ "$allow_insecure" == true ]]; then
  proto='=http,https'
  echo "::warning title=deploy-notifier::sending to a plain http:// target (allow_insecure: true)"
else
  die "refusing plain http:// target; use https:// (or allow_insecure: true for local testing)"
fi

[[ -n "$audience" ]] || audience="$origin"

if [[ "$auth" == hmac && -z "${INPUT_HMAC_SECRET:-}" ]]; then
  die "input 'hmac_secret' is required when auth is 'hmac'"
fi
if [[ "$auth" == oidc && (-z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" || -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}") ]]; then
  die "no OIDC token available: add 'permissions: id-token: write' to the job, or use auth: hmac"
fi

jq -e 'type == "object"' >/dev/null 2>&1 <<<"$extra" || die "input 'extra' must be a JSON object"

py="$(command -v python3 || command -v python || true)"
[[ "$auth" != hmac || -n "$py" ]] || die "python3 is required for auth: hmac"

# --- build payload ----------------------------------------------------------

work="$(mktemp -d "${RUNNER_TEMP:-/tmp}/deploy-notifier.XXXXXX")"
chmod 700 "$work"
trap 'rm -rf "$work"' EXIT
umask 077

body_file="$work/body.json"
headers_file="$work/headers"
resp_file="$work/response"

url_hash="$(printf '%s' "$url" | openssl dgst -sha256 -r | cut -c1-12)"
deploy_id="${GITHUB_RUN_ID:-0}-${GITHUB_RUN_ATTEMPT:-0}-${url_hash}"

jq -nc \
  --arg image "${INPUT_IMAGE:-}" \
  --arg version "${INPUT_VERSION:-}" \
  --argjson extra "$extra" \
  --arg repository "${GITHUB_REPOSITORY:-}" \
  --arg repository_id "${GITHUB_REPOSITORY_ID:-}" \
  --arg sha "${GITHUB_SHA:-}" \
  --arg ref "${GITHUB_REF:-}" \
  --arg run_id "${GITHUB_RUN_ID:-}" \
  --arg run_attempt "${GITHUB_RUN_ATTEMPT:-}" \
  --arg actor "${GITHUB_ACTOR:-}" \
  --arg deploy_id "$deploy_id" \
  --arg sent_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{
    image: (if $image == "" then null else $image end),
    version: (if $version == "" then null else $version end),
    extra: $extra,
    repository: $repository,
    repository_id: $repository_id,
    sha: $sha,
    ref: $ref,
    run_id: $run_id,
    run_attempt: $run_attempt,
    actor: $actor,
    deploy_id: $deploy_id,
    sent_at: $sent_at
  }' >"$body_file"

# --- authentication ---------------------------------------------------------

fetch_oidc_token() {
  local req_headers="$work/oidc-headers" aud_enc token
  printf 'Authorization: bearer %s\n' "$ACTIONS_ID_TOKEN_REQUEST_TOKEN" >"$req_headers"
  aud_enc="$(jq -rn --arg a "$audience" '$a | @uri')"
  token="$(curl -sS --fail --proto '=https' --max-time 30 -H @"$req_headers" \
    "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=${aud_enc}" | jq -er '.value')" ||
    die "failed to obtain an OIDC token from GitHub"
  printf '%s' "$token"
}

hmac_signature() {
  HMAC_TS="$1" HMAC_BODY_FILE="$body_file" "$py" -c '
import hashlib, hmac, os
key = os.environ["INPUT_HMAC_SECRET"].encode()
with open(os.environ["HMAC_BODY_FILE"], "rb") as f:
    msg = os.environ["HMAC_TS"].encode() + b"." + f.read()
print(hmac.new(key, msg, hashlib.sha256).hexdigest())'
}

# Fresh credentials per attempt: the server rejects replayed tokens/signatures.
write_headers() {
  {
    echo "Content-Type: application/json"
    echo "User-Agent: deploy-notifier/1"
    echo "X-Deploy-Id: $deploy_id"
  } >"$headers_file"
  if [[ "$auth" == oidc ]]; then
    local token
    token="$(fetch_oidc_token)"
    echo "Authorization: Bearer $token" >>"$headers_file"
  else
    local ts sig
    ts="$(date +%s)"
    sig="$(hmac_signature "$ts")"
    {
      echo "X-Timestamp: $ts"
      echo "X-Signature-256: sha256=$sig"
    } >>"$headers_file"
  fi
}

# --- send -------------------------------------------------------------------

echo "Notifying $origin (auth: $auth, deploy id: $deploy_id, timeout: ${timeout}s)"

max_attempts=3
http_code=000
rc=0
for ((attempt = 1; attempt <= max_attempts; attempt++)); do
  write_headers
  : >"$resp_file"
  set +e
  http_code="$(curl -sS --proto "$proto" --connect-timeout 10 --max-time "$timeout" \
    -X POST --data-binary @"$body_file" -H @"$headers_file" \
    -o "$resp_file" -w '%{http_code}' "$url")"
  rc=$?
  set -e

  # Retry only when the request evidently never reached the deploy logic.
  # Timeouts (28) and 504 are never retried: the deploy may still be running.
  retry=false
  case "$rc" in
    0) case "$http_code" in 502 | 503) retry=true ;; esac ;;
    6 | 7 | 35) retry=true ;;
  esac

  if [[ "$retry" == true && "$attempt" -lt "$max_attempts" ]]; then
    delay=$((attempt * 5))
    echo "::warning title=deploy-notifier::attempt $attempt failed (curl exit $rc, HTTP $http_code); retrying in ${delay}s"
    sleep "$delay"
    continue
  fi
  break
done

# --- report -----------------------------------------------------------------

response="$(head -c 4096 "$resp_file" 2>/dev/null || true)"

if [[ "$rc" -ne 0 ]]; then
  status=failed
elif [[ "$http_code" == 202 ]]; then
  status=deferred
elif [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
  status=deployed
else
  status=failed
fi

if [[ -n "$response" ]]; then
  # Server output is untrusted: stop it from being parsed as workflow commands.
  stop_token="$(rand_hex)"
  echo "::stop-commands::$stop_token"
  printf 'Response (HTTP %s):\n%s\n' "$http_code" "$response"
  echo "::$stop_token::"
fi

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  delim="EOF_$(rand_hex)"
  {
    echo "status=$status"
    echo "http_code=$http_code"
    echo "response<<$delim"
    printf '%s\n' "$response"
    echo "$delim"
  } >>"$GITHUB_OUTPUT"
fi

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  echo "deploy-notifier → \`$origin\`: **$status** (HTTP $http_code, deploy id \`$deploy_id\`)" >>"$GITHUB_STEP_SUMMARY"
fi

case "$rc" in
  0) ;;
  28) die "no response within ${timeout}s; the server may still be deploying (not retried to avoid a double deploy)" ;;
  *) die "request to $origin failed (curl exit $rc)" ;;
esac

[[ "$status" != failed ]] || die "server responded HTTP $http_code"
echo "Server responded HTTP $http_code: $status"
