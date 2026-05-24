"""
main.py — Axis AI Control Loop v3.0
State machine: STANDBY ↔ ACTIVE.

Changes from v2:
  • App discovery index is built/loaded on startup
  • Early-return after local/rule/workflow layers — Gemini is NEVER
    called after a successful local execution (Bug #1 fix)
  • generate_response() only called when an AI provider actually handled
    the command
  • 'axis rebuild index' command to force a fresh app scan
  • Status report includes app index stats
"""

from __future__ import annotations

import sys
import time
from enum import Enum, auto

# ─── ANSI Colors ──────────────────────────────────────────────────────────────

CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

# Layers that resolve entirely locally — no AI response generation needed
_LOCAL_LAYERS = {"local_command", "rule_nlp", "workflow", "workflow_result"}


def axis_print(msg: str, color: str = GREEN):
    print(f"\n{color}{BOLD}[AXIS]{RESET} {msg}")


def hint(msg: str):
    print(f"  {YELLOW}{msg}{RESET}")


def get_input(prompt: str = "") -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


# ─── State ────────────────────────────────────────────────────────────────────

class State(Enum):
    STANDBY = auto()
    ACTIVE  = auto()


# ─── Banner ───────────────────────────────────────────────────────────────────

def banner():
    print(f"""
{CYAN}{BOLD}
    ╔════════════════════════════════════════════╗
    ║         A X I S  A I  v3.0                ║
    ║  Self-Healing 6-Layer Automation Engine   ║
    ╚════════════════════════════════════════════╝
{RESET}""")


def _layer_status():
    from config_loader import CONFIG
    layer_cfg = CONFIG.get("layers", {})
    layers = [
        ("L0 Local Command", "local_command", "< 50 ms, zero AI"),
        ("L1 Rule NLP",      "rule_nlp",      "no AI, natural phrasing"),
        ("L2 Workflow",      "workflow",       "multi-step plans"),
        ("L3 Tiny Model",    "tiny_model",     "offline AI (optional)"),
        ("L4 Ollama",        "ollama",         "local AI server"),
        ("L5 Gemini",        "gemini",         "cloud AI"),
    ]
    print(f"\n  {CYAN}Intelligence Layers:{RESET}")
    for label, key, note in layers:
        enabled = layer_cfg.get(key, {}).get("enabled", True)
        icon = f"{GREEN}●{RESET}" if enabled else f"{DIM}○{RESET}"
        print(f"    {icon}  {label:<20} {DIM}{note}{RESET}")


# ─── Command Processing ───────────────────────────────────────────────────────

def _strip_prefix(text: str) -> str:
    lower = text.lower()
    for prefix in ("axis, ", "axis "):
        if lower.startswith(prefix):
            return text[len(prefix):].strip()
    return text.strip()


def process_command(raw: str):
    """Parse, execute, and reply to a single command."""
    import time as _time
    from brain import generate_response, parse_intent
    from executor import execute
    from memory import memory
    from safety import check as safety_check
    from axis_logger import log_command

    cmd = _strip_prefix(raw)
    if not cmd:
        return

    print(f"\n  {DIM}Thinking...{RESET}")
    t0 = _time.perf_counter()

    memory.add_user(cmd)

    action_data = parse_intent(cmd)
    action      = action_data.get("action", "unknown")
    parameters  = action_data.get("parameters", {})
    layer       = action_data.get("_layer", "?")

    print(f"  {DIM}[Layer: {layer}]  Intent → {action} {parameters}{RESET}")

    # Safety gate
    try:
        safety_check(action_data, get_input)
    except Exception as se:
        axis_print(f"Action cancelled. {se}", YELLOW)
        return

    # Execute
    if action == "workflow_result":
        result = parameters.get("summary", "Workflow completed.")
    else:
        result = execute(action_data)
        memory.set_last_action(action_data)

    latency_ms = (_time.perf_counter() - t0) * 1000

    # ── CRITICAL: Early return for local layers — never call AI ──────────────
    # Local/rule/workflow layers produce deterministic results.
    # Calling generate_response() here would invoke an AI provider unnecessarily.
    if layer in _LOCAL_LAYERS:
        memory.add_assistant(result)
        log_command(cmd, layer, None, action, result, latency_ms)
        axis_print(result)
        if latency_ms < 100:
            print(f"  {DIM}↳ {latency_ms:.0f} ms (local){RESET}")
        return

    # AI layers — generate a conversational response
    response = generate_response(action, parameters, result)
    memory.add_assistant(response)
    log_command(cmd, layer, None, action, result, latency_ms)
    axis_print(response)

    if latency_ms < 55:
        print(f"  {DIM}↳ {latency_ms:.0f} ms (local){RESET}")


