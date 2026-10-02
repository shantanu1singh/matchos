"""Animation planner.

The event generator decides what happened. This module only chooses a point
the named player can reach, and how long the ball should take to meet them.
"""

from __future__ import annotations

import math

RUN_MPS = 5.6


def plan_arrival(
    runner_x: float,
    runner_y: float,
    desired_x: float,
    desired_y: float,
    start_x: float,
    start_y: float,
    pace: float,
    *,
    pull: bool = True,
    max_s: float = 2.7,
) -> tuple[float, float, int]:
    """Where the ball and the named player meet, and how long that takes.

    The desired point comes from the event (a formation slot, a cut on the
    pass). When `pull` is set, it is brought back along that line so the
    player can arrive without a teleport. The ball uses the same deadline.
    """
    dx, dy = desired_x - runner_x, desired_y - runner_y
    dist = math.hypot(dx, dy)
    reach = RUN_MPS * max_s
    if pull and dist > reach > 0:
        scale = reach / dist
        end_x = runner_x + dx * scale
        end_y = runner_y + dy * scale
    else:
        end_x, end_y = desired_x, desired_y
    run_s = math.hypot(end_x - runner_x, end_y - runner_y) / RUN_MPS + 0.22
    ball_s = math.hypot(end_x - start_x, end_y - start_y) / max(pace, 1.0)
    duration_s = min(max_s, max(0.64, run_s, ball_s))
    return end_x, end_y, int(duration_s * 1000)
