# MatchOS

Real-time personalized match intelligence on a synthetic football match.

**License:** Copyright © 2026 Shantanu Singh. All rights reserved. MatchOS is
proprietary, source-available software. The [MatchOS Evaluation License](LICENSE)
permits authorized hackathon judging and evaluation; other reuse requires prior
written permission, subject to applicable law, GitHub's terms, and separately
licensed third-party materials.

Liverpool and Manchester City play a simulated match. Twenty-two players and a ball move on a 2D tactical board. The same feed — provider-neutral events plus 105×68m tracking — is what MatchOS reads. When the match changes in a way that can be proved, a verified moment is overlaid and then rephrased for whoever is watching.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the system design, Mermaid diagrams, and component responsibilities.

## Critical path

Microsoft Agent Framework runs one workflow per live tick:

```text
state update (code)
  → trigger (code)
      → quiet end, or
      → verified candidates (code)
      → one typed editorial selection call
      → producer (code)
      → language agent (Gemini 3.8 Flash on Vertex AI when configured)
      → validated persona text or fallback
      → publish one finished card
```

When evidence produces candidates, each configured viewer makes an independent decision call that chooses a verified candidate or `NONE`. Triggers without a supported candidate skip the model call. The language model is a renderer. It only receives the verified moment, and any reply that introduces a number the moment does not contain is discarded.

The live feed sends frames every 100 ms independently of the model workflow. One shared evidence workflow runs at a time; separate background workers select and render for each viewer. Feed events and tracking samples arriving during evidence processing are combined into one pending batch with the latest frame. Separate rolling state engines keep the UI current and the model's evidence consistent. Each viewer publishes one finished card after its selected story clears production rules and its language response passes validation. The last aired card stays visible while the next response is prepared; no intermediate template is sent. Failures or the configured reasoning deadline produce one persona-specific fallback. Air-time and recent-story history start only when the final card is ready. Expired moments and results for an old viewer profile are discarded. Viewer changes cancel only the changed perspective and select again from the latest verified pool; pause controls do not wait for model calls.

