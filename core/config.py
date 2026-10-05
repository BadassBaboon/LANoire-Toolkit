from __future__ import annotations
import json
import os
import shutil
from pathlib import Path
from typing import Any, Optional


CONFIG_FILENAME = "config.json"


def get_config_path() -> Path:
    """Returns the absolute path to the local config.json file."""
    return Path(__file__).resolve().parent.parent / CONFIG_FILENAME


def get_default_config() -> dict[str, Any]:
    """Default configuration schema."""
    return {
        "ffmpeg_path": "",
        "game_path": "",
        "last_open_dir": "",
    }


def load_config() -> dict[str, Any]:
    """
    Safely loads the local configuration file.
    Creates default config.json if missing.
    Returns default config on any read or parsing error without raising.
    """
    cfg_path = get_config_path()
    defaults = get_default_config()

    if not cfg_path.is_file():
        # Try to find a default ffmpeg if available
        detected = detect_all_ffmpeg_candidates()
        if detected:
            defaults["ffmpeg_path"] = detected[0]
        save_config(defaults)
        return defaults

    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return defaults
        # Ensure all expected keys exist
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
        return data
    except Exception as e:
        print(f"Warning: Failed to load {CONFIG_FILENAME} ({e}), using default configuration.")
        return defaults


def save_config(cfg: dict[str, Any]) -> bool:
    """Saves the configuration dictionary to config.json atomically."""
    cfg_path = get_config_path()
    try:
        tmp_path = cfg_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        if cfg_path.exists():
            cfg_path.unlink()
        tmp_path.rename(cfg_path)
        return True
    except Exception as e:
        print(f"Warning: Failed to save {CONFIG_FILENAME}: {e}")
        return False


def get_config_value(key: str, default: Any = None) -> Any:
    """Retrieves a single configuration value."""
    cfg = load_config()
    return cfg.get(key, default)


def set_config_value(key: str, value: Any) -> bool:
    """Sets and persists a single configuration value."""
    cfg = load_config()
    cfg[key] = value
    return save_config(cfg)


def detect_all_ffmpeg_candidates() -> list[str]:
    """
    Scans the system for all valid FFmpeg executables.
    Returns a deduplicated list of absolute paths.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add_if_valid(p_str: Optional[str]):
        if not p_str:
            return
        try:
            resolved = Path(p_str).resolve()
            if resolved.is_file() and str(resolved).lower() not in seen:
                seen.add(str(resolved).lower())
                found.append(str(resolved))
        except Exception:
            pass

    # 1. Environment variable
    add_if_valid(os.environ.get("FFMPEG_PATH"))

    # 2. System PATH
    add_if_valid(shutil.which("ffmpeg"))

    # 3. Standard Windows locations and package managers
    standard_paths = [
        Path.cwd() / "ffmpeg.exe",
        Path.cwd() / "bin" / "ffmpeg.exe",
        Path(__file__).resolve().parent.parent / "bin" / "ffmpeg.exe",
        Path(os.path.expandvars(r"%ProgramFiles%\ffmpeg\bin\ffmpeg.exe")),
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path(r"C:\ffmpeg\ffmpeg.exe"),
        Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe")),
        Path(os.path.expandvars(r"%USERPROFILE%\scoop\shims\ffmpeg.exe")),
        Path(os.path.expandvars(r"%ProgramData%\chocolatey\bin\ffmpeg.exe")),
    ]
    for p in standard_paths:
        add_if_valid(str(p))

    return found


def get_ffmpeg_path() -> Optional[str]:
    """
    Resolves the FFmpeg executable path:
    1. Checks config.json
    2. Checks FFMPEG_PATH environment variable
    3. Checks system PATH
    4. Auto-detects standard locations
    """
    # 1. Check config.json
    cfg = load_config()
    cfg_p = cfg.get("ffmpeg_path", "").strip()
    if cfg_p and Path(cfg_p).is_file():
        return cfg_p

    # 2. Check environment variable
    env_p = os.environ.get("FFMPEG_PATH", "").strip()
    if env_p and Path(env_p).is_file():
        set_config_value("ffmpeg_path", env_p)
        return env_p

    # 3. Check system PATH and standard locations
    candidates = detect_all_ffmpeg_candidates()
    if candidates:
        first = candidates[0]
        set_config_value("ffmpeg_path", first)
        return first

    return None


def set_ffmpeg_path(path_str: str) -> bool:
    """Sets and validates the FFmpeg executable path."""
    p = Path(path_str).resolve()
    if not p.is_file():
        return False
    os.environ["FFMPEG_PATH"] = str(p)
    return set_config_value("ffmpeg_path", str(p))
