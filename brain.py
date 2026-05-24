"""
brain.py — Axis AI Intelligence Orchestrator v3.0

Routes every command through a layered intelligence stack.
Layers are tried in ascending order; AI providers are only called
when local layers cannot handle the command.

Layer fallback chain:
  L0 Local Command Engine    (< 50 ms, no AI)  ← returns immediately on match
  L1 Rule-Based NLP Engine   (no AI)            ← returns immediately on match
  L2 Workflow Engine         (no AI)            ← returns immediately on match
  L3 Tiny Local Model        (offline AI — optional)
  L4 Ollama                  (local AI)
  L5 Gemini                  (cloud AI)

IMPORTANT: main.py checks action_data["_layer"] after parse_intent() returns.
If the layer is in _LOCAL_LAYERS, generate_response() is NOT called — no AI
provider is contacted. This eliminates the Bug #1 false AI escalation.

After all layers fail → informative error message, never a crash.
"""

from __future__ import annotations

import time
from typing import Callable

from axis_logger import get_logger, log_fallback
from config_loader import CONFIG
from exceptions import (
    AllLayersFailedError,
    AllProvidersFailedError,
    NoLayerMatchError,
)
from layers import local_command, rule_nlp, workflow
from memory import memory
from provider_manager import provider_manager

log = get_logger("brain")
_layer_cfg = CONFIG.get("layers", {})


# ─── Layer-enabled guards ─────────────────────────────────────────────────────

def _enabled(name: str) -> bool:
    return _layer_cfg.get(name, {}).get("enabled", True)


# ─── Workflow executor ────────────────────────────────────────────────────────

def _execute_workflow(action_data: dict) -> dict:
    """
    Run a workflow (multi-step action list) and return a synthetic result dict.
    Each step is executed by the executor. Results are aggregated.
    """
    from executor import execute

    steps = action_data.get("parameters", {}).get("steps", [])
    results = []
    for step in steps:
        r = execute(step)
        memory.set_last_action(step)
        results.append(r)

    summary = " | ".join(results)
    return {
        "action": "workflow_result",
        "parameters": {"summary": summary},
        "_layer": "workflow",
    }


# ─── Public API ───────────────────────────────────────────────────────────────

def parse_intent(user_command: str) -> dict:
    """
    Attempt each intelligence layer in order.

    Returns a standardised action dict on success.
    The '_layer' key tells the caller which layer handled the command:
      • 'local_command', 'rule_nlp', 'workflow'  → local; no AI needed
      • 'tiny_model', 'ollama', 'gemini'         → AI provider was used

    main.py uses this to decide whether to call generate_response().

    Returns an "unknown" action dict on total failure — never raises.
    """
    resolved = memory.resolve_pronoun(user_command)
    if resolved != user_command:
        log.debug("Pronoun resolved: '%s' → '%s'", user_command, resolved)

    # ── Layer 0: Local Command ────────────────────────────────────────────────
    if _enabled("local_command"):
        try:
            result = local_command.handle(resolved)
            result["_layer"] = "local_command"
            log.info("L0 local_command matched: %s", result.get("action"))
            return result   # ← early return; main.py will NOT call generate_response
        except NoLayerMatchError:
            pass

    # ── Layer 1: Rule NLP ────────────────────────────────────────────────────
    if _enabled("rule_nlp"):
        try:
            result = rule_nlp.handle(resolved)
            result["_layer"] = "rule_nlp"
            log.info("L1 rule_nlp matched: %s", result.get("action"))
            log_fallback("local_command", "rule_nlp", "no exact match")
            return result   # ← early return; main.py will NOT call generate_response
        except NoLayerMatchError:
            pass

    # ── Layer 2: Workflow ─────────────────────────────────────────────────────
    if _enabled("workflow"):
        try:
            wf = workflow.handle(resolved)
            log.info("L2 workflow matched: %d steps", len(wf["parameters"]["steps"]))
            log_fallback("rule_nlp", "workflow", "compound command")
            return _execute_workflow(wf)   # ← _layer = "workflow"; no AI
        except NoLayerMatchError:
            pass

    # ── Layers 3-5: AI Providers ──────────────────────────────────────────────
    # provider_manager internally tries TinyModel → Ollama → Gemini
    hist = memory.get_history()
    try:
        result = provider_manager.parse_intent(resolved, hist)
        layer = result.get("_layer", "ai_provider")
        log.info("AI provider matched: %s action=%s", layer, result.get("action"))
        log_fallback("workflow", layer, "requires AI reasoning")
        return result   # ← main.py WILL call generate_response for these
    except AllProvidersFailedError as exc:
        log.error("All AI providers failed: %s", exc)
        return {
            "action": "unknown",
            "parameters": {
                "reason": (
                    "All intelligence layers failed.\n"
                    "  • Check internet / GEMINI_API_KEY for cloud AI.\n"
                    "  • Start Ollama for local AI: `ollama serve`\n"
                    "  • Configure a tiny model in config.yaml for offline use."
                )
            },
            "_layer": "none",
        }
    except Exception as exc:
        log.error("Unexpected brain error: %s", exc)
        return {
            "action": "unknown",
            "parameters": {"reason": f"Unexpected error: {type(exc).__name__}: {exc}"},
            "_layer": "none",
        }


def generate_response(action: str, parameters: dict, result: str) -> str:
    """
    Generate a conversational reply via AI.
    Only called by main.py when an AI layer handled the command.
    Never raises.
    """
    return provider_manager.generate_response(action, parameters, result)


def clear_history() -> None:
    """Reset conversation memory (called on 'axis off')."""
    memory.clear()
    log.info("Conversation history cleared.")
