# MatchOS demo video

A narrated **120-second, 1920×1080** HyperFrames composition built from the deployed app’s Judge demo. Eight scenes follow the three viewer perspectives through a press breakdown, Liverpool’s goal and Manchester City’s dismissal, then explain the agent pipeline, telemetry and grading, Azure hosting, and future work.

## Preview and review

From this folder:

```sh
npx hyperframes@0.8.192 preview --background --port 3017
npx hyperframes@0.8.192 preview --status
npm run check
```

Studio is the review surface. The project has narration and seekable scene timelines. Source clips are muted; `narration.wav` is the separate voice track. There is no background music. Narration is mastered to approximately −16 LUFS. An MP4 has not been exported yet.

When ready to export:

```sh
npx hyperframes@0.8.192 render --output renders/matchos-demo.mp4
```

## Editing

- `index.html`: scene timing and crossfades.
- `compositions/beat-1.html` through `beat-8.html`: editable scenes.
- `assets/theme.css`: MatchOS colors and local fonts.
- `SCRIPT.md`, `narration.txt`: spoken script.
- `STORYBOARD.md`, `DESIGN.md`: creative plan and measured timing.
- `timing.json`, `transcript.json`: measured voice intervals and word timings.
- `assets/output-quotes.json`: exact model outputs captured for this edit.
- `capture/screenshots/`: actual app evidence; `assets/*.mp4`: selected footage.

`tools/` contains the capture, narration and footage-preparation helpers used for this edit. Capture uses locally configured app credentials, never embeds them in footage, and requires Playwright plus its browser. Local narration uses Kokoro ONNX and FFmpeg; regenerate the word transcript after changing audio. Developer runtime paths in these helpers may need adjusting on another machine.

## Accuracy and provenance

All match footage comes from MatchOS’s simulated Liverpool–Manchester City Judge checkpoints at 64:00, 65:04 and 68:01. Large persona quotes are exact captured excerpts typeset for readability. Selection remains independent per viewer; different personas can legitimately choose the same event. Timing figures in the telemetry scene are one captured run, not a benchmark. The Foundry grading segment describes saved-run graders without inventing a completed cloud evaluation or scores. The last scene labels unshipped capabilities as roadmap items.

The film narration was generated locally using Kokoro `af_nova`. Azure Speech in the architecture describes the app’s own spoken-story feature. Font licenses are included beside the local font files. App footage and product assets remain subject to the repository license.

## Verification

HyperFrames `check` and `validate` pass with no source, runtime, layout or contrast errors. All eight scenes and the closing card were inspected as rendered frames. Craft warnings concern secondary labels below 28px and a deliberate reading hold; primary comparison body copy is 28px, with larger headlines. Internal review artifacts are under the ignored `.hyperframes/` directory.
