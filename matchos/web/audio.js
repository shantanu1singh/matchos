/* One audio owner. Requests and playback are invalidated together on navigation. */
class CommentaryPlayer {
  constructor({getStory, onState, fetchAudio = (...args) => fetch(...args), makeAudio = () => new Audio(), urls = URL}) {
    this.getStory = getStory; this.onState = onState;
    this.fetchAudio = fetchAudio; this.makeAudio = makeAudio; this.urls = urls;
    this.view = "primary"; this.auto = false; this.stage = "idle"; this.message = "";
    this.generation = 0; this.lastAuto = null; this.controller = null; this.audio = null; this.url = null;
  }
  stop(message = "") {
    this.generation++;
    this.controller?.abort(); this.controller = null;
    if (this.audio) {
      this.audio.onended = null; this.audio.onerror = null;
      this.audio.pause(); this.audio.removeAttribute("src"); this.audio.load(); this.audio = null;
    }
    if (this.url) this.urls.revokeObjectURL(this.url);
    this.url = null; this.stage = "idle"; this.message = message;
    this.onState();
  }
  reset() { this.lastAuto = null; this.stop(); }
  selectView(view) {
    this.stop(); this.view = view; this.lastAuto = null;
    this.onState();
    if (this.auto) this.offer(view, true);
  }
  setAuto(enabled) {
    this.auto = enabled;
    if (!enabled) this.stop();
    else { this.lastAuto = null; this.offer(this.view, true); }
    this.onState();
  }
  offer(view, aired) {
    if (!this.auto || view !== this.view || !aired) return;
    const story = this.getStory(view);
    if (!story?.id || !story.fresh || story.id === this.lastAuto) return;
    this.lastAuto = story.id;
    void this.play(view, true);
  }
  check() {
    if (this.stage === "idle") return;
    const story = this.getStory(this.view);
    if (!story || story.id !== this.id || (this.requireFresh && !story.fresh)) this.stop();
  }
  async play(view, automatic = false) {
    this.stop(); this.view = view;
    const story = this.getStory(view);
    if (!story?.id || (automatic && !story.fresh)) {
      this.message = "Waiting for an aired story to listen to."; this.onState(); return;
    }
    const generation = this.generation;
    this.id = story.id; this.requireFresh = automatic;
    if (this.auto) this.lastAuto = story.id;
    this.stage = "loading"; this.message = `Preparing ${story.label} commentary…`;
    const controller = this.controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    this.onState();
    try {
      const response = await this.fetchAudio(`/api/speech/${encodeURIComponent(story.id)}`, {signal: controller.signal});
      if (!response.ok) {
        let detail = "Audio is unavailable. Try listening again.";
        try { detail = (await response.json()).detail || detail; } catch (_) { /* Keep a readable fallback. */ }
        throw new Error(detail);
      }
      const blob = await response.blob();
      if (generation !== this.generation) return;
      const current = this.getStory(view);
      if (!current || current.id !== story.id || (automatic && !current.fresh)) { this.stop(); return; }
      this.url = this.urls.createObjectURL(blob);
      const audio = this.audio = this.makeAudio();
      audio.src = this.url;
      audio.onended = () => { if (generation === this.generation) this.stop(`${story.label} commentary finished.`); };
      audio.onerror = () => { if (generation === this.generation) this.stop("Audio could not play. Press Listen to retry."); };
      await audio.play();
      if (generation !== this.generation) return;
      this.stage = "playing"; this.message = `${story.label} · ${story.delivery}`;
      this.onState();
    } catch (error) {
      if (generation !== this.generation) return;
      this.stop(error.name === "NotAllowedError" ? "Your browser paused audio. Press Listen to play this story." :
        error.name === "AbortError" ? "Audio took too long. Press Listen to retry." : error.message);
    } finally { clearTimeout(timer); }
  }
}
