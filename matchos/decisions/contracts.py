"""The typed decision interface consumed by editorial workflows."""

from __future__ import annotations

from typing import Protocol

from matchos.schemas import MatchState, MomentDecision, VerifiedMoment, ViewerProfile


class DecisionClient(Protocol):
    provider: str

    async def select(
        self, state: MatchState, candidates: list[VerifiedMoment], viewer: ViewerProfile, context: dict
    ) -> MomentDecision: ...

    async def decide(
        self, state: MatchState, reasons: list[str], focus_player_id: str | None = None
    ) -> MomentDecision: ...