# ─── State Loops ──────────────────────────────────────────────────────────────

def standby_loop():
    axis_print("Standby. Type  axis start  to activate.", YELLOW)
    hint("(or type  exit  to quit)")

    while True:
        text = get_input(f"\n{DIM}standby >{RESET} ").lower()
        if not text:
            continue
        if any(p in text for p in ("axis start", "axis wake", "start axis")):
            return
        if text in ("exit", "quit", "bye"):
            raise SystemExit


def active_loop():
    axis_print("Online. Prefix every command with  axis  — e.g.  axis open chrome")
    hint("axis off=standby  |  axis exit=quit  |  axis status=diagnostics  |  axis rebuild index=rescan apps")

    while True:
        text = get_input(f"\n{CYAN}axis >{RESET} ")
        if not text:
            continue
        lower = text.lower()

        if any(p in lower for p in ("axis off", "axis sleep", "stop axis")):
            from brain import clear_history
            axis_print("Going to standby. Type  axis start  when you need me.", YELLOW)
            clear_history()
            return

        if any(p in lower for p in ("axis exit", "axis quit")):
            axis_print("Shutting down. Goodbye!")
            raise SystemExit

        if "axis status" in lower:
            from provider_manager import provider_manager
            from app_discovery import app_index
            report = provider_manager.status_report()
            report += f"\n\n{app_index.status()}"
            axis_print(report, CYAN)
            continue

        if "axis rebuild index" in lower:
            axis_print("Rebuilding application index... (this may take a few seconds)", CYAN)
            from app_discovery import app_index
            app_index.rebuild()
            axis_print(app_index.status(), GREEN)
            continue

        if "axis help" in lower:
            _print_help()
            continue

        if lower.startswith("axis"):
            process_command(text)
        else:
            hint("Prefix your command with  axis  — e.g.  axis search Python tutorials")


def _print_help():
    print(f"""
{CYAN}  Axis AI v3.0 — Quick Reference{RESET}
  ─────────────────────────────────────
  axis open <app>             Open an application (auto-discovered)
  axis open <website>         Open a website
  axis search <query>         Web search
  axis play <song> on youtube Play media
  axis time / date            Get time or date
  axis weather in <city>      Weather info
  axis screenshot             Take screenshot
  axis calculate <expr>       Math calculation
  axis volume up/down         System volume
  axis shutdown / restart     Power controls
  axis open Chrome and go to YouTube   (workflow)
  axis status                 Provider + app index health
  axis rebuild index          Rescan all installed applications
  axis off                    Standby mode
  axis exit                   Quit
  ─────────────────────────────────────
  {DIM}Hindi/Hinglish: kholo, dhundo, time batao, screenshot lo,
  volume badao/kam karo, band karo{RESET}
""")


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    banner()
    axis_print("Initialising Axis AI v3.0...", CYAN)

    try:
        from brain import parse_intent   # noqa: F401
        from executor import execute     # noqa: F401
    except Exception as exc:
        axis_print(f"Startup error: {exc}", RED)
        sys.exit(1)

    _layer_status()

    # ── Build / load application index ───────────────────────────────────────
    print(f"\n  {CYAN}Application Discovery:{RESET}")
    try:
        from app_discovery import app_index
        # _ensure_loaded() is lazy but we want startup feedback
        app_index._ensure_loaded()
        print(f"    {GREEN}●{RESET}  {app_index.status()}")
    except Exception as exc:
        print(f"    {YELLOW}○{RESET}  App index unavailable: {exc}")

    # ── Network check ─────────────────────────────────────────────────────────
    try:
        import urllib.request
        urllib.request.urlopen("https://www.google.com", timeout=3)
        axis_print("Network: online. Cloud AI available.", GREEN)
    except Exception:
        axis_print(
            "Network: offline. Running in intelligent offline mode.\n"
            "  Layers L0–L2 active. Ollama + Tiny Model available if configured.",
            YELLOW,
        )

    time.sleep(0.3)
    state = State.STANDBY

    while True:
        try:
            if state is State.STANDBY:
                standby_loop()
                state = State.ACTIVE
            else:
                active_loop()
                state = State.STANDBY

        except SystemExit:
            print(f"\n{CYAN}Axis AI terminated.{RESET}\n")
            sys.exit(0)

        except Exception as exc:
            axis_print(f"Unexpected error: {exc} — returning to standby.", RED)
            state = State.STANDBY
            time.sleep(1)


if __name__ == "__main__":
    main()
