# Developing MatchOS

Use Python 3.13 or newer and Node.js 22 or newer. Production runs Python 3.13; Node is needed only for browser-logic tests. Azure credentials are not required for development or tests.

## Set up

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-observability.txt -r requirements-dev.txt
```

To use the same runtime versions as production, replace the first two requirements files with `requirements-production.lock` and install it with `--require-hashes`. Install development tools separately afterward.

Use `.env.local` for local settings. For offline playback, set `DECISION_PROVIDER=local` and `REASONING_PROVIDER=local`, then run `.venv/bin/python -m matchos`. [README.md](README.md) covers Azure and Speech configuration.

## Validate a change

Run from the repository root:

```sh
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m pytest
node --test tests/*.test.cjs
.venv/bin/python -m matchos.evaluation run --local
```

Tests isolate configuration from local dotenv files and default to offline providers. Provider contract tests replace HTTP/credential boundaries; they exercise the real adapters without sending paid requests. Individual tests can override configuration explicitly. Do not add live provider calls to the default suite.

Apply mechanical formatting with `.venv/bin/python -m ruff format .`. CI runs lint, formatting, the full backend suite, browser tests, the offline evaluation baseline, package building, Bicep compilation and container access checks. It needs no cloud inference credentials.

## Where changes belong

Start with [ARCHITECTURE.md](ARCHITECTURE.md). The shared evidence workflow and independent viewer workflows are the same paths used by live playback, Judge demo and evaluations.

- **Match metrics and evidence:** compute deterministic aggregates, then attach candidate facts, checks and source events. Keep canonical units consistent. A language model must not invent a new candidate or calculate the canonical metrics.
- **Editorial behavior:** selection owns relevance; publication policy owns freshness, duplication and cadence. Keep those responsibilities separate. Test selection, abstention, holds and expiry during inference.
- **Personas and languages:** carry the profile through selection, copy, validation and Speech. Preserve neutral roles' lack of team allegiance. Keep localized fallback copy usable when a provider fails.
- **Providers:** retain asynchronous deadlines, credential redaction and cancellation. New providers must implement selection from the verified pool or generation from the selected facts, with visible fallback diagnostics.
- **Browser behavior:** match/pitch UI is in `app.js`; `demo.js`, `telemetry.js` and `audio.js` own their respective controllers. Assets must be included in content-versioned delivery and the deployment smoke check.

Add regression tests for changed behavior, especially async lifecycle boundaries. Do not replace meaningful assertions with unconditional skips or canned model outputs to make the demo look different.

## Package and dependency changes

Build a distributable wheel with:

```sh
.venv/bin/python -m build --wheel
```

The wheel includes the browser assets and license, excluding virtual environments, tests, credentials and video projects. Installing it provides both `python -m matchos` and the `matchos` command. Source runs load dotenv files from the checkout root; installed wheels load them from the directory where the app is launched.

Runtime requirements are mirrored in `pyproject.toml` and `requirements.txt`; a test checks they agree. Development tools live in `requirements-dev.txt`. Optional tracing/evaluation dependencies are documented in [evals/README.md](evals/README.md).

Regenerate the Linux production lock with uv after changing runtime requirements:

```sh
uv pip compile requirements.txt requirements-observability.txt \
  --python-version 3.13 --python-platform x86_64-unknown-linux-gnu \
  --generate-hashes --output-file requirements-production.lock
```

Review dependency changes and run the full checks before deploying. [DEPLOYMENT.md](DEPLOYMENT.md) describes cloud smoke tests, release recording and rollback. Paid provider/Speech checks are explicit deployment steps, not CI tests.

## Files and media

Keep secrets, generated caches, evaluation results, package builds and raw video takes out of Git. [videos/README.md](videos/README.md) identifies the current demo and the selected media required to reproduce it. Preserve the existing video projects and their licensed assets when cleaning generated files.

The repository's [evaluation license](LICENSE) applies to project contributions; font and GSAP assets retain their own notices.
