"""KEY=VALUE configuration file shared by credentials and options."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def parse_env_file(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or " " in key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def config_files(environ: Mapping[str, str], config_dir: Path) -> list[Path]:
    """``$DOMAIN_I_ENV`` alone if set, else every ``*.env`` in ``config_dir`` by name."""
    explicit = environ.get("DOMAIN_I_ENV")
    if explicit:
        return [Path(os.path.expanduser(explicit))]
    try:
        return sorted(p for p in config_dir.glob("*.env") if p.is_file())
    except OSError:
        return []


def read_config(environ: Mapping[str, str], config_dir: Path) -> dict[str, str]:
    """Merge the config files; later files (by name) override earlier ones."""
    merged: dict[str, str] = {}
    for path in config_files(environ, config_dir):
        try:
            merged.update(parse_env_file(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    return merged


def default_config_dir(environ: Mapping[str, str]) -> Path:
    base = environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "domain-i"


def default_cache_dir(environ: Mapping[str, str]) -> Path:
    base = environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "domain-i"
