"""
safety.py — Destructive-action confirmation layer.

Before any action flagged as dangerous is executed,
Axis asks the user to confirm. On refusal, the action
is silently dropped and a cancellation message returned.
"""

from __future__ import annotations

from config_loader import CONFIG
from exceptions import SafetyError

_safety_cfg = CONFIG.get("safety", {})
_ENABLED = _safety_cfg.get("confirm_destructive", True)
_DESTRUCTIVE = set(_safety_cfg.get("destructive_actions", [
    "delete", "move", "shutdown", "restart", "format"
]))


def _is_destructive(action_data: dict) -> bool:
    """Return True when the action or its operation sub-field is destructive."""
    action = action_data.get("action", "")
    operation = action_data.get("parameters", {}).get("operation", "")
    ctrl_action = action_data.get("parameters", {}).get("action", "")  # system_control

    candidates = {action.lower(), operation.lower(), ctrl_action.lower()}
    return bool(candidates & _DESTRUCTIVE)


def check(action_data: dict, get_input_fn) -> None:
    """
    Raise SafetyError if the action is destructive AND the user declines.
    get_input_fn is called to read user confirmation (injected to keep this testable).
    """
    if not _ENABLED:
        return
    if not _is_destructive(action_data):
        return

    action = action_data.get("action", "unknown")
    params = action_data.get("parameters", {})
    target = params.get("path") or params.get("action") or params.get("app") or ""

    print(
        f"\n  \033[93m[SAFETY]\033[0m  This action is potentially destructive.\n"
        f"  Action  : {action}\n"
        f"  Target  : {target}\n"
        f"  Type  yes  to confirm, anything else to cancel.\n"
    )
    answer = get_input_fn("  Confirm > ").strip().lower()

    if answer not in ("yes", "y"):
        raise SafetyError(action, f"User cancelled destructive action on '{target}'.")
