# MatchOS video projects

- `matchos-product-demo/`: current two-minute product walkthrough. Real Judge demo interactions, Spanish home fan and localized audio, Microsoft narration, architecture and deployment.
- `matchos-demo/`: earlier designed cut retained as an archive/reference.

## What belongs in Git

Commit the HTML compositions, CSS/JS, package/config files, script/storyboard/design/review documents, narration plan/timing, font licenses, vector wordmark, and only media referenced by a composition. Keep the selected footage, selected screenshots and final narration: a fresh checkout must be able to play the project without hidden local caches.

Do **not** commit `.hyperframes/` history/state/cache, `.waveform-cache/`, `.thumbnails/`, `.media/`, `.tts-cache/`, `snapshots*/`, `renders/`, raw capture takes, diagnostic captures/JSON, intermediate voice chunks, or `node_modules/`. Ignore rules are in the repository and project `.gitignore` files. Files remain locally available for review and regeneration. Generated waveform indexes are excluded from source control; selected media and editable source remain tracked.

The final exported MP4 is a distribution artifact; share it in the hackathon submission or a release instead of duplicating it in source control. Selected source media is currently small enough for ordinary Git. Regeneration helpers need local dependencies and, for Azure narration/product recording, ignored credentials. No credential values are stored in these projects.
