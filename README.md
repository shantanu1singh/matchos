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
      → language agent (GPT-5.6 Luna when Foundry is configured)
```

There is one decision call on the way to the overlay. The language model is a renderer. It only receives the verified moment, and any reply that introduces a number the moment does not contain is discarded.

## Run

```bash
pip install -r requirements.txt
python -m matchos
```

Open http://localhost:8000.

The default lab match is `press_collapse` with seed `47129` at 1×. Blue’s press starts high. From 55' the block drops and Red play through it. MatchOS is not told the scenario name.

## Viewer modes

Casual, analyst, home fan, player focus, and broadcaster all read the same verified facts. Only the framing changes. The Why control lists the stats and the underlying events.

## Optional services

| Variable | Effect |
|---|---|
| `JEV_BASE_URL` | POST `/decide` with state deltas and the six typed questions. If the call fails, the local typed model is used. |
| `JEV_API_KEY` | Bearer token for that call. |
| `FOUNDRY_PROJECT_ENDPOINT` | Foundry project for the language agent. |
| `FOUNDRY_MODEL` | Deployment name. Defaults to `gpt-5.6-luna`. |

Without those variables the decision slot is a local typed-probability model and the language agent is a fact-locked renderer behind the same Agent Framework `AgentExecutor`. The workflow graph does not change.

## Contracts

`MatchEvent`, `TrackingFrame`, `MatchState`, `MomentDecision`, `VerifiedMoment`.

Events follow a Kloppy-style ontology in StatsBomb-like JSON. Tracking is continuous and measured in metres. A later licensed feed can replace the simulator without changing verification or presentation.

## Tests

```bash
python -m pytest
```
