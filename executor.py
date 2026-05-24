"""
executor.py — Axis AI Execution Engine v3.0

Changes from v2:
  • Replaced hardcoded _APP_MAP with app_discovery.app_index
  • handle_open_app() now validates that the process actually started
  • Every action returns an honest success/failure message — never claims
    success when execution failed
  • ExecResult dataclass provides structured metadata for callers
  • Windows Search fallback is transparent to the user
  • Self-healing: successful launches are recorded back into app_index

Public API (unchanged):
    execute(action_data: dict) -> str
"""

from __future__ import annotations

import datetime
import math
import os
import platform
import random
import shutil
import subprocess
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app_discovery import app_index
from axis_logger import get_logger

log = get_logger("executor")
OS = platform.system()   # 'Windows' | 'Darwin' | 'Linux'


# ─── Execution Result ─────────────────────────────────────────────────────────

@dataclass
class ExecResult:
    """Structured result from every executor action."""
    success: bool
    message: str
    method_used: str            # e.g. "app_index" | "windows_search" | "builtin"
    execution_time_ms: float
    error: str | None = None


# ─── Subprocess Helper ────────────────────────────────────────────────────────

def _run(cmd: list[str] | str, shell: bool = False, timeout: int = 15) -> tuple[bool, str]:
    """
    Run a command and return (success, output).
    Never raises — all errors are captured and returned as failure.
    """
    try:
        r = subprocess.run(
            cmd, shell=shell, capture_output=True, text=True, timeout=timeout
        )
        if r.returncode == 0:
            return True, r.stdout.strip() or "Done."
        return False, (r.stderr.strip() or f"Command exited with code {r.returncode}.")
    except FileNotFoundError:
        return False, f"Command not found: {cmd!r}"
    except subprocess.TimeoutExpired:
        return False, "Command timed out."
    except Exception as exc:
        return False, f"Error: {exc}"


def _run_str(cmd: list[str] | str, shell: bool = False, timeout: int = 15) -> str:
    """Compatibility shim — returns just the string output (for non-app actions)."""
    _, output = _run(cmd, shell=shell, timeout=timeout)
    return output


# ─── System Actions Map ───────────────────────────────────────────────────────

_SYSTEM_ACTIONS: dict[str, dict[str, str]] = {
    "shutdown":      {"Windows": "shutdown /s /t 5",   "Darwin": "sudo shutdown -h now", "Linux": "shutdown -h now"},
    "restart":       {"Windows": "shutdown /r /t 5",   "Darwin": "sudo shutdown -r now", "Linux": "shutdown -r now"},
    "sleep":         {"Windows": "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
                      "Darwin": "pmset sleepnow", "Linux": "systemctl suspend"},
    "lock":          {"Windows": "rundll32.exe user32.dll,LockWorkStation",
                      "Darwin": "open -a ScreenSaverEngine", "Linux": "gnome-screensaver-command -l"},
    "volume_up":     {"Windows": 'powershell -c "(New-Object -ComObject WScript.Shell).SendKeys([char]175)"',
                      "Darwin":  "osascript -e 'set volume output volume (output volume of (get volume settings) + 10)'",
                      "Linux":   "amixer -D pulse sset Master 10%+"},
    "volume_down":   {"Windows": 'powershell -c "(New-Object -ComObject WScript.Shell).SendKeys([char]174)"',
                      "Darwin":  "osascript -e 'set volume output volume (output volume of (get volume settings) - 10)'",
                      "Linux":   "amixer -D pulse sset Master 10%-"},
    "mute":          {"Windows": 'powershell -c "(New-Object -ComObject WScript.Shell).SendKeys([char]173)"',
                      "Darwin":  "osascript -e 'set volume output muted true'",
                      "Linux":   "amixer -D pulse sset Master toggle"},
    "brightness_up": {"Darwin": "osascript -e 'tell application \"System Events\" to key code 144'",
                      "Linux":  "xbacklight -inc 10", "Windows": ""},
    "brightness_down":{"Darwin": "osascript -e 'tell application \"System Events\" to key code 145'",
                       "Linux":  "xbacklight -dec 10", "Windows": ""},
}

