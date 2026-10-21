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

    config = apply_env_overrides(config)
    return validate_config(config)


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


def validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Validate key orchestrator config values.

    Raises:
        ValueError: for invalid configuration values.
    """
    agent_cfg = config.get("agent", {}) or {}
    planner = str(agent_cfg.get("planner", "agentloop")).strip().lower()
    allowed_planners = {"agentloop", "openclaw"}
    if planner not in allowed_planners:
        raise ValueError(
            f"Invalid agent.planner '{planner}'. Allowed values: {sorted(allowed_planners)}"
        )

    openclaw_cfg = agent_cfg.get("openclaw", {}) or {}
    runtime_callable = openclaw_cfg.get("runtime_callable", "")
    if runtime_callable and (not isinstance(runtime_callable, str) or ":" not in runtime_callable):
        raise ValueError(
            "Invalid agent.openclaw.runtime_callable. Expected 'module.submodule:function'"
        )

    timeout_ms = openclaw_cfg.get("timeout_ms", 30000)
    try:
        timeout_val = int(timeout_ms)
    except Exception as exc:
        raise ValueError("Invalid agent.openclaw.timeout_ms; must be an integer") from exc
    if timeout_val <= 0:
        raise ValueError("Invalid agent.openclaw.timeout_ms; must be > 0")

    ui_cfg = config.get("ui", {}) or {}
    if ui_cfg:
        if ui_cfg.get("protocol", "a2ui-v0.9.1") != "a2ui-v0.9.1":
            raise ValueError("Invalid ui.protocol; only a2ui-v0.9.1 is supported")
        if ui_cfg.get("mode", "deterministic") != "deterministic":
            raise ValueError("Invalid ui.mode; only deterministic mode is supported")
        if ui_cfg.get(
            "catalog_id", "urn:llm-remote:a2ui:catalog:v1"
        ) != "urn:llm-remote:a2ui:catalog:v1":
            raise ValueError("Invalid ui.catalog_id; unsupported catalog")
        if ui_cfg.get("allow_model_layout", False):
            raise ValueError("ui.allow_model_layout is not supported")
        try:
            max_components = int(ui_cfg.get("max_components_per_surface", 100))
            ttl_seconds = int(ui_cfg.get("pending_action_ttl_seconds", 300))
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid UI numeric configuration") from exc
        if not 1 <= max_components <= 500:
            raise ValueError("ui.max_components_per_surface must be between 1 and 500")
        if not 30 <= ttl_seconds <= 3600:
            raise ValueError("ui.pending_action_ttl_seconds must be between 30 and 3600")

    return config
