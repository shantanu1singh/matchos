# MatchOS product walkthrough storyboard

119 seconds. The original designed demo remains in `../matchos-demo`.

## 0–52: Real match, independent audiences

Retain the continuous 1920×1080 browser recording at 1x. Judge demo launches at 4.83 seconds and all three workers finish at 13.86 seconds. The broadcaster and analyst choose press breakdown; the Liverpool fan chooses momentum in Spanish. A finite 1.28x camera push enlarges the real sidebar, then returns. Open the decision explanation, facts and pitch evidence using the genuine recorded interactions. This retained earlier recording contains the earlier navigation labels.

The genuine Spanish Listen response plays at 29.422–33.950 seconds while the English narrator is silent. No cuts or speed changes occur inside the opening.

## 52–70: Broadcast — one published story, several uses

Use `assets/broadcast-current.png`, captured from the current local app with configured Azure models. Begin on the graphics preview and audience selector, then move to actual match-so-far recaps, including the Spanish fan. Explain that Broadcast reuses aired stories for on-screen graphics, a separate graphics window, and audience recaps. The large annotation says “One story. Ready to use.” then “Catch up in their language.” No extra editorial call is implied for repackaging.

## 70–89: Evaluations — read a saved test

Use `assets/evaluations-overview-clean.jpg` for the saved-run selector, explanation cards and actual results. The whole native capture fits the frame without horizontal cropping. Define the dropdown as a saved test run and a row as one audience in one situation. Then reveal `assets/evaluations-results-clean.jpg`: compact, collapsed result rows fit all six columns. A gold video outline identifies the Broadcaster “Update just aired” row, which was held back and passed. Keep individual checks collapsed to avoid a sparse expanded column. Large text explains that a repeated update should stay off air. Automatic checks are shown; optional separate AI writing grades are not fabricated. Source details are in `assets/new-tabs-provenance.json`.

## 89–103: How the system works

Adapt the original architecture diagram to a shorter reveal. Shared deterministic state feeds checked candidates; each audience independently selects with Decision-1 and writes with gpt-6-luna through Microsoft Agent Framework. Publication policy checks freshness, repetition and supported copy. Telemetry records input and output. Caption the deployed Azure Container Apps, managed identity, Key Vault and private Blob exports. Hold the completed diagram for reading.

## 103–119: Proposed partnership extension and close

Show a future licensed match feed flowing through MatchOS into official app/Companion stories, audience-specific graphics and multilingual catch-up. Label this “A proposed extension,” with a synthetic-data prototype footnote. The narration says “could extend” Microsoft's Premier League partnership. End on “One match. Every perspective.”

## Sound and timing

`timing.json` records actual synthesized section durations. English narration and the final composition are exactly 119 seconds. The new closing narration ends at 116.528 seconds, leaving a final reading hold. Keep the original Spanish audio and voice-only sound design. Sound is measured and transcribed; no by-ear audition is claimed.
