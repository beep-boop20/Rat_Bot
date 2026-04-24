from __future__ import annotations

from typing import Any, Dict, Optional

_CONTROL_QUEUE = None


def configure_control_queue(queue) -> None:
    """Attach a process-shared control queue to this process."""
    global _CONTROL_QUEUE
    _CONTROL_QUEUE = queue


def request_control_action(action: str) -> bool:
    """Request a supervisor action such as 'restart' or 'shutdown'."""
    if not action:
        return False

    queue = _CONTROL_QUEUE
    if queue is None:
        return False

    payload: Dict[str, Any] = {"action": str(action).strip().lower()}
    try:
        queue.put_nowait(payload)
    except Exception:
        return False
    return True


def parse_control_message(message: Any) -> Optional[str]:
    """Extract a control action from queue messages."""
    if isinstance(message, dict):
        action = message.get("action")
    else:
        action = message

    if not isinstance(action, str):
        return None

    action = action.strip().lower()
    if action in {"restart", "shutdown"}:
        return action
    return None
