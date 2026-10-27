# Published output and personalized recaps

The **Broadcast** tab consumes completed AIR results from the normal viewer workflows. It never promotes held candidates or starts extra inference. Choose a viewer to preview a lower-third graphic, inspect/export its packet and follow that viewer's aired-story timeline.

**Open reference renderer** opens `/broadcast` in a separate window. The source tab sends the same publication packets and match clock through a same-origin `BroadcastChannel`. Keep both tabs in the same browser/profile. The renderer creates no WebSocket session, simulator or model client. It clears graphics at their match-time expiry, on reset, or after five seconds without a source heartbeat. Pausing the match retains its match clock and continues heartbeats. This is a reference integration, not a claimed integration with a commercial graphics vendor or a cross-device publication service.

## Versioned contract

The normal authenticated `/ws` connection emits `broadcast_packet` messages containing a `packet`:

| Field | Meaning |
|---|---|
| `schema_version`, `event` | `matchos.insight.v1`, `story.published`. |
| `stream_id`, `sequence` | A new stream for each match/seek/demo checkpoint; monotonically increasing sequence within it. |
| `story_id` | Hash of the viewer profile, story frame, validity start, source IDs and facts. |
| `published_at`, `published_at_match_ms` | UTC publication timestamp and match-clock publication time. |
| `viewer_index`, `viewer` | Which of the up-to-three viewer profiles this packet serves. |
| `frame` | Verified story category. |
| `clock` | `timebase: match-elapsed-ms`, `valid_from_ms`, `expires_at_ms`. These use the match clock, not Unix time. |
| `content` | Published headline/body/kicker, structured stats and optional graphic metadata. |
| `evidence` | Canonical facts, source-event IDs, source-event details and passed checks. |
| `provenance` | Decision/copy providers, agent/template source and editorial selection confidence. |

Consumers must not keep airing a packet after its match-time expiry. The stream also sends `story_reset` and `story_history` control messages. Ignore packets from obsolete streams after a reset. Selection confidence is an editorial score, not a probability that verified facts are true. **Export packet** downloads the actual packet used by the preview, not a mocked integration example.

## Developing stories and recap

Each connection keeps at most 60 aired packets per viewer. `story_history` exposes that viewer's timeline and `recap`. The recap keeps the most recent version of each routine story thread and individual major events, then extracts up to four items in match-time order. It uses the already published headline/body in the viewer's chosen language; no extra generative synthesis or invented tactical explanation is added.

The recap is labelled **match so far**, not a full-time report. It covers observed, aired stories only, not the complete event log. Moving forward through Judge demo preserves earlier checkpoint history. A retry, backward checkpoint jump, new match, seek or changed profile clears incompatible history so future or another viewer's stories do not leak into a recap. Disconnecting loses session-local histories; evaluation archives have separate durable storage.

**Export recaps** downloads `matchos.recap.v1` JSON with per-viewer source story IDs, timestamps and extracted copy. Chronology establishes sequence, not causation: a pressure story followed by a goal does not prove the pressure caused the goal.

The Broadcast UI shows human-readable story names and match-time validity windows. Stable story identifiers remain in the expandable technical JSON and exported packets, where they let downstream consumers and debugging tools refer to the same publication. They are not audience or user identifiers. The former Producer tab is consolidated into **Telemetry → Candidate history · primary perspective**, retaining inspection of the primary audience's decisions without another top-level page.
