# MatchOS · Two-minute judge demo

Format: 1920×1080, 120 seconds. Voice: local Kokoro af_nova, calm and conversational. No stock music; prioritize narration and readable proof. Style basis: DESIGN.md and the actual app capture. Never place login or credentials on the timeline.

## Asset audit
| Asset | Assignment | Role |
|---|---|---|
| capture/screenshots/press.png | 1, 2, 5 | Actual complete Azure app / shared evidence |
| capture/screenshots/press-broadcaster.png, press-fan.png, press-analyst.png | 2 | Three authentic views |
| capture/screenshots/goal.png and goal-*.png | 1, 3 | Goal and persona copy |
| capture/screenshots/card.png and card-*.png | 4 | Dismissal and player advantage |
| capture/screenshots/evidence.png | 5 | Source-event map |
| capture/screenshots/telemetry.png and telemetry-detail.png | 6 | Actual provider, latency, publication record |
| assets/press.mp4, goal.mp4, card.mp4, evidence.mp4, telemetry.mp4 | 1–6 | Real recorded UI, muted |
| assets/brand.png, persona-*.svg | Every beat | Actual captured wordmark / actual role symbols |
| assets/output-quotes.json | 2–4 | Exact generated excerpts, typeset for video readability |
| assets/fonts/*.ttf | Every beat | Captured brand font families |

## 01 · Hook · 0–8 seconds
Experience: the pitch is already visible, and a large gold “3” resolves into the three viewer labels. Brand top-left, large question on the left, real framed app on the right. MO mark and “One match. Every perspective.” resolve in order. Caption “Simulated match · actual app capture.”
Techniques: screen compositing, kinetic title, subtle screen push on a child wrapper. Depth: pitch-line motif, framed UI, title. Medium energy with expo.out title, power3.out frame, sine.out attribution. Hand off with a 0.45s scene-container crossfade; content remains visible.

## 02 · One moment, three perspectives · 8–32 seconds
Experience: start on a full app view for 4 seconds, then bring its three roles forward as readable columns. Use real captured headlines and exact body excerpts from output-quotes.json; label them “Actual generated output · 64:00”. Reveal broadcaster, fan, analyst in narration order, with neutral/club/tactical labels. Show “36% → 86%” as shared evidence, not three fabricated facts. Focal highlights move between columns with finite pulse, without hiding earlier cards.
Techniques: screen compositing, role cascade, shared-metric line drawing. Use separate card entrance and child emphasis wrappers. Colors: violet, red, gold accents within brand. Crossfade to next beat at the scene level.

## 03 · Goal · 32–42 seconds
Experience: the score changes to Liverpool 2–1 Manchester City at 65:04 over the real goal screen. Three short exact headlines appear under distinct role labels; the full neutral/broadcaster wording differs from the fan and analyst. Keep score and persona rows legible. Use the recorded goal clip and screenshot; no invented football footage.
Techniques: screen crop/scale, score emphasis, persona headline cascade. No claim that press breakdown caused this goal. Crossfade at end.

## 04 · Dismissal · 42–52 seconds
Experience: City’s red-card marker becomes a large red “10”; Liverpool remains “11”. Show the actual three output angles: “Manchester City down to 10”, “Liverpool have a player advantage”, “Dismissal: Manchester City”. Use real card capture at 68:01 and short exact lines. No invented formation or future consequence.
Techniques: role-aligned copy reveal, number emphasis, framed screen pan. Crossfade at end.

## 05 · Architecture · 52–74 seconds
Experience: the source-event map contracts to the left as a clear pipeline draws itself across the frame. Deterministic state → verified candidate pool → three parallel persona branches → Decision-1 → gpt-6-luna → production/copy guard → viewer cards. Microsoft Agent Framework is the orchestration bracket; Microsoft Foundry is the model-services bracket. Clarify that models choose from evidence and may choose NONE, and the pitch loop continues independently.
Techniques: connector drawing, sequential node reveal, finite signal travel. Use large labels and tiny amounts of code, never a page of text. Real evidence map as supporting asset. Crossfade at end.

## 06 · Inspect and evaluate · 74–92 seconds
Experience: real telemetry footage on the left, cropped to model names and call outcomes. On the right, a clean ladder exposes Inputs → Decision → Copy → Air / Hold. Switch the lower-right section to a grading workflow labeled “Saved-run grading · separate from live play”, displaying the four real grader names. Do not display invented scores, portal screenshots or a fictional completed cloud run.
Techniques: recorded UI compositing, trace-path drawing, rubric cascade. Label “Application Insights + Foundry traces”. Crossfade at end.

## 07 · Azure deployment · 92–106 seconds
Experience: an internet viewer connects through a small lock to Container Apps; a spoke diagram connects ACR, managed identity, Key Vault, Foundry, Speech and Application Insights. A small “Deployed” badge distinguishes this from the next scene. Lower rail: HTTPS / health probes / rollback. State one replica today in a quiet 22px note; no scale claim.
Techniques: connector reveal, node cascade, finite traffic pulse. No portal resource keys or credentials. Crossfade at end.

## 08 · Roadmap + close · 106–120 seconds
Experience: a clear “Next” label prevents confusion with shipped functionality. Three large staggered lines: licensed live feeds; deeper player and language experiences; shared state + reconnect + horizontal scale. A thin future feed path passes into a small real app screenshot. Resolve to brand, MO mark, and the closing phrase “One match. Every perspective.” Final scene alone may fade to the brand ground in the last 0.6s.
Techniques: roadmap progression, line drawing, brand title resolve. Opening and closing both use the product mark. Keep the end readable through 119s.

## Production tree
index.html orchestrates eight external composition clips and narration. DESIGN.md defines the exact look. SCRIPT.md and narration.txt hold the exact spoken text. transcript.json records audio-aligned timings. Each scene lives in compositions/beat-N.html and registers its own paused timeline. Actual captures and recordings live in capture/. Local reusable footage, icons, fonts and narration live in assets/.

## Measured narration

Local Kokoro `af_nova`, aligned to a 120.000-second WAV; short rests separate the scenes. `transcript.json` contains the ASR word timings.

| Scene | Scene window | Measured speech |
|---|---|---|
| 1 | 0–8s | 0.60–5.93s |
| 2 | 8–32s | 8.60–27.76s |
| 3 | 32–42s | 32.60–40.68s |
| 4 | 42–52s | 42.60–51.38s |
| 5 | 52–74s | 52.60–72.71s |
| 6 | 74–92s | 74.60–91.57s |
| 7 | 92–106s | 92.60–105.56s |
| 8 | 106–120s | 106.60–118.39s |

## Review notes

The full-frame comparison deliberately holds after the final reveal so viewers can read all three outputs. Small monospace labels and captured UI are secondary to the 28px+ comparison copy and large headlines. Cross-scene overlaps during the 0.45-second dissolves are intentional. No fabricated grading scores are shown: scene 6 presents the implemented saved-run submission workflow. The narration uses local TTS; Azure Speech is a feature of MatchOS itself.