## Run

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m matchos
```

Open http://localhost:8000.

Use the same Python environment to install dependencies and start the app. If a reasoning dependency is missing, MatchOS logs the active Python path and uses the fact renderer. To run with another environment, install with that environment's `python -m pip install -r requirements.txt` before starting `python -m matchos`.

The default lab match is `press_collapse` with seed `47129` at 1×. Manchester City’s press starts high. From 55' the block drops and Liverpool play through it. The same default lab also includes these synthetic turning points:

| Match time (default seed) | Feed event | Match effect |
|---|---|---|
| 65:03 | Alexander Isak scores after a visible setup pass | Liverpool lead 2–1 |
| 68:00 | Mateo Kovacic receives a red card | City have ten players and sit deeper/narrower |

The goal is staged from 65' and its exact second can vary with a different seed. These are simulated events, not claims about a real fixture. They enter the normal feed, rolling state, evidence checks, decision calls and persona rendering; scenario labels are not sent to the models. The scoreboard shows red-card counts, and each persona's live stat capsules show the current player advantage after a dismissal. Analyst copy explains the score/player-count context, the Liverpool fan view frames the advantage for their team, and Broadcaster gives a neutral event headline.

Routine candidates have a 40-second **match-time** validity window; major goal/dismissal stories have 120 seconds, anchored to the actual event. At 20× these windows are only two and six real seconds. Decision/language latency can therefore make a story expire before publication; the old aired card remains, and the held notice explains which wait exceeded the window. Slow remote models are easier to watch at 1× or 2×. Already stale/covered stories skip model calls. Major evidence remains discoverable in the rolling feed for 60 match seconds and is deduplicated by source event ID for each viewer, so a busy worker can recover a recent event without airing it repeatedly.

## Viewer modes

Casual, analyst, home fan, player focus, and broadcaster all read the same verified facts. Each viewer can choose a different story or no interruption. The same immutable candidate pool supplies all viewers; the model is not forced to disagree. Why this decision explains selection and suppression, while Facts & evidence exposes the exact underlying checks and events.

Analyst and Broadcaster model inputs omit team allegiance and inactive player focus. Fan inputs retain the supported team; Player inputs retain the focused player. The local fallback also uses persona preferences, recent-story history and interruption cadence, and can choose `NONE`. Its scores are editorial heuristics, not calibrated model probabilities. Different personas may still choose the same supported story.

Press-breakdown candidates require a bypass increase of at least 25 percentage points on at least five attempts, corroborated by either a defensive-line retreat of at least 5m or at least five additional final-third entries by the opponent. A higher counterattack bypass ratio alone does not establish a collapsing press. These checks apply equally to both clubs. The **Press beaten** capsules describe the named team's defending press being bypassed, not that team's attacking success.

Broadcaster asks what the audience should notice right now: goals, dismissals and clear new match developments take priority over routine metric changes. Its card has a short, score-aware headline built from verified facts and separate model-written supporting copy (one or two sentences, at most 45 words, focusing on one statistic or comparison). Broadcaster model inputs round distances to whole metres; Analyst inputs, canonical facts and detailed evidence keep their precision. Repeated headlines, excessive length, decimal-distance copy and unsupported numbers fall back to the broadcast template. The generated text never replaces the headline or the production graphic's title.

## Spoken commentary

Each persona card has a **Listen** button. Enable **Auto commentary** and choose one perspective to speak its new aired stories. Audio is off by default. The speech path reads only the completed, verified, producer-approved card text; it makes no new editorial or language call and does not speak held stories.

Add these values to `.env.local` (or the deployed app's environment) and restart:

```dotenv
MATCHOS_SPEECH_ENABLED=1
AZURE_SPEECH_KEY=YOUR_SPEECH_RESOURCE_KEY
AZURE_SPEECH_REGION=YOUR_SPEECH_RESOURCE_REGION
AZURE_SPEECH_MODEL=MAI-Voice-2.1-Flash
```

In Azure, use the key and region from a Speech-capable resource's **Keys and Endpoint** page. These are separate settings from the inference endpoint/key; a Foundry model deployment alone does not configure Speech. No extra Python package is needed: the adapter calls the [Azure Speech REST endpoint](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/rest-text-to-speech) with async HTTP and SSML. See [MAI voice availability and styles](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/mai-voices) for your region. MAI-Voice-2.1-Flash is in preview.

| Perspective | English / Hindi voice | Delivery |
|---|---|---|
| Broadcaster | `en-GB-Emily` / `hi-IN-Dhruv` | Composed narration; excited for a goal or dismissal |
| Analyst | `en-US-Grant` / `hi-IN-Harper` | Educational style, slightly slower |
| Fan | `en-GB-Emily` / `hi-IN-Priya` | Warm and joyful for their team's advantage; concerned for setbacks |

Delivery uses supported SSML styles and speaking rates, rather than adding guidance to the spoken words. Full voice IDs append `:MAI-Voice-2.1-Flash`; optional persona/language overrides are listed in `.env.example`. Overridden voices must support the mapped styles. Original prebuilt voices are used; this feature does not clone a commentator.

Only one perspective speaks at a time. Selecting another view stops the previous audio. New selected-view updates supersede older requests; automatic audio is checked for freshness before playback. Seeking, resetting, changing the active profile, disconnecting or moving to another Judge demo checkpoint aborts the browser fetch and stops playback. Manual **Listen** can replay a retained **Last aired** card. If browser autoplay is blocked, press Listen explicitly.

Synthesis starts only on Listen/auto playback, never for every card or frame. Identical text, voice, language and delivery share audio within the server process. Requests for the same clip share one synthesis job; already-running jobs can finish into the cache after a browser aborts, but their stale results never play. The cache retains at most 128 clips / 16 MiB for up to one hour, and the approved-story registry retains at most 256 entries. Concurrent synthesis is limited to two jobs, with at most four pending/in-progress unique clips and a 12-second total deadline (`SPEECH_TIMEOUT_S`). Cache eviction or a server restart can require synthesis again.

Speech requests, delivery, errors and cache hits appear in terminal diagnostics and the Telemetry tab, correlated with the source viewer trace when cloud tracing is enabled. Raw audio and credentials are not logged. Missing Speech settings disable the controls with an explanation; failures leave pitch playback and cards usable.

## Demo squads

The default views are neutral Broadcaster (primary), Liverpool Home fan, and neutral Analyst. Analyst and Broadcaster have no team selector and show statistics for both sides. Team selections, pitch labels, evidence, and model prompts use the club and player identities in [`matchos/teams.py`](matchos/teams.py). The selected squads use [2026/27 Premier League shirt numbers](https://www.premierleague.com/en/news/4725682), checked on 23 October 2026. These are demo selections, not a real fixture or confirmed starting lineups; match events and player abilities are generated by the simulator.

`RED` maps to Liverpool and `BLUE` to Manchester City. Player IDs such as `red_7` are stable simulation slots, **not shirt numbers**. Always use the roster metadata for names and numbers; this also covers the substitute and keeps replay scripts stable.

The Producer tab is a read-only history of the primary viewer's production decisions (Broadcaster by default): aired, suppressed, or rejected updates, model frame scores, and verification checks. Telemetry provides execution inputs and outputs across all viewers.

## LLM configuration

The default providers are **Azure Foundry `Decision-1`** for editorial selection and **`gpt-6-luna`** for persona language. **Microsoft Agent Framework** runs the workflow and calls the generative deployment through its official `OpenAIChatClient` (Responses API). Decision-1 uses Foundry's native typed `/providers/microsoft/v1/systemone` API through Microsoft's **Azure Core async SDK**; it is not sent to chat completions. All decision providers select from the same verified candidate IDs, with an explicit `NONE` option.

Cloudflare Clef, Vercel Laya and the LiteLLM/Vertex adapter remain available. All model calls are asynchronous, have overall deadlines and no automatic SDK retries, and retain correlated terminal logs and execution telemetry. Missing configuration or remote errors use the existing local fallback; startup/session logs identify the active providers.

Install the Microsoft provider dependencies into the interpreter that runs the app:

```bash
.venv/bin/python -m pip install -r requirements.txt
```

Add these settings to the project-root `.env.local` (merge them into an existing file to preserve old credentials):

```dotenv
DECISION_PROVIDER=azure
REASONING_PROVIDER=azure
AZURE_FOUNDRY_ENDPOINT=https://YOUR-RESOURCE.services.ai.azure.com
AZURE_FOUNDRY_API_KEY=YOUR-RESOURCE-KEY
AZURE_AUTH_MODE=key
AZURE_DECISION_DEPLOYMENT=Decision-1
AZURE_REASONING_DEPLOYMENT=gpt-6-luna
```

In Foundry, use each deployment's **Get code / endpoint and samples** panel to find the resource endpoint and key. Use the HTTPS resource root, such as `https://RESOURCE.services.ai.azure.com` or `https://RESOURCE.openai.azure.com`; omit project paths, `/openai/v1`, and `/providers/microsoft/v1/systemone`. Deployment names are case sensitive. The code appends the appropriate API path. An Azure subscription ID, resource group, and Foundry project ID are not needed for inference.

