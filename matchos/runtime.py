"""Typed boundaries between playback, viewer scheduling and editorial workflows."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from agent_framework import Agent

from matchos.decisions.contracts import DecisionClient
from matchos.schemas import MatchState
from matchos.telemetry import Telemetry

SendMessage = Callable[[dict], Awaitable[None]]


@dataclass(frozen=True)
class ViewerDependencies:
    """Only what a viewer needs; playback/session internals stay outside the graph.

    State is read on demand because playback advances while model calls await.
    The workflow's evidence snapshot remains frozen in its PipelineResult.
    """

    decisions: DecisionClient
    agent: Agent | None
    language_provider: str
    reasoning_timeout_s: float
    current_state: Callable[[], MatchState]
    telemetry: Telemetry
    demo_step: int | None = None
