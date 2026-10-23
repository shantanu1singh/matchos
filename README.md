# MatchOS

Real-time personalized match intelligence on a synthetic football match.

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

## Demo squads

The default views are neutral Analyst, Liverpool Home fan, and neutral Broadcaster. Analyst and Broadcaster have no team selector and show statistics for both sides. Team selections, pitch labels, evidence, and model prompts use the club and player identities in [`matchos/teams.py`](matchos/teams.py). The selected squads use [2026/27 Premier League shirt numbers](https://www.premierleague.com/en/news/4725682), checked on 23 October 2026. These are demo selections, not a real fixture or confirmed starting lineups; match events and player abilities are generated by the simulator.

`RED` maps to Liverpool and `BLUE` to Manchester City. Player IDs such as `red_7` are stable simulation slots, **not shirt numbers**. Always use the roster metadata for names and numbers; this also covers the substitute and keeps replay scripts stable.

The Producer tab is a read-only history of the primary viewer's production decisions: aired, suppressed, or rejected updates, model frame scores, and verification checks. Telemetry provides execution inputs and outputs across all viewers.

## LLM configuration

The Python backend uses **LiteLLM** for provider-neutral reasoning completions and retains Microsoft Agent Framework for workflow orchestration. Decisions default to Cloudflare Workers AI's `@cf/cloudflare/clef-flash`. Native HTTP adapters handle Cloudflare's typed decision schema and Vercel's `/v1/decisions` schema. Both select from the same verified candidate IDs, with an explicit `NONE` option.

| Variable | Effect |
|---|---|
| `DECISION_PROVIDER` | `cloudflare` (default), `vercel`, or `local`. |
| `CLOUDFLARE_ACCOUNT_ID` | Account ID for Workers AI REST calls. |
| `CLOUDFLARE_API_TOKEN` | Workers AI API token. `CLOUDFLARE_AUTH_TOKEN` is also accepted. |
| `CLOUDFLARE_MODEL` | Defaults to `@cf/cloudflare/clef-flash`; also accepts `@cf/cloudflare/clef`. |
| `AI_GATEWAY_API_KEY` | Gateway key when `DECISION_PROVIDER=vercel`. |
| `DECISION_MODEL` | Vercel decision model; defaults to `convaiinnovations/laya-free`. Set `convaiinnovations/laya` to use Laya, another Gateway decision model slug to swap models, or `local` for offline decisions. |
| `AI_GATEWAY_BASE_URL` | Decisions API base URL; defaults to `https://ai-gateway.vercel.sh/v1`. |
| `DECISION_TIMEOUT_S` | Decision request timeout in seconds; defaults to `10`. Frames continue during model waits. |
| `VERTEXAI_PROJECT` | Google Cloud project that enables Vertex reasoning. `GOOGLE_CLOUD_PROJECT` is also accepted. |
| `VERTEXAI_LOCATION` | Defaults to `global`; choose a location supported by your model. |
| `REASONING_MODEL` | LiteLLM provider/model identifier; defaults to `vertex_ai/gemini-3.8-flash`. Set `local` for offline rendering. |
| `REASONING_API_KEY` | Optional key for a replacement reasoning provider. Native provider credential environment variables also work through LiteLLM. |
| `REASONING_BASE_URL` | Optional API base URL for a replacement reasoning provider. |
| `REASONING_TIMEOUT_S` | Reasoning deadline in seconds; defaults to `15`, with no automatic retries. |

For Vertex, use Application Default Credentials (for local development, run `gcloud auth application-default login`), a workload identity, or `GOOGLE_APPLICATION_CREDENTIALS` pointing to your credential file. Enable the Vertex AI API and billing for the project.

```bash
export CLOUDFLARE_ACCOUNT_ID="your_account_id"
export CLOUDFLARE_API_TOKEN="your_workers_ai_token"
export VERTEXAI_PROJECT="your_gcp_project"
export VERTEXAI_LOCATION="global"
.venv/bin/python -m matchos
```

To swap the reasoning provider to an OpenAI-compatible service, set `REASONING_MODEL=openai/<model-id>`, `REASONING_BASE_URL`, and `REASONING_API_KEY`. Other LiteLLM provider prefixes (such as `anthropic/` or `bedrock/`) use that provider's credentials. These changes need no workflow edits.

Without credentials for the selected decision provider, decisions use the local typed-probability model. Without a Vertex project, the default reasoning configuration uses the local fact renderer. Remote failures fall back to those same local implementations. Invalid or refused decision answers also fall back; non-finite or out-of-range probabilities cannot reach the verifier. Candidates pass deterministic evidence checks before selection; the adapter rejects unknown candidate IDs and invalid probabilities, and language copy still passes the fact-faithfulness check. The language agent only renders verified facts; there is no separate reasoning stage in this application.

The old `JEV_*` and `FOUNDRY_*` settings have been replaced by the variables above. The app automatically loads `.env` and `.env.local` from the project root when reading LLM configuration, including when started through Uvicorn. Precedence is **shell environment > `.env.local` > `.env` > defaults**. Both secret files are ignored by Git. Restart the app after changing them.

To configure credentials in a file:

```bash
cp .env.example .env.local
# Edit .env.local with your Cloudflare credentials and Google Cloud project.
gcloud auth application-default login
.venv/bin/python -m matchos
```

For Cloudflare, open **Workers AI → Use REST API** in your dashboard and create a Workers AI API token for the selected account. The token needs **Workers AI Read** and **Workers AI Edit** permissions. Copy the Account ID into `CLOUDFLARE_ACCOUNT_ID` and the token into `CLOUDFLARE_API_TOKEN`. No Cloudflare Worker deployment or Gateway ID is required.

To switch back to Vercel, set `DECISION_PROVIDER=vercel`. Your existing `AI_GATEWAY_API_KEY` and `DECISION_MODEL` settings are retained and only used for that provider. Cloudflare credentials remain configured for switching back. Use `DECISION_PROVIDER=local` to force offline decisions. Restart after switching providers.

Provider-native credentials such as `GOOGLE_APPLICATION_CREDENTIALS` or `ANTHROPIC_API_KEY` can also be placed in these files.

If remote decisions fall back, the log includes the HTTP error or timeout reason. A `403` with `customer_verification_required` means Vercel requires a credit card on the Gateway account, even for the free model. Complete account verification in the Vercel dashboard. A `401` indicates a rejected API key; a timeout can be adjusted with `DECISION_TIMEOUT_S`.

Provider references: [Clef Flash API and schemas](https://developers.cloudflare.com/workers-ai/models/clef-flash/), [Cloudflare REST credentials](https://developers.cloudflare.com/workers-ai/get-started/rest-api/), [Vercel Decisions API](https://vercel.com/docs/ai-gateway/sdks-and-apis/openai-decisions), [Laya Free](https://vercel.com/ai-gateway/models/laya-free), [LiteLLM Vertex support](https://docs.litellm.ai/docs/providers/vertex), [Gemini 3.8 Flash](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/3-8-flash).

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

The shared pitch fills roughly two-thirds of the desktop view; three compact viewer cards stack on the right. Neutral Analyst, Liverpool Home fan and neutral Broadcaster open by default. Each card has its own persona/team/language/player settings in the ellipsis popover. Remove an extra card there or use the plus button to add it back.

Every viewer independently selects a candidate or `NONE` from the same verified pool, with separate recent-update history and producer rules. Each card may therefore show a different story, the same story, or no interruption. A slow selection or language response never blocks another viewer or the pitch. Each worker has one pending snapshot; a busy worker coalesces newer snapshots instead of cancelling its current request or queuing every frame. Removing/changing a perspective cancels its outdated work; unchanged perspectives keep their history.

**Why this decision?** shows the chosen story, evidence summaries, and candidates held back. Model preferences are labelled separately from factual checks, producer policy and local fallback selection. The last aired story stays visible until a new finished story replaces it or the viewer/match is reset. After its live validity window it is labelled Last aired; this does not make expired candidates eligible to air again. Later suppressions appear in a separate row below the message with their reason and match time, while Facts & evidence stays attached to the aired story. No candidate is invented to force visual differences. Each extra viewer adds its own decision call per processed candidate pool, plus a language call only when its producer airs a story.

Each viewer card shows live stat capsules for its persona and selected team or
player. **Facts & evidence** opens the exact facts, source events and verification
checks for its displayed moment; **Raw verified data** exposes the moment and
rendered overlay as JSON. These facts stay tied to the update while live capsules
continue tracking the match.

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

Capture stays in memory for each WebSocket session: the last 40 meaningful runs and the latest quiet trigger evaluation. Resetting or seeking starts a fresh trace session. The inspector polls once per second only while visible; capturing does not wait for model or telemetry network calls. State inputs include the frame, events, total sample count, and latest 30 tracking samples; the full aggregated deterministic state is the output. Credentials are redacted using the same sanitizer as terminal model logs.

Possession and press bypass facts sent to the language model use percentages (`64%`), while canonical evidence retains fractions (`0.639`). Distances keep metres and counts stay integers. A fractional possession/bypass response is rejected in favor of verified template copy.

For persistent external telemetry, install the optional OpenTelemetry dependencies:

```sh
.venv/bin/python -m pip install -e '.[telemetry]'
```

Then configure your collector in `.env.local` and restart:

```dotenv
MATCHOS_OTEL_ENABLED=1
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```

This uses Microsoft Agent Framework's native OpenTelemetry instrumentation with MatchOS parent spans and stage events. Sensitive prompt/response capture is disabled for external export; full sanitized inputs/outputs remain in the local inspector. External collection is optional; the built-in view needs no account or extra package.

### Editorial selection

After a trigger, the verifier checks every supported story category and builds evidence-backed candidates. The decision model receives those candidate IDs, verified facts/checks, viewer persona and team/player preferences, match time/score, trigger reasons, and the last ten aired updates with elapsed time. It chooses which candidate is most worth showing now, or `NONE`, and supplies editorial importance and interruption probability. It does not decide whether possession or bypass thresholds were crossed.

The producer retains its novelty, relevance, cadence, and on-air rules after model selection. It may suppress the selected candidate but does not substitute a different story. Model importance and selection confidence affect presentation priority; selection confidence is an editorial model score, not a probability that verified facts are true. Viewer cards share candidate verification and perform separate editorial selections. Telemetry includes the entire candidate set and the editorial selection input/output.

Cloudflare remains the default, Vercel remains available, and existing local/offline failure handling is retained, restricted to verified candidates. No new selectable deterministic editorial branch is introduced.
