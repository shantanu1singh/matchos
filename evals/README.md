# Foundry tracing and evaluations

MatchOS keeps its local Telemetry tab and adds optional Azure export plus separate, repeatable quality evaluation. Running the app never starts paid grading automatically.

## Install

```sh
.venv/bin/python -m pip install -r requirements.txt -r requirements-observability.txt
```

The Foundry Projects SDK is pinned to 2.2.x to preserve OpenAI 2.x compatibility with the retained LiteLLM adapter. Azure Monitor's exporter is a preview package. This file and the `telemetry` / `evaluation` extras in `pyproject.toml` describe the same optional integrations.

## Connect cloud tracing

1. In Foundry, open your project and connect an Application Insights resource using Microsoft's [tracing setup](https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-setup). The local inference resource endpoint alone does not configure tracing.
2. In that Application Insights resource, copy **Connection String** from Overview. Add these values to the project-root `.env.local`, or your deployed app's environment variables:

   ```dotenv
   MATCHOS_OTEL_ENABLED=1
   MATCHOS_TRACE_MODE=compact
   APPLICATIONINSIGHTS_CONNECTION_STRING=InstrumentationKey=...;IngestionEndpoint=https://...
   MATCHOS_TRACE_CONTENT=1
   ```

3. Restart the backend and refresh the browser. Run a Judge demo checkpoint, open Telemetry, and copy its **Trace ID**. Find the matching trace in Foundry or Application Insights; ingestion can take a few minutes.

You should see shared state/trigger/candidate evidence, three independent viewer agent runs, Decision-1 selection spans, any language calls, fact guards and publication outcomes. Viewer spans carry `gen_ai.agent.id=matchos-broadcaster-v1`, `matchos-fan-v1`, or `matchos-analyst-v1`. Custom model spans include call IDs and response/error/fallback phases. Reported token counts are included when the provider returns them; the app does not invent missing token or cost data.

For agent-scoped traces, use **Build > Agents > New agent > Link external agent** in Foundry and register each persona with the exact OpenTelemetry ID above. The portal accepts alphanumeric characters and hyphens, so IDs use `-v1` rather than `:1`. Registration names may differ from these IDs. Use the same connected Application Insights resource. Restart MatchOS after updating the code and generate new traffic; historical spans retain their original IDs.

Trace context crosses the asynchronous candidate handoff, so the evidence and its viewers share a trace even when evidence processing has already finished. Local session/run IDs, persona and match timestamps provide additional correlation. Trace IDs are also present in Export JSON and evaluation outputs.

Content capture is optional. With `MATCHOS_TRACE_CONTENT` unset, cloud spans export metadata rather than prompts, facts or copy. Enabling it exports MatchOS-sanitized content, with credentials/connection strings redacted and each content field limited to 12,000 characters. GenAI message envelopes remain valid JSON even when their text is truncated. Native SDK sensitive-content capture stays off. The local inspector retains full sanitized payloads in memory. Full-dataset evaluation uses the saved untruncated outputs, rather than assuming every trace has complete content.

Cloud export defaults to `MATCHOS_TRACE_MODE=compact`. One triggered match evaluation groups deterministic state/trigger/check events into an **Evidence** span, with independent persona outcomes and model calls beneath the same workflow trace. Persona names show AIR, SUPPRESS or REJECT and the selected frame. Root attributes summarize score, match time, candidate IDs and publication. Verified facts are recorded once on Evidence; persona input/output summaries omit repeated full state and evidence. Actual sanitized model requests/responses remain available on each model call when content capture is enabled.

Routine quiet ticks and viewer reviews skipped without an editorial decision are not exported. Their cumulative `quiet_ticks` and `skipped_viewer_reviews` counters remain in local telemetry and are attached to subsequent exported capture spans. Errors, cancellations, actual model abstentions, and producer suppressions after selection remain visible. Framework plumbing spans are filtered before the cloud batch queue; retained spans explicitly reference retained MatchOS parents. This filtering applies to Application Insights and OTLP trace destinations. Compact export is trace-only.

Use `MATCHOS_TRACE_MODE=full` and restart to export the original framework internals, per-step spans and, when content capture is enabled, detailed stage payloads. The local Telemetry inspector and Export JSON retain detailed steps in either mode. Historical cloud traces are unchanged; the new mode affects new executions.

Exports use background batch processors; no exporter HTTP call runs on the playback loop. SDK offline storage is disabled, so failed deliveries are not a durable archive. On shutdown the app attempts a bounded flush in a thread. Missing optional packages or invalid configuration produce a warning and leave the local inspector available. Standard OpenTelemetry sampling can further control trace volume for longer-running deployments.

For collector-based export, keep `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`. This can replace or supplement Application Insights. Cloud export does not persist match sessions or enable reconnect recovery.

Example Application Insights query (classic tables):

```kusto
union requests, dependencies
| where operation_Id == "PASTE_TRACE_ID"
| project timestamp, name, duration, success, operation_Id, customDimensions
| order by timestamp asc
```

## Run the regression dataset

[`cases.json`](cases.json) defines eight authored conditions, evaluated for neutral Broadcaster, Liverpool fan and neutral Analyst: **24 rows**. The cases cover a pressing change, a goal, a dismissal, a quiet opening, recently aired updates, expired candidates, an already-aired goal and an already-aired dismissal. The simulator seed is 47129. The last four conditions deliberately configure history/freshness fixtures; they are not additional real match events.

