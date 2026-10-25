# MatchOS

**One match. A different perspective for every viewer.**

MatchOS turns a simulated football match into live, personalized stories. A broadcaster gets a concise match update, a supporter gets their team's perspective, and an analyst gets the tactical evidence—all beside the same moving pitch.

Microsoft Agent Framework orchestrates shared evidence generation and independent viewer workflows. Microsoft Foundry's **Decision-1** selects what is worth showing; **gpt-6-luna** writes the supporting copy. Deterministic checks establish the match facts and control when a finished story can air.

## Explore the product

The default match features Liverpool and Manchester City, with three perspectives:

| Viewer | Default language | Perspective |
|---|---|---|
| Broadcaster — primary viewer | English | Neutral, timely headlines and concise commentary. |
| Liverpool fan | Spanish | Developments that matter to the supported team. |
| Analyst | English | Neutral tactical context and supporting statistics. |

Each viewer independently chooses a supported story or no interruption. They can agree when the same event matters to everyone. Casual and player-focused modes are also available from a card's settings.

- **Watch:** follow the pitch, pause, change speed, or drag the timeline. Add or configure up to three viewer cards in the sidebar.
- **Facts & evidence:** inspect a story's facts, checks and source events, including raw JSON. **View evidence on pitch** opens a historical event map.
- **Producer:** review the primary viewer's aired, suppressed and rejected updates. This is a publication history, not another model.
- **Telemetry:** inspect execution stages, model inputs and outputs, timings, errors and fallbacks; export the captured history as JSON.
- **Lab:** choose a scenario and replay a repeatable match.

Stories and spoken commentary support **English, French, Spanish and Hindi**, chosen per viewer. Application controls remain in English. **Listen** reads an aired card using Microsoft MAI-Voice models through Azure Speech; **Auto commentary** follows one selected perspective.

Cards update only when the selected story's copy and publication checks are complete. The previous aired story stays visible during model calls and later holds, with the hold reason shown separately. Live stat capsules keep following the match; expanded story evidence stays attached to the original update.

## Try the Judge demo

Click **Judge demo** to compare the three viewers during short passages of the default press-collapse scenario, starting at these checkpoints:

| Checkpoint | Development |
|---|---|
| 64:00 | Liverpool are playing through Manchester City's weakening press. |
| 65:04 | Liverpool have taken a 2–1 lead through Alexander Isak. |
| 68:01 | Mateo Kovacic's dismissal leaves Manchester City with ten players. |

These are synthetic events, not a real fixture or confirmed starting lineups. Club and player identities provide context; the simulator generates the match.

The guide reconstructs checkpoint evidence, then plays at least 12 seconds of the match at 1× while the normal model and publication workflows run. The pitch keeps moving until all three viewers finish, with at least three seconds after the last response before pausing for comparison. It shows actual aired or held outcomes; responses are not prerecorded, and distinct stories are not forced. Each checkpoint starts fresh viewer history while keeping its telemetry available for comparison.

For regular playback with cloud models, start at **1×**. At high speeds, a story's match-time validity window can expire while inference is running.

## Run locally

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Choose Azure or offline mode below, then start the app with the same interpreter:

```sh
.venv/bin/python -m matchos
```

