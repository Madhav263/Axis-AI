"""
config_loader.py — Loads and validates config.yaml.
Falls back to safe defaults if the file is missing or malformed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

logger = logging.getLogger(__name__)

# Absolute path of this file's directory
_HERE = Path(__file__).parent


# ─── Hardcoded Defaults ───────────────────────────────────────────────────────
# Mirrors config.yaml so Axis works even without the file.

_DEFAULTS: dict[str, Any] = {
    "axis": {"version": "2.0", "language": "en"},
    "layers": {
        "local_command": {"enabled": True, "priority": 0},
        "rule_nlp":      {"enabled": True, "priority": 1},
        "workflow":      {"enabled": True, "priority": 2},
        "tiny_model":    {"enabled": True, "priority": 3},
        "ollama":        {"enabled": True, "priority": 4},
        "gemini":        {"enabled": True, "priority": 5},
    },
    "gemini": {
        "models": [
            "gemini-1.5-flash-latest",
            "gemini-1.5-flash-001",
            "gemini-2.0-flash",
            "gemini-1.5-pro-latest",
        ],
        "max_retries": 3,
        "timeout": 30,
        "max_output_tokens": 512,
        "temperature": 0.1,
        "response_temperature": 0.7,
    },
    "ollama": {
        "base_url": "http://localhost:11434",
        "models": ["llama3", "llama3.1", "mistral", "gemma"],
        "max_retries": 2,
        "timeout": 60,
    },
    "tiny_model": {
        "model_path": "",
        "max_tokens": 256,
        "temperature": 0.1,
    },
    "memory": {
        "max_history_pairs": 10,
        "entity_ttl_seconds": 300,
    },
    "safety": {
        "confirm_destructive": True,
        "destructive_actions": ["delete", "move", "shutdown", "restart", "format"],
    },
    "logging": {
        "enabled": True,
        "dir": "logs",
        "level": "INFO",
        "max_bytes": 5_242_880,
        "backup_count": 3,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (non-destructive)."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """
    Load config.yaml and merge with defaults.
    Returns a plain dict — never raises.
    """
    config_path = Path(path) if path else _HERE / "config.yaml"

    if not _YAML_AVAILABLE:
        logger.warning("PyYAML not installed — using built-in defaults. pip install pyyaml")
        return dict(_DEFAULTS)

    if not config_path.exists():
        logger.warning("config.yaml not found at %s — using defaults.", config_path)
        return dict(_DEFAULTS)

    try:
        with config_path.open("r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        return _deep_merge(_DEFAULTS, user_cfg)
    except Exception as exc:
        logger.error("Failed to parse config.yaml: %s — using defaults.", exc)
        return dict(_DEFAULTS)


# Module-level singleton
CONFIG: dict[str, Any] = load_config()
