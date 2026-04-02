"""
config.py
Configuration loader for the Orchestrator backend.
Supports JSON/YAML and environment variable overrides.
"""

from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml
except ImportError:
    yaml = None


def load_config(path: str | Path) -> Dict[str, Any]:
    """
    Load configuration from a JSON or YAML file.

    Args:
        path: Path to config file.

    Returns:
        Parsed configuration dictionary.
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    if path.suffix.lower() in {".yaml", ".yml"}:
        if yaml is None:
            raise RuntimeError("PyYAML is required for YAML config files.")
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    elif path.suffix.lower() == ".json":
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        raise ValueError(f"Unsupported config format: {path.suffix}")

    return apply_env_overrides(config)


def apply_env_overrides(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Override config values using environment variables.
    Convention: ENV vars use uppercase and underscores, e.g.:
        SAGE_HOST overrides config["sagetv"]["host"]

    Args:
        config: Base config dictionary.

    Returns:
        Updated config dictionary.
    """
    for key, value in os.environ.items():
        parts = key.lower().split("_")
        ref = config

        try:
            for p in parts[:-1]:
                ref = ref[p]
            ref[parts[-1]] = value
        except Exception:
            # Ignore env vars that don't map cleanly
            pass

    return config
