from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from agents import IncidentState

LIFECYCLE_OPENED = "opened"
LIFECYCLE_INVESTIGATING = "investigating"
LIFECYCLE_NEEDS_HUMAN_REVIEW = "needs_human_review"
LIFECYCLE_RESOLVED = "resolved"
LIFECYCLE_POSTMORTEM_READY = "postmortem_ready"
LIFECYCLE_FAILED = "failed"


def sync_lifecycle(state: IncidentState) -> None:
    """Sync lifecycle_status based on current state.
    The graph nodes set lifecycle_status explicitly at key transitions;
    this function only fills in the gaps (failed state, and basic investigating)."""
    if state.current_status == "failed":
        state.lifecycle_status = LIFECYCLE_FAILED
        return
    # Don't override if the graph has already set a terminal value
    if state.lifecycle_status in {LIFECYCLE_RESOLVED, LIFECYCLE_NEEDS_HUMAN_REVIEW, LIFECYCLE_POSTMORTEM_READY}:
        return
    if state.completed_steps:
        state.lifecycle_status = LIFECYCLE_INVESTIGATING


def append_review_event(
    record: Dict[str, Any],
    *,
    action: str,
    actor: str,
    decision: str,
    reason: str = "",
    previous_value: Any = None,
    new_value: Any = None,
) -> Dict[str, Any]:
    event = {
        "timestamp": datetime.now().isoformat(),
        "actor": actor or "anonymous",
        "action": action,
        "decision": decision,
        "reason": reason,
        "previous_value": previous_value,
        "new_value": new_value,
    }
    record.setdefault("review_events", []).append(event)
    return event


def set_lifecycle_after_review(record: Dict[str, Any]) -> None:
    rca_decisions = [
        e for e in record.get("review_events", [])
        if e.get("action") in {"accept_rca", "override_root_cause"}
    ]
    if rca_decisions and record.get("quality_gates", {}).get("overall_passed"):
        record["lifecycle_status"] = LIFECYCLE_POSTMORTEM_READY
    elif record.get("root_cause"):
        record["lifecycle_status"] = LIFECYCLE_NEEDS_HUMAN_REVIEW
