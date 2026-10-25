# MatchOS product walkthrough cut

Two-minute narrated alternate cut focused on the real product. The original designed demo remains in `../matchos-demo`.

The opening **52 seconds are one continuous 1x browser recording**: show all four language options, click Judge demo, wait for actual model responses, compare three real persona cards, open the selection explanation, inspect facts, and display source events on the pitch. The first approximately 9 seconds after launch remain in the recording as the real model wait. No screenshots or reconstructed cards replace the product in this section. One camera push enlarges the sidebar, then returns to the complete app.

In this recorded run, the broadcaster and analyst selected press breakdown; the Liverpool fan selected momentum in Spanish. Architecture, telemetry/grading, Azure deployment and roadmap follow. Telemetry is labeled as a separate actual captured run. No cloud grading scores are fabricated.

```sh
npx hyperframes@0.8.192 preview --background --port 3018
npm run check
```

Studio: `http://localhost:3018/#project/matchos-product-demo`.

`SCRIPT.md` contains the revised narration. `STORYBOARD.md` and `SPEC.md` describe timing and camera choices. `assets/walkthrough-provenance.json` records actual interaction timings; `assets/output-quotes.json` records the selected outputs. Raw takes and diagnostic JSON stay ignored. `tools/narrate_azure.py` regenerates narration using the authored `narration-plan.json`; `tools/narrate.py` is a local fallback. Capture is a simulated match in the deployed app. The pointer is a recording annotation following actual clicks; no product data or model output is modified.

Microsoft en-GB-Emily MAI-Voice-2.1 narration in narrator style, approximately -16 LUFS, 120-second WAV, no background music. The separate Spanish speech track is the actual Microsoft Voice response from the recorded fan Listen interaction (29.422–33.950 seconds); the English narrator leaves space for it. English, French, Spanish and Hindi are implemented app features, not roadmap promises. Capture helpers read local ignored credentials and do not persist their values. Developer runtime paths in `tools/` may require adjusting on another machine. Font licenses are under `assets/fonts/`; app assets and footage remain subject to the repository license. This is an editable preview; no MP4 export has been generated.

The architecture scene now labels three independent Agent Framework viewer workflows and deterministic publication policy. The telemetry segment uses a separate real local run of the refactored app with live Azure models; `assets/telemetry-provenance.json` records the selected analyst run, stage names and measured timings. The product walkthrough and narration remain unchanged, and the film remains 120 seconds.
