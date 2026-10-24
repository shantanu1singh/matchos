"""On-demand speech for server-approved aired copy, with bounded shared caching."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import math
import os
import re
import time
import weakref
from xml.sax.saxutils import escape, quoteattr

import httpx

from matchos.model_logging import ModelCall
from matchos.observability import trace_carrier
from matchos.schemas import PipelineResult, ViewerProfile
from matchos.telemetry import Telemetry, record
from matchos.teams import team_name


@dataclass(frozen=True)
class SpeechConfig:
    enabled: bool = False
    key: str = field(default="", repr=False)
    region: str = ""
    model: str = "MAI-Voice-2.1-Flash"
    timeout_s: float = 12

    @classmethod
    def from_env(cls):
        timeout = float(os.getenv("SPEECH_TIMEOUT_S", "12"))
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("SPEECH_TIMEOUT_S must be greater than 0 and at most 60")
        region = os.getenv("AZURE_SPEECH_REGION", "").strip().lower()
        if region and not re.fullmatch(r"[a-z0-9]+", region):
            raise ValueError("AZURE_SPEECH_REGION must be a region identifier such as eastus")
        model = os.getenv("AZURE_SPEECH_MODEL", "MAI-Voice-2.1-Flash").strip()
        if model not in {"MAI-Voice-2.1-Flash", "MAI-Voice-2.1"}:
            raise ValueError("AZURE_SPEECH_MODEL must be MAI-Voice-2.1-Flash or MAI-Voice-2.1")
        return cls(os.getenv("MATCHOS_SPEECH_ENABLED", "").lower() in {"1", "true"},
                   os.getenv("AZURE_SPEECH_KEY", "").strip(), region, model, timeout)

    @property
    def available(self):
        return bool(self.enabled and self.key and self.region)


@dataclass(frozen=True)
class SpeechSpec:
    ssml: str
    text: str
    voice: str
    style: str
    delivery: str
    persona: str
    language: str

    @property
    def cache_key(self):
        return hashlib.sha256(self.ssml.encode()).hexdigest()


def prepare_speech(result: PipelineResult, viewer: ViewerProfile, config: SpeechConfig) -> SpeechSpec:
    """SSML controls delivery; no new model rewrites or spoken instructions."""
    overlay, moment = result.overlay, result.moment
    if (result.kind != "moment" or not result.producer or result.producer.action != "AIR"
            or not overlay or not moment or overlay.moment_id != moment.id
            or not result.verification or not result.verification.verified
            or not moment.checks or not all(check.passed for check in moment.checks)
            or overlay.persona != viewer.persona or overlay.language != viewer.language
            or overlay.team_id != viewer.team_id
            or (viewer.persona == "player" and overlay.player_id != viewer.player_id)):
        raise ValueError("Speech requires a finished aired story for this viewer")
    headline, body = overlay.headline.strip(), overlay.body.strip()
    # Don't read the same headline twice if a fallback body already contains it.
    text = body if body.casefold().startswith(headline.casefold()) else f"{headline.rstrip('.!?।')}. {body}"
    if not text.strip() or len(text) > 1500:
        raise ValueError("Speech copy must contain 1–1500 characters")
    hindi = viewer.language == "hi"
    major = moment.frame in {"GOAL", "COMEBACK", "RED_CARD"}
    if viewer.persona == "analyst":
        base, style, rate = ("hi-IN-Harper" if hindi else "en-US-Grant"), "educational", "-5%"
        delivery = "Measured, explanatory analysis"
    elif viewer.persona == "broadcaster":
        base, style, rate = ("hi-IN-Dhruv" if hindi else "en-GB-Emily"), ("excited" if major else "narrator"), "+2%"
        if hindi and not major:
            style = "neutral"
        delivery = "Energetic major-event cue" if major else "Composed broadcast delivery"
    elif viewer.persona == "fan":
        base, rate = ("hi-IN-Priya" if hindi else "en-GB-Emily"), "+5%"
        good_news = (moment.team_id != viewer.team_id if moment.frame == "RED_CARD" else
                     moment.facts.get("attackingTeam") == team_name(viewer.team_id) if moment.frame == "PRESS_BREAKDOWN" else
                     moment.team_id == viewer.team_id)
        style = "joyful" if good_news else "sad"
        delivery = "Warm, celebratory supporter" if good_news else "Concerned supporter"
    else:
        base, style, rate = ("hi-IN-Grant" if hindi else "en-GB-Harry"), "neutral", "0%"
        delivery = "Clear, conversational delivery"
    # Locale-specific overrides must support the mapped SSML style.
    override = os.getenv(f"AZURE_SPEECH_VOICE_{viewer.persona.upper()}_{viewer.language.upper()}", "").strip()
    voice = override or f"{base}:{config.model}"
    if not re.fullmatch(r"[A-Za-z0-9:._-]+", voice):
        raise ValueError("Invalid speech voice identifier")
    locale = voice.split(":")[0][:5]
    ssml = (f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xmlns:mstts="http://www.w3.org/2001/mstts" xml:lang={quoteattr(locale)}>'
            f'<voice name={quoteattr(voice)}><mstts:express-as style={quoteattr(style)}>'
            f'<prosody rate={quoteattr(rate)}>{escape(text)}</prosody>'
            '</mstts:express-as></voice></speak>')
    return SpeechSpec(ssml, text, voice, style, delivery, viewer.persona, viewer.language)


class SpeechUnavailable(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


@dataclass
class AiredSpeech:
    spec: SpeechSpec
    registered: float
    store: object
    parent: dict
    moment_id: str
    timestamp_ms: int


class SpeechService:
    """Each process has an LRU cache; no synthesis until an audio URL is fetched."""
    def __init__(self, config: SpeechConfig):
        self.config = config
        self.entries = OrderedDict()
        self.cache = OrderedDict()
        self.jobs = {}
        self.slots = asyncio.Semaphore(2)
        self.ttl_s = 3600
        self.max_entries = 256
        self.max_cache_entries = 128
        self.max_cache_bytes = 16 * 1024 * 1024

    def capabilities(self):
        return {"available": self.config.available, "model": self.config.model,
                "message": "Select a story to listen" if self.config.available else
                "Audio needs MATCHOS_SPEECH_ENABLED=1, AZURE_SPEECH_KEY and AZURE_SPEECH_REGION on the server."}

    def register(self, payload, store, now_ms):
        if not self.config.available or payload.get("explanation", {}).get("status") != "update":
            return None
        result = PipelineResult.model_validate(payload["result"])
        if not result.moment or result.moment.expires_at_ms <= now_ms:
            return None
        try:
            spec = prepare_speech(result, ViewerProfile.model_validate(payload["profile"]), self.config)
        except ValueError:
            return None
        self._prune()
        # Source-scoped clip IDs keep telemetry private to the correct match/viewer.
        # Audio bytes and inference jobs still share the content-based cache key.
        source = f"{store.id}:{payload.get('index', 0)}:{result.moment.id}:{result.moment.valid_from_ms}:{spec.cache_key}"
        key = hashlib.sha256(source.encode()).hexdigest()
        self.entries[key] = AiredSpeech(spec, time.monotonic(), weakref.ref(store), trace_carrier(),
                                       result.moment.id, result.moment.valid_from_ms)
        self.entries.move_to_end(key)
        while len(self.entries) > self.max_entries:
            self.entries.popitem(last=False)
        return {"id": key, "delivery": spec.delivery, "voice": spec.voice, "style": spec.style}

    def _prune(self):
        cutoff = time.monotonic() - self.ttl_s
        for key in [key for key, entry in self.entries.items() if entry.registered < cutoff]:
            self.entries.pop(key)
        for key in [key for key, (_, created) in self.cache.items() if created < cutoff]:
            self.cache.pop(key)

    async def audio(self, key):
        if not self.config.available:
            raise SpeechUnavailable(self.capabilities()["message"])
        self._prune()
        entry = self.entries.get(key)
        if entry is None:
            raise SpeechUnavailable("This audio has expired. Listen to a newly aired story.", 404)
        key = entry.spec.cache_key
        if key in self.cache:
            audio, _ = self.cache[key]
            self.cache.move_to_end(key)
            store = entry.store() or Telemetry(capacity=1)
            with store.capture("Spoken commentary", {"viewer": {"persona": entry.spec.persona},
                "timestamp_ms": entry.timestamp_ms, "moment": entry.moment_id}, parent=entry.parent):
                record("speech cache", {"voice": entry.spec.voice}, {"cache_hit": True, "audio_bytes": len(audio)})
            return audio, True
        task = self.jobs.get(key)
        if task is None:
            if len(self.jobs) >= 4:
                raise SpeechUnavailable("Commentary is busy. Try listening again shortly.", 429)
            task = self.jobs[key] = asyncio.create_task(self._produce(key, entry))
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        else:
            store = entry.store() or Telemetry(capacity=1)
            with store.capture("Spoken commentary", {"viewer": {"persona": entry.spec.persona},
                "timestamp_ms": entry.timestamp_ms, "moment": entry.moment_id}, parent=entry.parent):
                audio = await asyncio.shield(task)
                record("speech shared synthesis", {"voice": entry.spec.voice}, {"audio_bytes": len(audio)})
                return audio, False
        # Shared requests finish in the cache if one browser abandons its fetch.
        return await asyncio.shield(task), False

    async def _produce(self, key, entry):
        store = entry.store() or Telemetry(capacity=1)
        call = None
        try:
            with store.capture("Spoken commentary", {"viewer": {"persona": entry.spec.persona},
                "timestamp_ms": entry.timestamp_ms, "moment": entry.moment_id}, parent=entry.parent):
                try:
                    async def synthesize():
                        nonlocal call
                        async with self.slots:
                            call = ModelCall("speech", f"azure-speech:{entry.spec.voice}", {
                                "text": entry.spec.text, "voice": entry.spec.voice, "style": entry.spec.style,
                                "delivery": entry.spec.delivery}, secrets=(self.config.key,))
                            return await self._request(entry.spec)
                    audio = await asyncio.wait_for(synthesize(), self.config.timeout_s)
                    call.result({"audio_bytes": len(audio), "format": "audio/mpeg", "cache_hit": False})
                    self.cache[key] = (audio, time.monotonic())
                    self.cache.move_to_end(key)
                    while (len(self.cache) > self.max_cache_entries or
                           sum(len(blob) for blob, _ in self.cache.values()) > self.max_cache_bytes):
                        self.cache.popitem(last=False)
                    return audio
                except asyncio.CancelledError:
                    if call:
                        call.result({"reason": "speech request cancelled"}, phase="CANCELLED")
                    raise
                except Exception as exc:
                    details = {"error": type(exc).__name__}
                    if isinstance(exc, httpx.HTTPStatusError):
                        details["status"] = exc.response.status_code
                    if call:
                        call.result(details, phase="ERROR")
                    else:
                        record("speech unavailable", {}, details, status="error")
                    raise SpeechUnavailable("Commentary could not be generated. Check the Speech resource, voice availability and server logs.", 502) from None
        finally:
            self.jobs.pop(key, None)

    async def _request(self, spec):
        endpoint = f"https://{self.config.region}.tts.speech.microsoft.com/cognitiveservices/v1"
        async with httpx.AsyncClient(timeout=self.config.timeout_s) as client:
            response = await client.post(endpoint, content=spec.ssml.encode(), headers={
                "Ocp-Apim-Subscription-Key": self.config.key, "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3", "User-Agent": "MatchOS"})
            response.raise_for_status()
            if not response.content or len(response.content) > 2 * 1024 * 1024 or not response.headers.get("content-type", "").startswith(("audio/", "application/octet-stream")):
                raise ValueError("Invalid speech audio response")
            return response.content

    async def close(self):
        tasks = list(self.jobs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
        self.entries.clear()
        self.cache.clear()