Open [localhost:8000](http://localhost:8000).

### Azure models

Create or update `.env.local` in the repository root. [`.env.example`](.env.example) lists the available settings; preserve existing credentials when updating your file.

```dotenv
DECISION_PROVIDER=azure
REASONING_PROVIDER=azure
AZURE_FOUNDRY_ENDPOINT=https://YOUR-RESOURCE.services.ai.azure.com
AZURE_FOUNDRY_API_KEY=YOUR-RESOURCE-KEY
AZURE_AUTH_MODE=key
AZURE_DECISION_DEPLOYMENT=Decision-1
AZURE_REASONING_DEPLOYMENT=gpt-6-luna
```

Use the resource's HTTPS root and the exact deployment names from Foundry's **Get code / endpoint and samples** panel. Do not use a project URL or append an API path. Separate resources can use `AZURE_DECISION_ENDPOINT` / `AZURE_DECISION_API_KEY` and `AZURE_REASONING_ENDPOINT` / `AZURE_REASONING_API_KEY`.

For keyless access, use `AZURE_AUTH_MODE=entra`, omit the keys and run `az login` with an identity permitted to call both deployments. Azure deployments use managed identity.

The app loads `.env` and `.env.local` automatically. Precedence is **shell environment > `.env.local` > `.env` > defaults**. Both files are Git-ignored. Restart the backend after changing configuration.

### Offline mode and other providers

For a local demonstration without cloud inference, set:

```dotenv
DECISION_PROVIDER=local
REASONING_PROVIDER=local
```

Offline mode uses persona-aware selection heuristics and fact-based copy. Provider failures also have labelled local fallbacks; these are visible in logs and Telemetry.

Cloudflare Clef and Vercel AI Gateway Laya remain available for selection, and the LiteLLM adapter supports the retained Vertex AI configuration for language generation. Select them independently with `DECISION_PROVIDER` and `REASONING_PROVIDER`; credentials and model options are in [`.env.example`](.env.example).

## Spoken commentary

Add these settings to `.env.local` and restart:

```dotenv
MATCHOS_SPEECH_ENABLED=1
AZURE_SPEECH_KEY=YOUR-SPEECH-RESOURCE-KEY
AZURE_SPEECH_REGION=YOUR-SPEECH-RESOURCE-REGION
AZURE_SPEECH_MODEL=MAI-Voice-2.1-Flash
```

Use the key and region from a Speech-capable Azure resource's **Keys and Endpoint** page. Speech credentials are separate from model inference settings. Optional voice overrides are listed in [`.env.example`](.env.example).

Audio is off by default. Synthesis starts only when listening is requested and reads the published card with persona-appropriate delivery. Held stories are not spoken. One perspective plays at a time; seeking, resetting or changing the selected profile stops outdated audio.

## Execution telemetry

The **Telemetry** tab works locally without an Azure monitoring resource. Runs remain selected while new activity arrives, and expanded input/output panels preserve their scroll positions. Capture is bounded and session-local; export JSON before resetting or disconnecting if you want to keep it.

Terminal model logs include correlated call IDs, requests, responses, durations and errors with credentials redacted. Set `MATCHOS_MODEL_LOG_CONTENT=0` to retain operational metadata without prompt/response content; the production template does this by default.

To export traces to Foundry, install the optional dependencies, link an Application Insights resource to your Foundry project, and configure:

```sh
.venv/bin/python -m pip install -r requirements-observability.txt
```

```dotenv
MATCHOS_OTEL_ENABLED=1
APPLICATIONINSIGHTS_CONNECTION_STRING=YOUR-APPLICATION-INSIGHTS-CONNECTION-STRING
```

Cloud traces default to a compact view of evidence, viewer outcomes and model calls. Content export is off by default; `MATCHOS_TRACE_CONTENT=1` opts into sanitized payloads. See [tracing setup and diagnostics](evals/README.md#connect-cloud-tracing) for registration, detailed tracing and optional OTLP export.

## Evaluations and grading

Run the regression cases through the same evidence and viewer workflows as the app:

```sh
# Offline baseline, without cloud inference:
.venv/bin/python -m matchos.evaluation run --local
# Configured providers; incurs model inference charges:
.venv/bin/python -m matchos.evaluation run
```

Eight authored conditions across three viewers produce 24 rows, with actual outputs, checks, latency and fallback information. Reports go to Git-ignored `evals/results/`. Mechanical checks are useful regression tests; they do not establish overall editorial quality.

Foundry grading is a separate, explicit submission of saved outputs. It assesses editorial selection, persona quality, grounding and restraint without adding latency to live playback. See [evaluation setup, commands and limitations](evals/README.md).

## Deployment and development

[DEPLOYMENT.md](DEPLOYMENT.md) covers Azure Container Apps provisioning, managed identity, Key Vault, the shared password gate, smoke checks and rollback. Sessions currently live in one process; durable sessions and horizontal scaling require further work.

[ARCHITECTURE.md](ARCHITECTURE.md) explains the agents, Agent Framework workflows, data flow, services and implementation entry points.

Run the backend and browser logic tests with:

```sh
.venv/bin/python -m pytest
node --test tests/*.test.cjs
```

## License

Copyright © 2026 Shantanu Singh. All rights reserved. MatchOS is proprietary, source-available software. The [MatchOS Evaluation License](LICENSE) permits authorized hackathon judging and evaluation; other reuse requires prior written permission, subject to applicable law, GitHub's terms, and separately licensed third-party materials.