If the deployments are on different resources, set `AZURE_DECISION_ENDPOINT` / `AZURE_DECISION_API_KEY` and `AZURE_REASONING_ENDPOINT` / `AZURE_REASONING_API_KEY`. Each override falls back to the shared setting above when absent.

For **Microsoft Entra authentication**, set `AZURE_AUTH_MODE=entra`, omit API keys and run `az login` locally with an identity permitted to call both deployments. The async `DefaultAzureCredential` also supports managed identity and service principals (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`). Tokens are acquired and refreshed by the Azure SDK. An expired or unauthorized identity falls back locally with an error log.

| Variable | Effect |
|---|---|
| `DECISION_PROVIDER` | `azure` (default), `cloudflare`, `vercel`, or `local`. |
| `REASONING_PROVIDER` | `azure` (default), `litellm`, or `local`. |
| `AZURE_FOUNDRY_ENDPOINT` | Shared HTTPS Azure resource root. |
| `AZURE_FOUNDRY_API_KEY` | Shared resource API key; used only with `AZURE_AUTH_MODE=key`. |
| `AZURE_AUTH_MODE` | `key` (default) or `entra`. |
| `AZURE_DECISION_DEPLOYMENT` | Exact decision deployment name; defaults to `Decision-1`. |
| `AZURE_REASONING_DEPLOYMENT` | Exact generative deployment name; defaults to `gpt-6-luna`. |
| `AZURE_DECISION_ENDPOINT`, `AZURE_DECISION_API_KEY` | Optional decision resource overrides. |
| `AZURE_REASONING_ENDPOINT`, `AZURE_REASONING_API_KEY` | Optional generative resource overrides. |
| `AZURE_OPENAI_API_VERSION` | Agent Framework's Azure Responses API version; defaults to `preview`. Does not affect the Decision-1 endpoint. |
| `DECISION_TIMEOUT_S` | Overall decision deadline; defaults to `10` seconds. |
| `REASONING_TIMEOUT_S` | Overall reasoning deadline; defaults to `15` seconds. |
| `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` | Credentials when `DECISION_PROVIDER=cloudflare`. `CLOUDFLARE_AUTH_TOKEN` is also accepted. |
| `CLOUDFLARE_MODEL` | Defaults to `@cf/cloudflare/clef-flash`; also accepts `@cf/cloudflare/clef`. |
| `AI_GATEWAY_API_KEY` | Gateway key when `DECISION_PROVIDER=vercel`. |
| `DECISION_MODEL` | Vercel decision model; defaults to `convaiinnovations/laya-free`. |
| `AI_GATEWAY_BASE_URL` | Vercel Decisions base URL; defaults to `https://ai-gateway.vercel.sh/v1`. |
| `VERTEXAI_PROJECT`, `VERTEXAI_LOCATION` | Vertex project and location (`global` by default); `GOOGLE_CLOUD_PROJECT` is accepted. |
| `REASONING_MODEL` | Model when `REASONING_PROVIDER=litellm`; retained default `vertex_ai/gemini-3.8-flash`. `local` forces offline rendering for either provider. Azure uses `AZURE_REASONING_DEPLOYMENT`. |
| `REASONING_API_KEY`, `REASONING_BASE_URL` | Optional replacement LiteLLM provider credentials and endpoint. |

To switch back, set `DECISION_PROVIDER=cloudflare` or `vercel`, and/or `REASONING_PROVIDER=litellm`. Existing Cloudflare, Gateway, and Vertex credentials stay in place. Vertex uses Application Default Credentials (`gcloud auth application-default login` locally), workload identity, or `GOOGLE_APPLICATION_CREDENTIALS`. Other LiteLLM provider prefixes still work through `REASONING_MODEL`, e.g. `openai/<model-id>` with `REASONING_BASE_URL` and `REASONING_API_KEY`. Use `DECISION_PROVIDER=local` and `REASONING_PROVIDER=local` for offline operation.

The app automatically loads `.env` and `.env.local` from the project root, including when started through Uvicorn. Precedence is **shell environment > `.env.local` > `.env` > defaults**. Both secret files are ignored by Git. Explicit provider values in an existing file override the new Azure defaults; update those lines as shown above. Restart the app after changing settings.

```bash
.venv/bin/python -m matchos
```

Candidates pass evidence checks before selection. Unknown candidate IDs, refused responses, and non-finite or out-of-range probabilities cannot reach publication. The language agent only renders verified facts and its output still passes the fact-faithfulness check. A `401` indicates rejected credentials, `403` indicates missing access, `404` suggests the wrong resource/path or deployment, and a timeout can be adjusted with the deadline variables above. Unauthenticated deployments have not been live-tested; contract tests mock only network and credential boundaries.

Provider references: [Microsoft Decision-1 API and authentication](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision), [Microsoft Agent Framework Azure client migration](https://learn.microsoft.com/en-us/agent-framework/support/upgrade/python-2026-significant-changes), [Cloudflare Clef Flash](https://developers.cloudflare.com/workers-ai/models/clef-flash/), [Vercel Decisions API](https://vercel.com/docs/ai-gateway/sdks-and-apis/openai-decisions), [LiteLLM Vertex support](https://docs.litellm.ai/docs/providers/vertex).

## Contracts

`MatchEvent`, `TrackingFrame`, `MatchState`, `MomentDecision`, `VerifiedMoment`.

Events follow a Kloppy-style ontology in StatsBomb-like JSON. Tracking is continuous and measured in metres. A later licensed feed can replace the simulator without changing verification or presentation.

## Tests

```bash
.venv/bin/python -m pytest
node --test tests/telemetry-ui.test.cjs tests/viewer-cards.test.cjs
```

### Playback and persona comparisons

In Watch, drag the match timeline (or focus it and use the arrow keys) to rewind
or jump ahead. The range runs from the selected scenario's starting minute to
95:00. Seeking preserves Pause/Resume and playback speed, reconstructs the same
seed's positions, score and rolling evidence, and clears insights from the old
position. Long jumps briefly show a rebuilding state; historical model calls are
skipped.

Playback controls float over the pitch. While dragging the timeline, a preview
above the thumb shows the time playback will resume from.

The shared pitch fills roughly two-thirds of the desktop view; three compact viewer cards stack on the right. Neutral Broadcaster, Liverpool Home fan and neutral Analyst open by default; Broadcaster is the primary viewer. Each card has its own persona/team/language/player settings in the ellipsis popover. Remove an extra card there or use the plus button to add it back.

Every viewer independently selects a candidate or `NONE` from the same verified pool, with separate recent-update history and producer rules. Each card may therefore show a different story, the same story, or no interruption. A slow selection or language response never blocks another viewer or the pitch. Each worker has one pending snapshot; a busy worker coalesces newer snapshots instead of cancelling its current request or queuing every frame. Removing/changing a perspective cancels its outdated work; unchanged perspectives keep their history.

**Why this decision?** shows the chosen story, evidence summaries, and candidates held back. Model preferences are labelled separately from factual checks, producer policy and local fallback selection. The last aired story stays visible until a new finished story replaces it or the viewer/match is reset. After its live validity window it is labelled Last aired; this does not make expired candidates eligible to air again. Later suppressions appear in a separate row below the message with their reason and match time, while Facts & evidence stays attached to the aired story. No candidate is invented to force visual differences. Each extra viewer adds its own decision call per processed candidate pool, plus a language call only when its producer airs a story.

Each viewer card shows live stat capsules for its persona and selected team or
player. **Facts & evidence** opens the exact facts, source events and verification
checks for its displayed moment; **Raw verified data** exposes the moment and
rendered overlay as JSON. These facts stay tied to the update while live capsules
continue tracking the match.

### Guided judge demo

Restart the Python backend after updating the app, then refresh the browser. The UI can load new assets while an old server process is still running; Judge demo shows a restart instruction if that process does not support checkpoints.

Click **Judge demo** to review the default press-collapse match at three real checkpoints: City's pressing change at **64:00**, Liverpool's goal at **65:04**, and City's red card at **68:01**. The demo uses seed 47129 and the default neutral Broadcaster, Liverpool fan and neutral Analyst. It replays deterministic history without historical model calls, freezes the match clock, then requests a review through the normal Agent Framework evidence workflow and independent viewer workers. The selected providers make fresh decisions; local fallbacks still apply when configured providers fail or are unavailable.

The guide shows each viewer's actual aired or held outcome. **Next checkpoint** waits until all three workers finish their decisions and any required language rendering. A held story is a valid outcome; the demo does not manufacture disagreement. Checkpoints can be retried or revisited, and their execution traces stay together in Telemetry. Each checkpoint starts fresh editorial history. **Exit demo**, **Finish demo**, and **Reset** return to a regular replay from the start. Demo preparation and model waits run in background tasks; starting another match cancels outdated work.

### Visual evidence

Click **View evidence on pitch** on any aired card, or choose a source event inside **Facts & evidence**. The pitch switches to a labelled historical event map: numbered paths come only from the source events' recorded locations. Current players and the live ball are hidden while the map is open. Select an event to focus its path, or **All paths** to compare them. The close button returns to the current match; live playback can continue behind the map.

Press and momentum maps include a before/after comparison from that story's verified facts. Goals show the score change, and dismissals show the player counts. Aggregate stories with no event paths say so instead of inferring player movement. Raw facts and evidence retain their precision.

Model diagnostics print to the terminal by default for both cloud and local
providers. Correlated request IDs connect input payloads to responses, elapsed
time, errors, cancellations and fallbacks. Credentials and authorization fields
are redacted; logs include match facts and generated text.
Diagnostics go to stdout and flush after each record. Startup prints `MODEL
Logging enabled` with the active Python path; starting a match prints its decision
and reasoning providers. If the startup banner is missing, stop the previous
server and restart with `.venv/bin/python -u -m matchos`. Request logs appear
when live match triggers fire; warmup and timeline replay skip historical calls.

### Execution telemetry

Open **Telemetry** in the app to inspect live workflow runs, deterministic state and triggers, decision calls, verifier checks, producer decisions, and language calls. Expand a stage for input/output JSON; provider requests and responses share a call ID. Selected runs stay pinned while the match advances, and open panels retain their scroll positions through live updates. Fallbacks, rejected language, cancellation, and timing appear in the same run. Comparison personas and viewer changes have separate runs. **Export JSON** downloads the captured session history.

Each selected run also shows a compact summary of the decision model, language model, call durations, selected story, publication outcome and total duration. Calls that were skipped, local fallback use, incomplete evaluations and quiet ticks are labelled explicitly. The detailed stages and payloads remain below this summary.

Capture stays in memory for each WebSocket session: the last 40 meaningful runs and the latest quiet trigger evaluation. Resetting or seeking starts a fresh trace session. The inspector polls once per second only while visible; capturing does not wait for model or telemetry network calls. State inputs include the frame, events, total sample count, and latest 30 tracking samples; the full aggregated deterministic state is the output. Credentials are redacted using the same sanitizer as terminal model logs.

Possession and press bypass facts sent to the language model use percentages (`64%`), while canonical evidence retains fractions (`0.639`). Distances keep metres and counts stay integers. A fractional possession/bypass response is rejected in favor of verified template copy.

For Foundry tracing and grading, install the optional SDK dependencies:

```sh
.venv/bin/python -m pip install -r requirements-observability.txt
```

Connect an Application Insights resource to your Foundry project, then add its connection string to `.env.local` and restart:

```dotenv
MATCHOS_OTEL_ENABLED=1
APPLICATIONINSIGHTS_CONNECTION_STRING=YOUR_APPLICATION_INSIGHTS_CONNECTION_STRING
# Opt in to sanitized deterministic inputs, model requests/responses and viewer copy:
MATCHOS_TRACE_CONTENT=1
```

This exports through Microsoft Agent Framework's OpenTelemetry setup and Azure Monitor's background trace exporter. Shared evidence, independent viewer agents, deterministic stages and custom Decision-1 calls are correlated. Provider spans include response/error/fallback phases and token usage when returned. The Telemetry tab and exported JSON expose trace IDs for lookup in Application Insights/Foundry. Pending calls close on cancellation; shutdown flushes queued spans off the event loop.

Cloud traces default to `MATCHOS_TRACE_MODE=compact`: one meaningful match evaluation, grouped Evidence, independent persona outcomes and model calls. Quiet ticks and skipped viewer reviews are counted rather than exported; SDK plumbing spans are filtered. Use `MATCHOS_TRACE_MODE=full` and restart for the detailed cloud export. Local Telemetry and Export JSON retain their full steps in both modes.

Content export is off by default. `MATCHOS_TRACE_CONTENT=1` exports MatchOS-sanitized payloads, bounded to 12,000 characters per content field; native SDK sensitive logging stays disabled. Compact mode keeps model requests/responses and final copy while replacing repeated deterministic state with evidence and outcome summaries. Credentials and connection strings are redacted. Full local payloads remain available in the inspector. An OTLP collector is still supported with `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_PROTOCOL`, instead of or alongside Azure Monitor. Export is optional and doesn't make local session state persistent. See [Foundry setup and evaluations](evals/README.md).

### Evaluations and grading

Run eight deterministic match/history conditions across three viewers (24 rows) through the actual perspective workers:

```sh
# Offline regression baseline, no paid model inference:
.venv/bin/python -m matchos.evaluation run --local
# Use your configured providers, including Azure Decision-1 and language deployment:
.venv/bin/python -m matchos.evaluation run
# Or a small cloud smoke test:
.venv/bin/python -m matchos.evaluation run --case goal --output evals/results/azure-goal.json
```

Reports include actual outputs, mechanical checks, authored editorial expectations, latency and fallbacks. JSON, Markdown and Foundry-ready JSONL are saved under ignored `evals/results/`. Failed checks produce exit code 1; setup errors produce 2. These fixtures are an authored regression rubric, not a human-reviewed benchmark. Passing the offline baseline does not establish cloud model quality.

For separate Foundry cloud grading, set the **project** endpoint and a supported judge deployment, run `az login`, then submit saved outputs:

```dotenv
AZURE_AI_PROJECT_ENDPOINT=https://YOUR-RESOURCE.services.ai.azure.com/api/projects/YOUR-PROJECT
MATCHOS_EVAL_JUDGE_DEPLOYMENT=YOUR-JUDGE-DEPLOYMENT
```

```sh
.venv/bin/python -m matchos.evaluation submit evals/results/latest.json
.venv/bin/python -m matchos.evaluation status evals/results/latest.foundry.json
```

Foundry runs four model graders—editorial selection, persona quality, semantic grounding and restraint—plus the mechanical-check result. Submission is asynchronous, uses the Foundry Projects SDK with Entra authentication, and makes billable judge calls. It never reruns inference or adds grading latency to live play. Fetch status again later for completion, per-row scores/reasons and the Foundry report URL. See [the full setup and limitations](evals/README.md).

### Editorial selection

After a trigger, the verifier checks every supported story category and builds evidence-backed candidates. The decision model receives those candidate IDs, verified facts/checks, viewer persona and team/player preferences, match time/score, trigger reasons, and the last ten aired updates with elapsed time. It chooses which candidate is most worth showing now, or `NONE`, and supplies editorial importance and interruption probability. It does not decide whether possession or bypass thresholds were crossed.

The producer retains its novelty, relevance, cadence, and on-air rules after model selection. It may suppress the selected candidate but does not substitute a different story. Model importance and selection confidence affect presentation priority; selection confidence is an editorial model score, not a probability that verified facts are true. Viewer cards share candidate verification and perform separate editorial selections. Telemetry includes the entire candidate set and the editorial selection input/output.

Decision inputs include whether this viewer has received a story, wall-clock waiting time, and time since the last air. Before the broadcaster's first story, its guidance allows useful opening context backed by a meaningful development (such as sustained possession plus increased final-third entries). Later stories must add something new. The model can still choose `NONE`; publication and cadence checks remain in place. Held-story messages show the publication score or broadcaster importance threshold when either blocks an update.

Azure Foundry is the default, Cloudflare and Vercel remain available, and existing local/offline failure handling is retained, restricted to verified candidates. No new selectable deterministic editorial branch is introduced.
