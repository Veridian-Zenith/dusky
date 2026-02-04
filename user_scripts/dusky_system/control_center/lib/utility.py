"""
Utility functions for the Dusky Control Center.

Thread-safe, secure utility library for GTK4 control center on Arch Linux (Hyprland).
All file I/O is atomic. All public functions are safe to call from any thread.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from gi.repository import Adw

# =============================================================================
# CONSTANTS & PATHS
# =============================================================================
LABEL_NA = "N/A"


def _get_xdg_path(env_var: str, default_suffix: str) -> Path:
    """Get XDG path, properly handling empty string values."""
    value = os.environ.get(env_var, "").strip()
    if value:
        return Path(value)
    return Path.home() / default_suffix


_XDG_CACHE_HOME = _get_xdg_path("XDG_CACHE_HOME", ".cache")
_XDG_CONFIG_HOME = _get_xdg_path("XDG_CONFIG_HOME", ".config")

CACHE_DIR = _XDG_CACHE_HOME / "duskycc"
SETTINGS_DIR = _XDG_CONFIG_HOME / "dusky" / "settings"

# Resolved at first use to ensure directory exists; protected by lock
_RESOLVED_SETTINGS_DIR: Path | None = None
_SETTINGS_DIR_LOCK = threading.Lock()

# Characters requiring shell interpretation
_SHELL_METACHARACTERS = frozenset("|&;><$`\\\"'*?[](){}!")

# Thread-safe cache for static system information
_SYSTEM_INFO_CACHE: dict[str, str] = {}
_SYSTEM_INFO_LOCK = threading.Lock()


def _get_resolved_settings_dir() -> Path:
    """Lazily resolve and cache the settings directory path (thread-safe)."""
    global _RESOLVED_SETTINGS_DIR
    
    # Fast path: already resolved
    if _RESOLVED_SETTINGS_DIR is not None:
        return _RESOLVED_SETTINGS_DIR
    
    with _SETTINGS_DIR_LOCK:
        # Double-check after acquiring lock
        if _RESOLVED_SETTINGS_DIR is None:
            SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
            _RESOLVED_SETTINGS_DIR = SETTINGS_DIR.resolve(strict=True)
        return _RESOLVED_SETTINGS_DIR


def get_cache_dir() -> Path:
    """Get or create the cache directory."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


# =============================================================================
# CONFIGURATION LOADER
# =============================================================================
def load_config(config_path: Path) -> dict[str, Any]:
    """Load and parse a YAML configuration file.
    
    Handles missing files and parse errors gracefully. Thread-safe.
    """
    try:
        content = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"[INFO] Config not found: {config_path}")
        return {}
    except OSError as e:
        print(f"[ERROR] Config read error: {e}")
        return {}

    try:
        data = yaml.safe_load(content)
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError as e:
        print(f"[ERROR] YAML parse error in {config_path}: {e}")
        return {}


