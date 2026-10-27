# MatchOS product walkthrough

The current 119-second demo includes the real match walkthrough, Broadcast, Evaluations, the agent architecture and a proposed Microsoft–Premier League partnership extension. The original designed cut remains in `../matchos-demo`.

The opening 52 seconds are one continuous 1x browser recording. It preserves the actual model wait, three independent persona choices, the Spanish fan's real Listen response and the evidence inspection. This retained earlier recording includes earlier navigation labels. The new Broadcast and Evaluations scenes use separate native screenshots of the current local app, configured Azure Decision-1/gpt-6-luna output, and actual saved Azure test results. No UI cards or grades are invented.

Broadcast is explained as reusing aired stories for graphics and match recaps. Evaluations is explained as saved tests: choose a run, read each situation/audience row, and understand why holding a repeated story can pass. The closing proposes licensed-feed integrations with official companion, broadcast and multilingual experiences; this synthetic-data prototype is independent.

```sh
npx --yes hyperframes@0.8.192 preview --background --port 3018
npm run check
npx --yes hyperframes@0.8.192 render --quality delivery --fps 30 --workers 2 --video-frame-format png --no-best-effort --output renders/matchos-product-demo-evaluations-polished.mp4
```

Editable preview: <http://localhost:3018/#project/matchos-product-demo>. You can also open this project in the HyperFrames desktop app. The delivery export is `renders/matchos-product-demo-evaluations-polished.mp4`; exports stay ignored by Git. This delivered MP4 retains the preceding export’s verified -16 LUFS audio track without changing its timing; a fresh raw CLI render may use different mix gain.

`SCRIPT.md`, `STORYBOARD.md`, `SPEC.md` and `REVIEW.md` describe narration, framing and validation. `assets/walkthrough-provenance.json` records the original interactions; `assets/new-tabs-provenance.json` records the new captures. `narration-plan.json` and `timing.json` hold the authored and measured narration timing. `tools/narrate_azure.py` regenerates the Microsoft en-GB-Emily MAI-Voice-2.1 narrator. The master is 119 seconds, approximately -16 LUFS, with no background music. Spanish app speech plays at 29.422–33.950 seconds.

Preserve source media referenced by the composition. Ignore raw takes, caches, review frames and delivery renders. Regeneration requires local runtime dependencies and ignored Azure credentials; do not commit their values. Font licenses are in `assets/fonts/`; app assets and footage remain subject to the repository license.

The Evaluations screenshots were refreshed to fit every column and remove the sparse expanded-check layout. A thin gold annotation identifies the actual repeated-story result. The narration, other scenes and 119-second duration are unchanged.
