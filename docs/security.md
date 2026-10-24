# Security

## Pin IDs, not names

`repository_id` and `repository_owner_id` stay the same when a repository or owner is
renamed. A name, once freed, can be registered by someone else.

Pin an `environment` with required reviewers or branch rules, or at least a `ref`. The
verifier refuses to start an OIDC app without one of them, because otherwise any branch,
including one from an untrusted pull request, could deploy.

## The docker group

Membership of the `docker` group is effectively root on the host, and so is the
`docker.sock` mount in the [container setup](examples/container-traefik.md). The verifier
only runs the configured command, but anything that can make that command misbehave has
root.

If that's too much, drop the group from the `deploy` user and give it a single `sudo` rule
for the deploy script:

```text
# /etc/sudoers.d/deploy
deploy ALL=(root) NOPASSWD: /usr/local/bin/deploy-myapp
```

Then set `command = ["/usr/bin/sudo", "/usr/local/bin/deploy-myapp"]`, remove
`SupplementaryGroups=docker` from the unit, and remove `NoNewPrivileges=yes`, which blocks
sudo.

## Input validation

The command only receives validated values:

- Image refs must match `image_pattern`. The default accepts only digest-pinned refs.
- Versions must match `[\w.+-]{1,128}`.
- In OIDC mode, the repository, SHA and ref come from the signed token, not the body.
- In HMAC mode, the verifier checks repository, SHA, ref and actor against strict patterns.
- `extra` is passed through unchecked. Validate anything you read from it.

The verifier runs the command as argv, never through a shell, with a minimal environment.

## What the client sees

Failed deploys return only the exit code. Command output goes to the log
(`journalctl -u deploy-verifier`), never to the workflow. A rejected request returns a short
reason, and the log holds the detail.

## The action side

- The action sends to `https://` only, verifies certificates, and never follows redirects.
- It passes inputs to the script through environment variables, never by interpolating
  them into shell code.
- It writes tokens and signatures to a `0600` header file, so they never show up in a
  process listing.
- It prints the server's response with workflow commands disabled.

## HMAC secrets

Store the secret as an environment secret, not a repository secret. Only jobs in that
environment can then read it. Use at least 32 random bytes; the verifier refuses shorter
secrets. Prefer OIDC where it works, since there's no secret to leak or rotate.