# =============================================================================
# UWSM-COMPLIANT COMMAND RUNNER
# =============================================================================
def execute_command(cmd_string: str, title: str, run_in_terminal: bool) -> bool:
    """Execute a command via uwsm-app, optionally in a terminal.
    
    Thread-safe. Uses fire-and-forget pattern with proper process isolation.
    Returns True if the process was successfully spawned.
    """
    if not cmd_string:
        return False

    expanded_cmd = os.path.expanduser(os.path.expandvars(cmd_string)).strip()
    if not expanded_cmd:
        return False

    # Sanitize title: remove control characters that could cause issues
    safe_title = "".join(
        c if c.isprintable() and c not in '\n\r\t' else ' ' 
        for c in (title or "Dusky Terminal")
    ).strip() or "Dusky Terminal"

    try:
        if run_in_terminal:
            full_cmd = [
                "uwsm-app", "--",
                "kitty", "--class", "dusky-term", "--title", safe_title, "--hold",
                "sh", "-c", expanded_cmd,
            ]
        else:
            needs_shell = any(c in expanded_cmd for c in _SHELL_METACHARACTERS)

            if needs_shell:
                full_cmd = ["uwsm-app", "--", "sh", "-c", expanded_cmd]
            else:
                try:
                    parsed = shlex.split(expanded_cmd)
                    if not parsed:
                        return False
                    full_cmd = ["uwsm-app", "--"] + parsed
                except ValueError:
                    # Malformed quoting; fall back to shell
                    full_cmd = ["uwsm-app", "--", "sh", "-c", expanded_cmd]

        subprocess.Popen(
            full_cmd,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return True

    except FileNotFoundError:
        print("[ERROR] uwsm-app or required command not found in PATH")
        return False
    except OSError as e:
        print(f"[ERROR] Execute failed: {e}")
        return False


# =============================================================================
# PRE-FLIGHT DEPENDENCY CHECK
# =============================================================================
def preflight_check() -> None:
    """Verify all required dependencies before startup. Exits on failure."""
    missing: list[str] = []
    warnings: list[str] = []

    # Note: PyYAML is already imported at module level; if missing, we wouldn't reach here

    # Check GTK/Adwaita bindings
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk  # noqa: F401
    except ImportError:
        missing.append("python-gobject")
    except ValueError as e:
        err = str(e).lower()
        if "gtk" in err:
            missing.append("gtk4")
        elif "adw" in err:
            missing.append("libadwaita")
        else:
            missing.append("python-gobject")

    # Check for uwsm-app (hard requirement per UWSM compliance)
    if shutil.which("uwsm-app") is None:
        missing.append("uwsm")

    if missing:
        print(f"\n[FATAL] Missing dependencies: {', '.join(missing)}")
        print(f"Install with: sudo pacman -S {' '.join(missing)}\n")
        sys.exit(1)

    # Ensure settings directory exists and is writable
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        test_file = SETTINGS_DIR / ".write_test"
        test_file.touch()
        test_file.unlink()
    except OSError as e:
        warnings.append(f"Settings directory issue ({SETTINGS_DIR}): {e}")

    for warn in warnings:
        print(f"[WARN] {warn}")


# =============================================================================
# SYSTEM VALUE RETRIEVAL (CACHED, THREAD-SAFE)
# =============================================================================
def get_system_value(key: str) -> str:
    """Retrieve system information with thread-safe caching.
    
    Values are computed once and cached permanently (they're static).
    """
    # Fast path without lock (dict.get is atomic in CPython)
    cached = _SYSTEM_INFO_CACHE.get(key)
    if cached is not None:
        return cached

    with _SYSTEM_INFO_LOCK:
        # Double-check after acquiring lock (another thread may have populated)
        if key in _SYSTEM_INFO_CACHE:
            return _SYSTEM_INFO_CACHE[key]

        result = _compute_system_value(key)
        _SYSTEM_INFO_CACHE[key] = result  # Cache even LABEL_NA to prevent repeated failures
        return result


def _compute_system_value(key: str) -> str:
    """Compute a system value. Called with lock held."""
    if key == "memory_total":
        return _get_memory_total()
    elif key == "cpu_model":
        return _get_cpu_model()
    elif key == "gpu_model":
        return _get_gpu_model()
    elif key == "kernel_version":
        return os.uname().release
    return LABEL_NA


def _get_memory_total() -> str:
    """Read total memory from /proc/meminfo."""
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        kb = int(parts[1])
                        gb = round(kb / 1_048_576, 1)  # 1024 * 1024
                        return f"{gb} GB"
                    break
    except (OSError, ValueError, IndexError):
        pass
    return LABEL_NA


def _get_cpu_model() -> str:
    """Read CPU model from /proc/cpuinfo."""
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if line.startswith("model name"):
                    _, _, value = line.partition(":")
                    raw = value.strip()
                    # Remove frequency suffix (e.g., "@ 3.50GHz")
                    base, _, _ = raw.partition("@")
                    return base.strip() or raw
    except (OSError, ValueError):
        pass
    return LABEL_NA


def _get_gpu_model() -> str:
    """Get GPU model from lspci."""
    try:
        result = subprocess.run(
            ["lspci"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "VGA compatible controller" in line or "3D controller" in line:
                    parts = line.split(":", 2)
                    if len(parts) > 2:
                        return parts[2].strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return LABEL_NA


# =============================================================================
# SETTINGS PERSISTENCE (ATOMIC, THREAD-SAFE)
# =============================================================================
def _validate_key_path(key: str) -> Path | None:
    """Validate a settings key and return its safe filesystem path.
    
    Returns None if the key is invalid or would escape the settings directory.
    """
    if not key or not isinstance(key, str):
        return None

    # Block null bytes (can bypass path checks on some systems)
    if "\0" in key:
        print(f"[WARN] Invalid null byte in settings key: {key!r}")
        return None

    try:
        resolved_base = _get_resolved_settings_dir()
        key_path = (SETTINGS_DIR / key).resolve()
        
        # Security: ensure path is within settings directory
        key_path.relative_to(resolved_base)
        return key_path
        
    except (ValueError, OSError) as e:
        print(f"[WARN] Path traversal or invalid key blocked: {key!r}")
        return None


def save_setting(key: str, value: Any, as_int: bool = False) -> None:
    """Atomically save a setting value to a file.
    
    Thread-safe and crash-safe. Uses write-to-temp-then-rename pattern.
    """
    key_path = _validate_key_path(key)
    if key_path is None:
        return

    if as_int and isinstance(value, bool):
        content = str(int(value))
    else:
        content = str(value)

    tmp_path = None
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: create temp file in same directory, then rename
        fd, tmp_path = tempfile.mkstemp(
            dir=key_path.parent,
            prefix=f".{key_path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        except:
            os.close(fd)
            raise

        os.replace(tmp_path, key_path)  # Atomic on POSIX
        tmp_path = None  # Rename succeeded, don't clean up

    except OSError as e:
        print(f"[WARN] Failed to save setting '{key}': {e}")
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def load_setting(key: str, default: Any = None, is_inversed: bool = False) -> Any:
    """Load a setting value from a file.
    
    Thread-safe. Returns the default value if the file doesn't exist or on error.
    Type is inferred from the default value.
    """
    key_path = _validate_key_path(key)
    if key_path is None:
        return default

    try:
        value = key_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return default
    except OSError as e:
        print(f"[WARN] Failed to load setting '{key}': {e}")
        return default

    try:
        if isinstance(default, bool):
            return _parse_bool(value, is_inversed)
        elif isinstance(default, int):
            return int(value)
        elif isinstance(default, float):
            return float(value)
        return value
    except ValueError as e:
        print(f"[WARN] Failed to parse setting '{key}' value '{value}': {e}")
        return default


def _parse_bool(value: str, is_inversed: bool) -> bool:
    """Parse a string value as boolean, optionally inverting the result."""
    try:
        result = int(value) != 0
    except ValueError:
        result = value.lower() in ("true", "yes", "on", "1")
    return result ^ is_inversed


# =============================================================================
# UI HELPERS (THREAD-SAFE)
# =============================================================================
def toast(toast_overlay: Adw.ToastOverlay | None, message: str, timeout: int = 2) -> None:
    """Display a toast notification, safe to call from any thread.
    
    Automatically marshals to the main thread if needed.
    """
    if toast_overlay is None:
        return

    from gi.repository import Adw, GLib

    def _show_toast() -> bool:
        try:
            toast_overlay.add_toast(Adw.Toast(title=message, timeout=timeout))
        except Exception as e:
            print(f"[WARN] Failed to show toast: {e}")
        return GLib.SOURCE_REMOVE

    # Check if we're on the main thread
    context = GLib.MainContext.default()
    if context.is_owner():
        _show_toast()
    else:
        GLib.idle_add(_show_toast)
