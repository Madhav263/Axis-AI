"""
axis_logger.py — Structured logging for Axis AI.

Creates  logs/axis.log  (rotating) and writes every command,
layer decision, provider switch, failure, and recovery event.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from config_loader import CONFIG

# ─── Setup ───────────────────────────────────────────────────────────────────

_log_cfg = CONFIG.get("logging", {})
_LOG_DIR = Path(_log_cfg.get("dir", "logs"))
_LEVEL   = _log_cfg.get("level", "INFO").upper()
_ENABLED = _log_cfg.get("enabled", True)
_MAX_B   = _log_cfg.get("max_bytes", 5_242_880)
_BACKUP  = _log_cfg.get("backup_count", 3)

# Ensure log directory exists
_LOG_DIR.mkdir(parents=True, exist_ok=True)

# Root logger for the project
_root = logging.getLogger("axis")
_root.setLevel(getattr(logging, _LEVEL, logging.INFO))

if _ENABLED and not _root.handlers:
    # File handler (rotating)
    _fh = logging.handlers.RotatingFileHandler(
        _LOG_DIR / "axis.log",
        maxBytes=_MAX_B,
        backupCount=_BACKUP,
        encoding="utf-8",
    )
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _root.addHandler(_fh)

    # Console handler (INFO+)
    _ch = logging.StreamHandler()
    _ch.setLevel(logging.WARNING)   # only warnings/errors to console
    _ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    _root.addHandler(_ch)


def get_logger(name: str) -> logging.Logger:
    """Return a child logger namespaced under 'axis'."""
    return _root.getChild(name)


# ─── Structured Event Helpers ─────────────────────────────────────────────────

_event_log = get_logger("events")


def log_command(
    command: str,
    layer: str,
    provider: str | None,
    action: str,
    result: str,
    latency_ms: float,
):
    """Log a processed command with full context."""
    _event_log.info(
        json.dumps({
            "ts": datetime.utcnow().isoformat() + "Z",
            "event": "command",
            "command": command[:200],
            "layer": layer,
            "provider": provider,
            "action": action,
            "result_snippet": result[:120],
            "latency_ms": round(latency_ms, 1),
        })
    )


def log_fallback(from_layer: str, to_layer: str, reason: str):
    """Log an intelligence-layer fallback."""
    _event_log.warning(
        json.dumps({
            "ts": datetime.utcnow().isoformat() + "Z",
            "event": "fallback",
            "from": from_layer,
            "to": to_layer,
            "reason": reason[:200],
        })
    )


def log_provider_switch(from_model: str, to_model: str, reason: str):
    """Log a provider/model switch."""
    _event_log.warning(
        json.dumps({
            "ts": datetime.utcnow().isoformat() + "Z",
            "event": "provider_switch",
            "from": from_model,
            "to": to_model,
            "reason": reason[:200],
        })
    )


def log_failure(layer: str, error: str):
    """Log a layer or provider failure."""
    _event_log.error(
        json.dumps({
            "ts": datetime.utcnow().isoformat() + "Z",
            "event": "failure",
            "layer": layer,
            "error": error[:300],
        })
    )
