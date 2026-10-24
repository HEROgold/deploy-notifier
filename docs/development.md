# Development

## Local checks

```sh
uv run prek install                       # gitleaks on every commit
uv run --group verifier pytest verifier   # verifier tests
shellcheck notify.sh                      # lint the action
uv run --group docs zensical serve        # preview the docs
```

`uv run prek run --all-files` runs the hooks from `.pre-commit-config.yaml` on the whole
tree, not only the staged files.

## Dependency groups

`pyproject.toml` exists only for tooling. Nothing in this repo gets packaged.

| Group | Holds | Used by |
|---|---|---|
| `verifier` | `pyjwt[crypto]` | the `verifier` test job |
| `dev` | `pytest`, `prek` | local checks |
| `docs` | `zensical` | the docs workflow |

The verifier itself declares `pyjwt[crypto]` in its PEP 723 header, and the systemd unit,
the e2e job and the container all install it from there. The `verifier` group repeats it
so that `uv.lock` holds a version. Dependabot reads the lock, and the test job runs
against it with `--locked`, so a Dependabot bump of pyjwt runs the verifier tests before
it merges. If you raise the floor in the PEP 723 header, raise it in the group too.

## CI checks

| Workflow | Runs on | Fails when |
|---|---|---|
| `test.yml` | every push and PR | shellcheck, the verifier tests, or the e2e deploy fails |
| `docs.yml` | PRs that touch the docs, `verifier/` or the lock; pushes to `main` | `zensical build` fails, for example on a missing snippet file |
| `gitleaks.yml` | PRs, pushes to `main` | gitleaks finds a secret anywhere in the history |
| `trivy.yml` | PRs that touch `verifier/`, pushes to `main`, Mondays 04:17 UTC | the verifier image has a fixable HIGH or CRITICAL finding (see below) |

### gitleaks

The prek hook checks staged changes before each commit. The workflow scans the full
history (`fetch-depth: 0`) for clones where nobody installed the hook. It needs
`pull-requests: read`, because gitleaks-action lists a PR's commits through the API.

### pre-commit.ci

The [pre-commit.ci](https://pre-commit.ci) app reads the `ci:` block in
`.pre-commit-config.yaml`. It's here only for `autoupdate`: once a week it opens a PR that
bumps each hook's `rev:`. The [Dependabot](#dependabot) entries don't cover hook revs.

`autofix_prs` is off, because no hook rewrites files yet. The check pre-commit.ci adds to
PRs passes without scanning anything: the gitleaks hook runs with `--staged`, and nothing is
staged in a fresh checkout. `gitleaks.yml` is the secret scan that counts.

The app has to be installed for the repository at <https://pre-commit.ci>. Without it, the
`ci:` block does nothing.

### Dependabot

`.github/dependabot.yml` checks three ecosystems weekly:

- `uv` at `/`: everything in `uv.lock`, including the verifier's pyjwt.
- `github-actions` at `/`: every `uses:` in the workflows.
- `docker` at `/verifier`: the `FROM` images in `verifier/Dockerfile`.

### Trivy

`trivy.yml` builds `verifier/Dockerfile` and scans the image twice:

1. **Everything, as SARIF**, uploaded to the repository's Security tab. This step never
   fails the job.
2. **HIGH and CRITICAL with a fix available**, as a table in the log. This step fails the
   job, but it skips `/usr/bin/docker` and the compose plugin.

The skipped files come prebuilt from `docker:29-cli`. Their findings are usually Go
advisories that only Docker can fix by rebuilding, for example a `golang.org/x/net`
version that even the latest docker/compose release still pins. Failing on them would
leave the weekly run red with nothing to change in this repo. They still show up in the
Security tab, and a newer `docker:29-cli` clears them.

The scan runs weekly because new advisories land without any change to the code.

`aquasecurity/trivy-action` is pinned to a commit SHA, not a tag. Its tags were hijacked
in 2026 and pointed at a malicious version. When Dependabot bumps it, check that the new
SHA belongs to a release on the
[trivy-action releases page](https://github.com/aquasecurity/trivy-action/releases).

The Dockerfile uninstalls pip after installing pyjwt. The verifier never uses pip at
runtime, and pip vendors its own copies of urllib3, msgpack and others, which Trivy reports
as soon as they fall behind.
