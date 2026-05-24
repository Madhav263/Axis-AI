"""
provider_manager.py — AI Provider Manager.

Responsibilities:
  • Provider registration and priority ordering
  • Health checks (lazy + periodic)
  • Seamless failover: Gemini → Ollama → TinyModel
  • Recovery: when a provider comes back online, promote it
  • Generates natural-language responses via the best available provider

Graceful degradation order (highest capability first):
  Gemini → Ollama → TinyModel
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from axis_logger import get_logger, log_fallback, log_failure
from config_loader import CONFIG
from exceptions import AllProvidersFailedError, ProviderAuthError, ProviderError
from providers.base_provider import BaseProvider
from providers.gemini_provider import GeminiProvider
from providers.ollama_provider import OllamaProvider
from providers.tiny_model_provider import TinyModelProvider

log = get_logger("provider_manager")

_HEALTH_TTL = 60   # Re-check provider health every 60 s


class _ProviderEntry:
    def __init__(self, provider: BaseProvider, enabled: bool):
        self.provider = provider
        self.enabled = enabled
        self.last_check: float = 0.0
        self.healthy: bool = False

    def refresh(self, force: bool = False) -> bool:
        now = time.time()
        if force or (now - self.last_check) > _HEALTH_TTL:
            self.healthy = self.provider.is_available()
            self.last_check = now
        return self.healthy


class ProviderManager:
    """
    Manages multiple AI providers with priority ordering and failover.
    Thread-safe for single-threaded use (Axis CLI is single-threaded).
    """

    def __init__(self):
        layer_cfg = CONFIG.get("layers", {})

        # Ordered highest-capability first (used in reverse for intent escalation)
        self._entries: list[_ProviderEntry] = [
            _ProviderEntry(GeminiProvider(),    layer_cfg.get("gemini",    {}).get("enabled", True)),
            _ProviderEntry(OllamaProvider(),    layer_cfg.get("ollama",    {}).get("enabled", True)),
            _ProviderEntry(TinyModelProvider(), layer_cfg.get("tiny_model",{}).get("enabled", True)),
        ]
        self._current_idx: int = 0
        self._last_successful: str | None = None

    # ── Availability ──────────────────────────────────────────────────────────

    def any_available(self) -> bool:
        return any(e.refresh() for e in self._entries if e.enabled)

    def _first_healthy(self) -> _ProviderEntry | None:
        for entry in self._entries:
            if entry.enabled and entry.refresh():
                return entry
        # Force re-check all
        for entry in self._entries:
            if entry.enabled and entry.refresh(force=True):
                return entry
        return None

    # ── Parse Intent (with fallover) ─────────────────────────────────────────

    def parse_intent(self, command: str, history: list[dict]) -> dict:
        """
        Try providers in priority order until one succeeds.
        Raises AllProvidersFailedError if every provider fails.
        """
        errors: list[str] = []

        for entry in self._entries:
            if not entry.enabled:
                continue
            if not entry.refresh():
                continue

            provider = entry.provider
            try:
                result = provider.parse_intent(command, history)
                self._last_successful = provider.name
                log.info("parse_intent: provider=%s action=%s",
                         provider.name, result.get("action"))
                return result
            except ProviderAuthError as exc:
                # Auth errors are fatal for that provider — disable and continue
                log.error("Auth error for %s: %s — disabling.", provider.name, exc)
                entry.enabled = False
                errors.append(f"{provider.name}: auth error")
                continue
            except ProviderError as exc:
                prev = provider.name
                err  = str(exc)[:120]
                errors.append(f"{provider.name}: {err}")
                log_failure(provider.name, err)
                entry.healthy = False  # mark unhealthy; will re-check next call
                log.warning("Provider %s failed: %s — trying next.", prev, err)
                continue

        raise AllProvidersFailedError(
            "All AI providers failed.\n" + "\n".join(f"  • {e}" for e in errors)
        )

    # ── Generate Response (best-effort) ──────────────────────────────────────

    def generate_response(self, action: str, parameters: dict, result: str) -> str:
        """
        Generate a conversational reply. Falls back to a static message if all
        providers fail — this must never crash.
        """
        for entry in self._entries:
            if not entry.enabled or not entry.refresh():
                continue
            try:
                return entry.provider.generate_response(action, parameters, result)
            except Exception:
                continue
        # Hard fallback — always works
        return f"Done — {action.replace('_', ' ')}: {result[:80]}"

    # ── Status ────────────────────────────────────────────────────────────────

    def status_report(self) -> str:
        lines = ["AI Provider Status:"]
        for entry in self._entries:
            icon = "✓" if entry.refresh() else "✗"
            state = "enabled" if entry.enabled else "disabled"
            lines.append(f"  {icon} {entry.provider.name} [{state}]")
        return "\n".join(lines)


# Module-level singleton
provider_manager = ProviderManager()