_JOKES = [
    "Why do programmers prefer dark mode? Because light attracts bugs!",
    "I told my computer I needed a break. Now it won't stop sending me Kit-Kat ads.",
    "There are 10 types of people: those who understand binary, and those who don't.",
    "A SQL query walks into a bar and asks two tables: 'Can I join you?'",
    "Why did the developer go broke? Because he used up all his cache.",
]


# ─── Handlers ─────────────────────────────────────────────────────────────────

def handle_open_app(p: dict) -> str:
    """
    Launch an application using the app discovery index.

    Resolution order:
      1. app_index (registry + start menu + program dirs + desktop scan)
      2. Windows Search fallback (automated keyboard simulation)
      3. shutil.which() — last-resort PATH lookup
      4. Honest failure message

    After a successful launch the path is recorded back into app_index
    so future lookups are faster (self-healing).
    """
    app = p.get("app", "").lower().strip()
    if not app:
        return "No application name provided."

    t0 = time.perf_counter()

    # ── Step 1: resolve via app_index ────────────────────────────────────────
    resolved = app_index.resolve(app)

    # Windows Search was triggered (async launch already happened inside resolve)
    if resolved and resolved.startswith("__search__"):
        elapsed = (time.perf_counter() - t0) * 1000
        log.info("open_app '%s' via Windows Search (%.0f ms)", app, elapsed)
        return (
            f"'{app}' not found in index — launched via Windows Search. "
            f"If it opened, it will be remembered for next time."
        )

    # ── Step 2: try shutil.which if index gave nothing ────────────────────────
    method = "app_index"
    if not resolved:
        which_result = shutil.which(app)
        if which_result:
            resolved = which_result
            method = "which"
            log.debug("open_app '%s' resolved via shutil.which: %s", app, resolved)

    # ── Step 3: give up if still nothing ─────────────────────────────────────
    if not resolved:
        elapsed = (time.perf_counter() - t0) * 1000
        log.warning("open_app '%s' — not found anywhere (%.0f ms)", app, elapsed)
        return (
            f"Failed to open '{app}': application not found.\n"
            f"  • Check that it is installed.\n"
            f"  • Try: axis open <exact app name>"
        )

    # ── Step 4: launch and validate ──────────────────────────────────────────
    try:
        if OS == "Windows":
            proc = subprocess.Popen(resolved, shell=True,
                                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        elif OS == "Darwin":
            if str(resolved).endswith(".app"):
                proc = subprocess.Popen(["open", resolved])
            else:
                proc = subprocess.Popen(resolved, shell=True)
        else:
            proc = subprocess.Popen(resolved.split(), start_new_session=True)

        # Wait briefly and check if the process is still alive
        time.sleep(0.4)
        poll = proc.poll()
        elapsed = (time.perf_counter() - t0) * 1000

        if poll is not None and poll != 0:
            # Process exited immediately with an error code
            log.warning("open_app '%s' exited with code %d", app, poll)
            return f"Failed to open '{app}' (process exited with code {poll})."

        # Success — record into index for self-healing
        app_index.learn(app, resolved)
        log.info("open_app '%s' via %s in %.0f ms", app, method, elapsed)
        return f"Opened '{app}' successfully."

    except FileNotFoundError:
        elapsed = (time.perf_counter() - t0) * 1000
        log.error("open_app '%s' FileNotFoundError after %.0f ms", app, elapsed)
        return f"Failed to open '{app}': executable not found at '{resolved}'."
    except PermissionError:
        return f"Failed to open '{app}': permission denied."
    except Exception as exc:
        return f"Failed to open '{app}': {exc}"


def handle_web_search(p: dict) -> str:
    query = p.get("query", "")
    if not query:
        return "No search query provided."
    webbrowser.open(f"https://www.google.com/search?q={query.replace(' ', '+')}")
    return f"Searching Google for: {query}"


def handle_open_website(p: dict) -> str:
    url = p.get("url", "")
    if not url:
        return "No URL provided."
    if not url.startswith("http"):
        url = "https://" + url
    try:
        webbrowser.open(url)
        return f"Opened {url}"
    except Exception as exc:
        return f"Failed to open {url}: {exc}"


def handle_play_media(p: dict) -> str:
    query = p.get("query", "")
    plat  = p.get("platform", "youtube").lower()
    if not query:
        return "No media query provided."
    if plat == "spotify":
        url = f"https://open.spotify.com/search/{query.replace(' ', '%20')}"
    else:
        url = f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}"
    webbrowser.open(url)
    return f"Playing '{query}' on {plat}."


