# MatchOS

Real-time personalized match intelligence on a synthetic football match.

Twenty-two players and a ball move on a 2D tactical board. The same feed — provider-neutral events plus 105×68m tracking — is what MatchOS reads. When the match changes in a way that can be proved, a verified moment is overlaid and then rephrased for whoever is watching.

## Critical path

Microsoft Agent Framework runs one workflow per live tick:

```text
state update (code)
  → trigger (code)
      → quiet end, or
      → one typed decision call
      → verifier (code)
      → producer (code)
      → fast template overlay
      → language agent (Gemini 3.8 Flash on Vertex AI when configured)
```

There is one decision call on the way to the overlay. The language model is a renderer. It only receives the verified moment, and any reply that introduces a number the moment does not contain is discarded.

The live feed sends frames every 100 ms independently of the model workflow. One background workflow runs at a time; feed events and tracking samples arriving during a model call are combined into one pending batch with the latest frame. Separate rolling state engines keep the UI current and the model's evidence consistent. Template overlays are delivered as soon as verification completes, with language enrichment arriving later. Expired moments and results for an old viewer profile are discarded. Viewer changes show a template immediately and enrich in the background; pause controls do not wait for model calls.

## Run

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m matchos
```

Open http://localhost:8000.

Use the same Python environment to install dependencies and start the app. If a reasoning dependency is missing, MatchOS logs the active Python path and uses the fact renderer. To run with another environment, install with that environment's `python -m pip install -r requirements.txt` before starting `python -m matchos`.

The default lab match is `press_collapse` with seed `47129` at 1×. Blue’s press starts high. From 55' the block drops and Red play through it. MatchOS is not told the scenario name.

## Viewer modes

Casual, analyst, home fan, player focus, and broadcaster all read the same verified facts. Only the framing changes. The Why control lists the stats and the underlying events.

## LLM configuration

The Python backend uses **LiteLLM** for provider-neutral reasoning completions and retains Microsoft Agent Framework for workflow orchestration. Decisions default to Cloudflare Workers AI's `@cf/cloudflare/clef-flash`. Native HTTP adapters handle Cloudflare's typed decision schema and Vercel's `/v1/decisions` schema; the verifier receives the same contract from either provider.

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

Without credentials for the selected decision provider, decisions use the local typed-probability model. Without a Vertex project, the default reasoning configuration uses the local fact renderer. Remote failures fall back to those same local implementations. Invalid or refused decision answers also fall back; non-finite or out-of-range probabilities cannot reach the verifier. The verifier and fact-faithfulness check still apply to remote results. The language agent only renders verified facts; there is no separate reasoning stage in this application.

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
```

### Playback and persona comparisons

In Watch, drag the match timeline (or focus it and use the arrow keys) to rewind
or jump ahead. The range runs from the selected scenario's starting minute to
95:00. Seeking preserves Pause/Resume and playback speed, reconstructs the same
seed's positions, score and rolling evidence, and clears insights from the old
position. Long jumps briefly show a rebuilding state; historical model calls are
skipped.

Compare personas is enabled by default with Home fan and Analyst side by side.
Choose either card's persona to compare other perspectives. Both cards use the
same verified moment and the Team, Language and Focus player settings in the
viewer controls. Templates appear immediately; each persona's language model
response arrives independently in the background. Comparison adds one language
request per selected view for each aired moment and does not duplicate decision
model calls. Turn off Compare personas to disable those additional requests.
