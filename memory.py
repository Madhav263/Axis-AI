"""
memory.py — Short-term conversational memory and entity tracking.

Tracks:
  • Recent conversation turns (user / assistant pairs)
  • Named entities (last app opened, last file mentioned, etc.)
    so pronouns like "it", "that", "the same one" resolve correctly.

All state is in-process (no persistence between sessions).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from config_loader import CONFIG

_mem_cfg = CONFIG.get("memory", {})
_MAX_PAIRS = _mem_cfg.get("max_history_pairs", 10)
_ENTITY_TTL = _mem_cfg.get("entity_ttl_seconds", 300)


# ─── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class Turn:
    role: str       # "user" | "assistant"
    text: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class Entity:
    name: str       # e.g. "chrome", "~/Documents/report.pdf"
    kind: str       # "app" | "file" | "folder" | "url" | "query"
    timestamp: float = field(default_factory=time.time)


# ─── Memory Store ─────────────────────────────────────────────────────────────

class Memory:
    """Thread-safe (single-threaded) conversational memory store."""

    def __init__(self):
        self._turns: deque[Turn] = deque(maxlen=_MAX_PAIRS * 2)
        self._entities: dict[str, Entity] = {}          # kind → Entity
        self._last_action: dict[str, Any] | None = None  # last executed action_data

    # ── Turns ──────────────────────────────────────────────────────────────

    def add_user(self, text: str) -> None:
        self._turns.append(Turn("user", text))

    def add_assistant(self, text: str) -> None:
        self._turns.append(Turn("assistant", text))

    def get_history(self) -> list[dict]:
        """Return history as list of {role, text} dicts for provider prompts."""
        return [{"role": t.role, "content": t.text} for t in self._turns]

    # ── Entities ────────────────────────────────────────────────────────────

    def track_entity(self, name: str, kind: str) -> None:
        """Record a named entity (app, file, url, …)."""
        self._entities[kind] = Entity(name=name, kind=kind)

    def resolve_pronoun(self, text: str) -> str:
        """
        Replace vague references ("it", "that app", "the same one") in text
        with the most recently tracked entity of the appropriate kind.
        """
        now = time.time()
        lower = text.lower()

        # App pronoun resolution
        if any(p in lower for p in ("it", "that app", "the app", "same app")):
            ent = self._entities.get("app")
            if ent and (now - ent.timestamp) < _ENTITY_TTL:
                text = text.replace("it", ent.name).replace(
                    "that app", ent.name).replace("the app", ent.name).replace(
                    "same app", ent.name)

        # File pronoun resolution
        if any(p in lower for p in ("that file", "the file", "it")):
            ent = self._entities.get("file")
            if ent and (now - ent.timestamp) < _ENTITY_TTL:
                text = text.replace("that file", ent.name).replace(
                    "the file", ent.name)

        return text

    # ── Last Action ─────────────────────────────────────────────────────────

    def set_last_action(self, action_data: dict) -> None:
        self._last_action = action_data
        # Auto-track entities from the action
        params = action_data.get("parameters", {})
        action = action_data.get("action", "")
        if action == "open_app" and "app" in params:
            self.track_entity(params["app"], "app")
        elif action in ("file_operation", "open_folder") and "path" in params:
            self.track_entity(params["path"], "file")
        elif action == "open_website" and "url" in params:
            self.track_entity(params["url"], "url")

    def get_last_action(self) -> dict | None:
        return self._last_action

    # ── Reset ────────────────────────────────────────────────────────────────

    def clear(self) -> None:
        self._turns.clear()
        self._entities.clear()
        self._last_action = None


# Module-level singleton
memory = Memory()