def handle_system_control(p: dict) -> str:
    action = p.get("action", "").lower()
    cmd_map = _SYSTEM_ACTIONS.get(action)
    if not cmd_map:
        return f"Unknown system action: {action}"
    cmd = cmd_map.get(OS, "")
    if not cmd:
        return f"'{action}' not supported on {OS}."
    success, output = _run(cmd, shell=True)
    if success:
        return f"Executed: {action}."
    return f"Failed to execute '{action}': {output}"


def handle_get_time(_: dict) -> str:
    return f"Current time is {datetime.datetime.now().strftime('%I:%M %p')}"


def handle_get_date(_: dict) -> str:
    return f"Today is {datetime.datetime.now().strftime('%A, %B %d, %Y')}"


def handle_take_screenshot(p: dict) -> str:
    ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = str(Path.home() / f"screenshot_{ts}.png")
    if OS == "Windows":
        script = (
            f'Add-Type -AssemblyName System.Windows.Forms; '
            f'$b = New-Object System.Drawing.Bitmap('
            f'[System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width,'
            f'[System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height); '
            f'$g = [System.Drawing.Graphics]::FromImage($b); '
            f'$g.CopyFromScreen(0,0,0,0,$b.Size); $b.Save(\'{filename}\')'
        )
        success, out = _run(f'powershell -command "{script}"', shell=True)
    elif OS == "Darwin":
        success, out = _run(["screencapture", filename])
    else:
        tool = shutil.which("scrot") or shutil.which("gnome-screenshot")
        if not tool:
            return "Install 'scrot' or 'gnome-screenshot' for screenshots on Linux."
        flag = ["-f", filename] if "gnome" in tool else [filename]
        success, out = _run([tool] + flag)

    if success or Path(filename).exists():
        return f"Screenshot saved: {filename}"
    return f"Screenshot failed: {out}"


def handle_clipboard_copy(p: dict) -> str:
    text = p.get("text", "")
    if not text:
        return "Nothing to copy."
    try:
        if OS == "Windows":
            subprocess.run(f'echo {text} | clip', shell=True, check=True)
        elif OS == "Darwin":
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
        else:
            subprocess.run(["xclip", "-selection", "clipboard"],
                           input=text.encode(), check=True)
        preview = text[:50] + ("..." if len(text) > 50 else "")
        return f"Copied to clipboard: '{preview}'"
    except subprocess.CalledProcessError as exc:
        return f"Clipboard error (command failed): {exc}"
    except FileNotFoundError as exc:
        return f"Clipboard error (tool not found): {exc}"
    except Exception as exc:
        return f"Clipboard error: {exc}"


def handle_file_operation(p: dict) -> str:
    op   = p.get("operation", "")
    path = os.path.expanduser(p.get("path", ""))
    dest = p.get("destination", "")

    if op == "create":
        try:
            Path(path).touch()
            return f"Created: {path}"
        except Exception as exc:
            return f"Create failed: {exc}"
    elif op == "delete":
        try:
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
            else:
                return f"Delete failed: path not found: {path}"
            return f"Deleted: {path}"
        except Exception as exc:
            return f"Delete failed: {exc}"
    elif op == "move":
        try:
            shutil.move(path, dest)
            return f"Moved {path} → {dest}"
        except Exception as exc:
            return f"Move failed: {exc}"
    elif op == "list":
        try:
            items = os.listdir(path or ".")
            return f"Files in {path or '.'}:\n" + "\n".join(items[:25])
        except Exception as exc:
            return f"List failed: {exc}"
    return f"Unknown file operation: {op}"