```sh
# Offline baseline; forces both adapters local, regardless of .env.local:
.venv/bin/python -m matchos.evaluation run --local

# Configured providers (normally Decision-1 and gpt-6-luna); incurs inference charges:
.venv/bin/python -m matchos.evaluation run

# Smaller test, or comparison across model/prompt versions:
.venv/bin/python -m matchos.evaluation run --case goal --output evals/results/azure-goal.json
```

Each checkpoint is replayed once per run, without historical inference. Normal evidence verification builds the candidate pool; the required goal/press/card fixture must actually be present or the suite reports a setup error. Fresh perspective schedulers run the same Agent Framework viewer workflows as the app: selection, publication preflight, language, copy validation and publication commit. No separate evaluation-only editorial pipeline is used. The match clock stays fixed for reproducibility, so this suite measures wall-clock latency but does not simulate evidence aging during a moving match. Existing live freshness/cancellation tests cover that behavior.

Reports are written to ignored `evals/results/`:

| File | Contents |
|---|---|
| `latest.json` | Actual inputs, final outputs, checks, provider calls, timing, fallbacks and any trace IDs |
| `latest.md` | Readable summary, evaluated denominators and failed rows |
| `latest.jsonl` | Flat precomputed dataset for Foundry grading or portal import |

Exit codes: **0** all checks/expectations pass; **1** at least one fails; **2** setup or runner error. Provider fallback is reported and fails `provider_succeeded`, even if the fallback produces a useful card. A language fact-guard rejection is recorded separately as fallback telemetry. The offline baseline is labelled explicitly and is not a measurement of Azure model quality.

The checks verify candidate membership, verification checks, supported numbers, viewer profile, freshness, expected story category/publication, processing latency and unnecessary calls. Copy checks on held stories are **not applicable**, not passing examples of grounded prose. Number consistency cannot detect every incorrect association, invented qualitative claim or poor editorial decision.

Expectations are an authored starting rubric, **not human-reviewed benchmark labels**. Review and expand them before using the scores as product-quality claims. Model outputs can vary, and sensible persona agreement is valid. Do not force three different stories when the same goal is the most important event for everyone.

## Submit to Foundry cloud graders

Microsoft Foundry supports [custom graders](https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/azure-openai-graders) over [precomputed datasets](https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/cloud-evaluation-datasets). MatchOS submits saved actual outputs; it does not rerun the application or replace user-facing decisions.

Add:

```dotenv
AZURE_AI_PROJECT_ENDPOINT=https://YOUR-RESOURCE.services.ai.azure.com/api/projects/YOUR-PROJECT
MATCHOS_EVAL_JUDGE_DEPLOYMENT=YOUR-SUPPORTED-JUDGE-DEPLOYMENT
```

`AZURE_AI_PROJECT_ENDPOINT` is the **project URL**, different from the inference resource root in `AZURE_FOUNDRY_ENDPOINT`. The judge must be an available deployment that supports Foundry's model graders. You can use an existing compatible deployment, but a separate judge model/version reduces self-grading bias. Deployment availability and evaluation support depend on your region and account.

Project evaluations use **Microsoft Entra authentication**, independently of your inference API key. Run `az login` locally, or give the deployed/CI identity the appropriate [Foundry evaluation permissions](https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/evaluation-permissions). `DefaultAzureCredential` also supports managed identity and service principals. MatchOS does not create cloud resources or change role assignments.

```sh
.venv/bin/python -m matchos.evaluation submit evals/results/latest.json
# Or explicitly choose the judge:
.venv/bin/python -m matchos.evaluation submit evals/results/latest.json --judge-model YOUR-DEPLOYMENT

# Submission saves IDs immediately. Run this later to retrieve status/results:
.venv/bin/python -m matchos.evaluation status evals/results/latest.foundry.json
```

The Foundry run applies:

| Grader | What it assesses |
|---|---|
| `mechanical_checks` | Carries the local pass/fail result into the cloud run |
| `editorial_selection` | Selected story or NONE against viewer context and authored expectations |
| `persona_quality` | Neutral broadcast cue, evidence-led analysis or grounded supporter perspective |
| `semantic_grounding` | Factual support, unit/number associations and unsupported causal claims |
| `restraint` | Quiet play, repetition, freshness and justified interruptions |

Model graders score 0–1 with a pass threshold of 0.8. These are configurable hypotheses about quality, not calibrated truth probabilities. Held outcomes are graded for appropriate restraint; a high held-row score must not be presented as a measurement of generated-copy quality. All graders receive data as evidence rather than instructions. Judge-based grades can still be biased or wrong and need human spot checks.

Submission returns a queued/running run, not a passing grade. The status command saves the service's actual run response, paginated row-level scores/reasons when terminal, and the Foundry report URL. A model cannot grade errors into passing local checks. Failed/canceled cloud runs or completed runs with failed/errored rows return exit code 1; setup/auth errors return 2. Inspect `result_counts` and `output_items` for details.

Grading incurs additional model charges and runs separately from match playback. There is no implicit continuous grading or scheduled cloud job. The in-app telemetry and saved reports remain useful when no Foundry project or Application Insights resource is configured.
