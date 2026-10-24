# Pre-publication secret review

Reviewed on 24 October 2026. This is a point-in-time review, not a guarantee
that the repository contains no sensitive information. Re-run the checks after
further changes and before publication.

Base commit: `29aa6bf24581f87ca3eaf94cf67e3a5ef0c6339d`. The working-file scan also
includes the uncommitted changes present during this review.

## Results

No suspected secrets were reported by Gitleaks 8.30.1 in any of these scans:

| Scope | Coverage | Findings |
|---|---|---|
| Publishable working files | 584 tracked and non-ignored untracked paths, including uncommitted product changes | 0 |
| Git history | 76 commits reachable from all local refs after fetching all configured remote branches and tags; reflogs included in the scanner command | 0 |
| Local Git object database | All 872 locally stored blobs, including blobs outside normal branch history | 0 |

The repository was not a shallow checkout. The remote fetch completed
successfully. The Git object scan also covers stored versions that may differ
from the current working files, including staged content.

An additional local, in-memory exact-match check compared the three populated
credential values configured in `.env.local` against every scanned Git blob,
publishable working file, and commit messages reachable from refs and reflogs.
No matches were found. Credential values were not printed or written into the
audit summaries. No credential-authenticated remote URL was found.

Neither `.env` nor `.env.local` is tracked. Historical filename inspection found
only `.env.example` among environment/credential-looking paths; it contains
configuration examples. The committed `cacert.pem` is a third-party public CA
certificate bundle, not a private key.

## Preventive change

`.gitignore` excludes `.env` and `.env.*`, including `.env.local`, deployment
variants, and dotted backup names. `.env.example` and `.env.*.example` remain
eligible for version control; examples must contain placeholders only.

## Repository hygiene finding

The audit found 515 tracked files from an accidentally committed Python virtual
environment under `matchos/bin/`, `matchos/lib/`, and `matchos/pyvenv.cfg`.
They were included in the scans and produced no findings. Following the audit,
these artifacts were removed from the Git index while preserving the local
files, and explicit ignore rules were added for the legacy environment paths.
Their removal is staged for the next commit; older commits still contain them.
No Git history was rewritten. Retain any third-party license notices required
for third-party code that remains in the repository.

## Repeating the baseline checks

Install [Gitleaks](https://github.com/gitleaks/gitleaks), then run from the
repository root:

```sh
git fetch --all --tags
gitleaks git --log-opts="--all --reflog --full-history" --redact=100 --ignore-gitleaks-allow .
gitleaks git --staged --redact=100 --ignore-gitleaks-allow .
```

The staged command checks the index; neither Git command checks all unstaged
and untracked files. Before publishing, also scan a local export of all tracked
and non-ignored untracked working files with `gitleaks dir --redact=100`.
The review above additionally scanned every local Git blob and compared locally
configured credentials in memory; those checks go beyond the baseline commands.

All scanning was performed locally. Source code and credentials were not sent
to an external scanning service. The Gitleaks binary was downloaded from its
official GitHub release and checked against that release's SHA-256 checksum.
Reports retained for this review contain no credential values.

## Limits

Pattern detection cannot identify every credential or sensitive datum. Exact
matching only covers currently available local credential values and will not
identify every previously used, encoded, or split secret. Ignored runtime files
and installed environments outside the tracked file set were not generally
scanned; `.env.local` was read only to obtain the comparison values. Remote-only
pull-request refs, GitHub Actions logs/artifacts, release assets, issues, and
copies held by others are outside this review. Public author identities in Git
metadata were not treated as credentials.

If a secret is found later, revoke or rotate it first; removing a file or
rewriting history alone does not invalidate a credential.