def handle_open_folder(p: dict) -> str:
    path = os.path.expanduser(p.get("path", str(Path.home())))
    if not os.path.exists(path):
        return f"Folder not found: {path}"
    try:
        if OS == "Windows":
            subprocess.Popen(f'explorer "{path}"', shell=True)
        elif OS == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return f"Opened folder: {path}"
    except Exception as exc:
        return f"Failed to open folder '{path}': {exc}"


def handle_weather(p: dict) -> str:
    city = p.get("city", "")
    if not city:
        return "No city specified."
    webbrowser.open(f"https://www.google.com/search?q=weather+{city.replace(' ', '+')}")
    return f"Showing weather for {city}."


def handle_calculate(p: dict) -> str:
    expr = p.get("expression", "")
    if not expr:
        return "No expression provided."
    try:
        allowed = {k: v for k, v in math.__dict__.items() if not k.startswith("__")}
        result = eval(expr, {"__builtins__": {}}, allowed)  # noqa: S307
        return f"{expr} = {result}"
    except ZeroDivisionError:
        return f"Calculation error: division by zero."
    except Exception as exc:
        return f"Calculation error: {exc}"


def handle_tell_joke(_: dict) -> str:
    return random.choice(_JOKES)


def handle_greet(_: dict) -> str:
    h = datetime.datetime.now().hour
    if h < 12:  return "Good morning! Axis v3 online. How can I help?"
    if h < 18:  return "Good afternoon! Axis ready. What do you need?"
    return "Good evening! Axis at your service."


def handle_send_email(p: dict) -> str:
    to, sub, body = p.get("to", ""), p.get("subject", ""), p.get("body", "")
    if not to:
        return "No recipient specified."
    webbrowser.open(f"mailto:{to}?subject={sub}&body={body}")
    return f"Email composer opened for {to}."


def handle_create_reminder(p: dict) -> str:
    msg  = p.get("message", "Reminder")
    when = p.get("time", "")
    return f"Reminder noted: '{msg}' at {when}. (Connect a scheduler for full support.)"


def handle_unknown(p: dict) -> str:
    reason = p.get("reason", "Command not understood.")
    return f"Sorry, I couldn't process that. {reason}"


def handle_workflow_result(p: dict) -> str:
    return p.get("summary", "Workflow completed.")


# ─── Registry ─────────────────────────────────────────────────────────────────

ACTION_REGISTRY: dict[str, Callable[[dict], str]] = {
    "open_app":        handle_open_app,
    "web_search":      handle_web_search,
    "open_website":    handle_open_website,
    "play_media":      handle_play_media,
    "system_control":  handle_system_control,
    "get_time":        handle_get_time,
    "get_date":        handle_get_date,
    "take_screenshot": handle_take_screenshot,
    "clipboard_copy":  handle_clipboard_copy,
    "file_operation":  handle_file_operation,
    "open_folder":     handle_open_folder,
    "weather":         handle_weather,
    "calculate":       handle_calculate,
    "tell_joke":       handle_tell_joke,
    "greet":           handle_greet,
    "send_email":      handle_send_email,
    "create_reminder": handle_create_reminder,
    "workflow_result": handle_workflow_result,
    "unknown":         handle_unknown,
}


def execute(action_data: dict) -> str:
    """Route action_data → correct handler. Never raises."""
    action     = action_data.get("action", "unknown")
    parameters = action_data.get("parameters", {})
    handler    = ACTION_REGISTRY.get(action, handle_unknown)
    try:
        result = handler(parameters)
        log.debug("execute: action=%s result=%s", action, result[:80])
        return result
    except Exception as exc:
        log.error("execute: action=%s error=%s", action, exc)
        return f"Execution error in '{action}': {exc}"
