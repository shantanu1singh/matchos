"""Project lightweight session doubles onto the production viewer boundary."""

from matchos.runtime import ViewerDependencies
from matchos.telemetry import Telemetry


def viewer_dependencies(session):
    services = session.services
    return ViewerDependencies(
        decisions=services.decisions,
        agent=getattr(services, "agent", None),
        language_provider=getattr(services, "language_provider", "test"),
        reasoning_timeout_s=getattr(services, "reasoning_timeout_s", 15),
        current_state=lambda: session.live_state_engine.state,
        telemetry=getattr(session, "telemetry", None) or Telemetry(),
        demo_step=getattr(session, "demo_step", None),
    )
