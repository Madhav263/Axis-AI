"""
app_discovery.py — Axis AI Application Discovery System v3.0

Builds and maintains a local index of installed applications so Axis can
launch them without hardcoded paths.

Discovery methods (run on startup in this order):
  1. Windows Registry  — HKLM/HKCU App Paths key (fastest, most reliable)
  2. Start Menu scan   — .lnk shortcuts in all Start Menu folders
  3. Common program dirs — ProgramFiles, ProgramFiles(x86), LocalAppData/Programs
  4. Desktop shortcuts  — user Desktop .lnk files

Fallback (at launch time, if app not in index):
  5. Windows Search    — opens Start, types the name, presses Enter

Self-healing:
  If an app is found via Windows Search, its path is saved to the index
  so the next launch uses the faster direct-path method.

Cache:
  Stored at ~/.axis/app_index.json
  Rebuilt automatically on first run or if older than CACHE_TTL_HOURS.

Public API:
  app_index.resolve(name: str) -> str | None
  app_index.rebuild()
  app_index.save()
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from axis_logger import get_logger

log = get_logger("app_discovery")

OS = platform.system()  # 'Windows' | 'Darwin' | 'Linux'

# Rebuild cache if older than this (hours)
CACHE_TTL_HOURS = 24

# Where the cache lives
_CACHE_DIR  = Path.home() / ".axis"
_CACHE_FILE = _CACHE_DIR / "app_index.json"


# ─── Utilities ────────────────────────────────────────────────────────────────

def _normalise_name(name: str) -> str:
    """Lowercase, strip common suffixes, collapse whitespace."""
    name = name.lower().strip()
    for suffix in (".exe", ".lnk", ".app", " (x86)", " (x64)", " - shortcut"):
        name = name.replace(suffix, "")
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _resolve_lnk(lnk_path: str) -> str | None:
    """Resolve a Windows .lnk shortcut to its target executable path."""
    try:
        import winreg  # noqa: F401 — confirms we're on Windows
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"(New-Object -ComObject WScript.Shell)"
                f".CreateShortcut('{lnk_path}').TargetPath"
            ],
            capture_output=True, text=True, timeout=5
        )
        target = result.stdout.strip()
        if target and Path(target).exists():
            return target
    except Exception:
        pass
    return None


# ─── Windows Discovery ────────────────────────────────────────────────────────

def _scan_registry() -> dict[str, str]:
    """Read HKLM & HKCU App Paths from Windows registry."""
    if OS != "Windows":
        return {}
    index: dict[str, str] = {}
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
            try:
                root = winreg.OpenKey(hive, key_path)
            except FileNotFoundError:
                continue
            i = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root, i)
                    i += 1
                    sub = winreg.OpenKey(root, sub_name)
                    try:
                        exe_path, _ = winreg.QueryValueEx(sub, "")
                        exe_path = exe_path.strip().strip('"')
                        if exe_path and Path(exe_path).exists():
                            canonical = _normalise_name(sub_name)
                            index[canonical] = exe_path
                    except FileNotFoundError:
                        pass
                    winreg.CloseKey(sub)
                except OSError:
                    break
            winreg.CloseKey(root)
    except Exception as exc:
        log.warning("Registry scan failed: %s", exc)
    log.debug("Registry scan: %d apps found", len(index))
    return index


def _scan_start_menu() -> dict[str, str]:
    """Walk all Start Menu folders for .lnk files and resolve their targets."""
    if OS != "Windows":
        return {}
    index: dict[str, str] = {}
    start_dirs = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    for start_dir in start_dirs:
        if not start_dir.exists():
            continue
        for lnk in start_dir.rglob("*.lnk"):
            target = _resolve_lnk(str(lnk))
            if target:
                canonical = _normalise_name(lnk.stem)
                index[canonical] = target
    log.debug("Start menu scan: %d apps found", len(index))
    return index


def _scan_program_dirs() -> dict[str, str]:
    """Glob common installation directories for .exe files (one level deep)."""
    if OS != "Windows":
        return {}
    index: dict[str, str] = {}
    search_roots = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
    ]
    for root in search_roots:
        if not root.exists():
            continue
        # Go two levels deep: root/<vendor>/<app>.exe  or  root/<app>/<app>.exe
        for exe in list(root.glob("*/*.exe")) + list(root.glob("*.exe")):
            canonical = _normalise_name(exe.stem)
            if canonical not in index:
                index[canonical] = str(exe)
    log.debug("Program dirs scan: %d apps found", len(index))
    return index


def _scan_desktop() -> dict[str, str]:
    """Scan the user Desktop for .lnk shortcuts."""
    if OS != "Windows":
        return {}
    index: dict[str, str] = {}
    desktop = Path.home() / "Desktop"
    if not desktop.exists():
        # OneDrive-moved desktop
        desktop = Path.home() / "OneDrive" / "Desktop"
    if desktop.exists():
        for lnk in desktop.glob("*.lnk"):
            target = _resolve_lnk(str(lnk))
            if target:
                canonical = _normalise_name(lnk.stem)
                index[canonical] = target
    log.debug("Desktop scan: %d apps found", len(index))
    return index


# ─── macOS / Linux Discovery ──────────────────────────────────────────────────

def _scan_macos() -> dict[str, str]:
    if OS != "Darwin":
        return {}
    index: dict[str, str] = {}
    for search_dir in ("/Applications", str(Path.home() / "Applications")):
        p = Path(search_dir)
        if not p.exists():
            continue
        for app_bundle in p.glob("*.app"):
            name = _normalise_name(app_bundle.stem)
            index[name] = str(app_bundle)
    log.debug("macOS scan: %d apps found", len(index))
    return index


def _scan_linux() -> dict[str, str]:
    if OS != "Linux":
        return {}
    index: dict[str, str] = {}
    # Parse .desktop files
    desktop_dirs = [
        Path("/usr/share/applications"),
        Path("/usr/local/share/applications"),
        Path.home() / ".local/share/applications",
    ]
    for d in desktop_dirs:
        if not d.exists():
            continue
        for desk in d.glob("*.desktop"):
            try:
                content = desk.read_text(errors="ignore")
                exec_line = next(
                    (l for l in content.splitlines() if l.startswith("Exec=")), None
                )
                name_line = next(
                    (l for l in content.splitlines() if l.startswith("Name=")), None
                )
                if exec_line and name_line:
                    exe = exec_line.split("=", 1)[1].split()[0].strip()
                    name = _normalise_name(name_line.split("=", 1)[1])
                    if exe:
                        index[name] = exe
            except Exception:
                pass
    log.debug("Linux scan: %d apps found", len(index))
    return index


# ─── Windows Search Fallback ──────────────────────────────────────────────────

def _windows_search_launch(app_name: str) -> bool:
    """
    Open Windows Search, type the app name, and press Enter.
    Returns True if the sequence was sent (not if the app actually launched —
    that's confirmed by the caller checking process state).
    """
    if OS != "Windows":
        return False
    try:
        # Use PowerShell + WScript.Shell to simulate Win key, type, Enter
        script = (
            f'$wsh = New-Object -ComObject WScript.Shell; '
            f'$wsh.SendKeys("^{{ESC}}"); '
            f'Start-Sleep -Milliseconds 600; '
            f'$wsh.SendKeys("{app_name}"); '
            f'Start-Sleep -Milliseconds 800; '
            f'$wsh.SendKeys("{{ENTER}}"); '
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", script],
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0,
        )
        time.sleep(2.0)   # wait for the app to start
        return True
    except Exception as exc:
        log.warning("Windows Search fallback failed: %s", exc)
        return False


# ─── Alias Map ────────────────────────────────────────────────────────────────
# Maps user-friendly names → normalised keys that may differ from exe names.

_ALIASES: dict[str, list[str]] = {
    "chrome":       ["google chrome", "chrome"],
    "google chrome":["google chrome", "chrome"],
    "edge":         ["microsoft edge", "msedge", "edge"],
    "vscode":       ["visual studio code", "code", "vscode"],
    "vs code":      ["visual studio code", "code", "vscode"],
    "notepad++":    ["notepad++", "notepad plus plus"],
    "discord":      ["discord"],
    "telegram":     ["telegram", "telegram desktop"],
    "whatsapp":     ["whatsapp", "whatsapp desktop"],
    "obs":          ["obs studio", "obs64", "obs"],
    "capcut":       ["capcut", "cap cut"],
    "steam":        ["steam"],
    "spotify":      ["spotify"],
    "vlc":          ["vlc media player", "vlc"],
    "word":         ["microsoft word", "winword"],
    "excel":        ["microsoft excel", "excel"],
    "powerpoint":   ["microsoft powerpoint", "powerpnt"],
    "zoom":         ["zoom", "zoom meetings"],
    "slack":        ["slack"],
    "teams":        ["microsoft teams", "teams"],
    "notepad":      ["notepad"],
    "calculator":   ["calculator", "calc"],
    "paint":        ["paint", "mspaint"],
    "explorer":     ["file explorer", "windows explorer", "explorer"],
    "terminal":     ["windows terminal", "wt"],
    "cmd":          ["command prompt", "cmd"],
    "powershell":   ["powershell", "windows powershell"],
    "task manager": ["task manager", "taskmgr"],
    "settings":     ["settings", "ms-settings"],
    "firefox":      ["firefox", "mozilla firefox"],
}


# ─── Application Index ────────────────────────────────────────────────────────

class AppIndex:
    """
    Central application index. Singleton used by executor.py.

    Usage:
        from app_discovery import app_index
        path = app_index.resolve("chrome")   # -> "C:\\...\\chrome.exe" or None
    """

    def __init__(self):
        self._index: dict[str, str] = {}     # normalised_name -> executable path
        self._built_at: float = 0.0
        self._loaded = False

    # ── Build / Load ──────────────────────────────────────────────────────

    def _load_cache(self) -> bool:
        """Load cached index from disk. Returns True if cache is fresh."""
        if not _CACHE_FILE.exists():
            return False
        try:
            data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            age_hours = (time.time() - data.get("built_at", 0)) / 3600
            if age_hours > CACHE_TTL_HOURS:
                log.info("App index cache expired (%.1fh old). Rebuilding.", age_hours)
                return False
            self._index = data.get("index", {})
            self._built_at = data.get("built_at", 0)
            log.info("App index loaded from cache: %d apps.", len(self._index))
            return True
        except Exception as exc:
            log.warning("Failed to load app index cache: %s", exc)
            return False

    def rebuild(self) -> None:
        """Scan the system and rebuild the index. Called on startup."""
        log.info("Building application index...")
        t0 = time.perf_counter()

        merged: dict[str, str] = {}

        # Platform-specific scans
        if OS == "Windows":
            for scanner in (_scan_registry, _scan_start_menu,
                            _scan_program_dirs, _scan_desktop):
                try:
                    merged.update(scanner())
                except Exception as exc:
                    log.warning("Scanner %s failed: %s", scanner.__name__, exc)
        elif OS == "Darwin":
            merged.update(_scan_macos())
        else:
            merged.update(_scan_linux())

        self._index = merged
        self._built_at = time.time()

        elapsed = (time.perf_counter() - t0) * 1000
        log.info("App index built: %d apps in %.0f ms.", len(self._index), elapsed)
        self.save()

    def save(self) -> None:
        """Persist the current index to disk."""
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _CACHE_FILE.write_text(
                json.dumps({"built_at": self._built_at, "index": self._index},
                           indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log.debug("App index saved: %d entries.", len(self._index))
        except Exception as exc:
            log.warning("Failed to save app index: %s", exc)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._load_cache():
            self.rebuild()

    # ── Resolution ────────────────────────────────────────────────────────

    def _lookup(self, key: str) -> str | None:
        """Direct lookup in index."""
        key = _normalise_name(key)
        if key in self._index:
            path = self._index[key]
            if Path(path).exists() if os.path.sep in path else True:
                return path
        return None

    def resolve(self, app_name: str) -> str | None:
        """
        Resolve an app name to a launchable path/command.

        Resolution order:
          1. Direct key lookup in index
          2. Alias expansion → lookup each alias
          3. Partial-match scan (longest matching key)
          4. Windows Search fallback (launches and returns None to signal async launch)
        """
        self._ensure_loaded()

        norm = _normalise_name(app_name)

        # 1. Direct
        path = self._lookup(norm)
        if path:
            log.debug("resolve '%s' → direct hit: %s", app_name, path)
            return path

        # 2. Alias expansion
        for alias_key, candidates in _ALIASES.items():
            if norm == alias_key or norm in candidates:
                for candidate in candidates:
                    path = self._lookup(candidate)
                    if path:
                        log.debug("resolve '%s' → alias '%s': %s", app_name, candidate, path)
                        # Cache under original name for next time
                        self._index[norm] = path
                        return path

        # 3. Partial match (e.g. "code" matches "visual studio code")
        matches = [
            (k, v) for k, v in self._index.items()
            if norm in k or k in norm
        ]
        if matches:
            # Prefer the shortest matching key (most specific)
            best_key, best_path = min(matches, key=lambda kv: len(kv[0]))
            log.debug("resolve '%s' → partial match '%s': %s", app_name, best_key, best_path)
            return best_path

        # 4. Windows Search fallback
        log.info("'%s' not in index — trying Windows Search fallback.", app_name)
        launched = _windows_search_launch(app_name)
        if launched:
            # We can't know the path after a Search launch, so mark as "search-launched"
            self._index[norm] = f"__search__{app_name}"
            self.save()
            # Return a sentinel so executor knows this was a search launch
            return f"__search__{app_name}"

        return None

    def learn(self, app_name: str, path: str) -> None:
        """
        Record a newly discovered path (self-healing).
        Called by executor when it successfully launches an app by any method.
        """
        self._ensure_loaded()
        key = _normalise_name(app_name)
        if self._index.get(key) != path:
            self._index[key] = path
            self.save()
            log.info("App index updated: '%s' → %s", app_name, path)

    def status(self) -> str:
        self._ensure_loaded()
        age_h = (time.time() - self._built_at) / 3600
        return (
            f"App index: {len(self._index)} apps  "
            f"(built {age_h:.1f}h ago  •  cache: {_CACHE_FILE})"
        )


# Module-level singleton
app_index = AppIndex()
