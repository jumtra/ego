"""Packaged defaults (src/ego/defaults.toml) and the project [tuning] override (.ego/defaults.toml)."""
from __future__ import annotations
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any
import copy
import math
import tomllib
from ..shared.util import EgoError

SETTINGS_SECTIONS = ("usage", "lifecycle", "loopguard", "toolprune", "mcp", "analysis")


@lru_cache(maxsize=1)
def package_defaults() -> dict[str, Any]:
    try:
        return tomllib.loads(resources.files("ego").joinpath("defaults.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EgoError("INVALID_DEFAULTS", f"cannot read packaged defaults.toml: {exc}", 2) from exc


def _merge(base: dict[str, Any], override: dict[str, Any], where: str) -> None:
    for key, value in override.items():
        if key not in base:
            raise EgoError("INVALID_CONFIG", f"Unknown tuning key: {where}{key}", 2)
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise EgoError("INVALID_CONFIG", f"tuning.{where}{key} must be a table", 2)
            _merge(base[key], value, f"{where}{key}.")
        elif not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise EgoError("INVALID_CONFIG", f"tuning.{where}{key} must be an integer >= 1", 2)
        else:
            base[key] = value


def tuning_values(root: Path | None) -> dict[str, Any]:
    tuning = copy.deepcopy(package_defaults()["tuning"])
    path = root / ".ego" / "defaults.toml" if root is not None else None
    if path is not None and path.exists():
        try:
            override = tomllib.loads(path.read_text(encoding="utf-8")).get("tuning", {})
        except (OSError, ValueError) as exc:
            raise EgoError("INVALID_CONFIG", str(exc), 2) from exc
        _merge(tuning, override, "")
    return tuning


def _checked_setting(section: str, key: str, value: Any, default: Any) -> Any:
    where = f"{section}.{key}"
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise EgoError("INVALID_CONFIG", f"{where} must be a boolean", 2)
        return value
    if isinstance(default, str):
        if not isinstance(value, str):
            raise EgoError("INVALID_CONFIG", f"{where} must be a string", 2)
        return value
    if isinstance(default, float):
        numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        numeric = isinstance(value, int) and not isinstance(value, bool)
    if not numeric:
        raise EgoError("INVALID_CONFIG", f"{where} must be a number", 2)
    if isinstance(value, float) and not math.isfinite(value):
        raise EgoError("INVALID_CONFIG", f"{where} must be finite", 2)
    if value < 0:
        raise EgoError("INVALID_CONFIG", f"{where} must be >= 0", 2)
    return float(value) if isinstance(default, float) else value


def section_values(root: Path | None, name: str) -> dict[str, Any]:
    if name not in SETTINGS_SECTIONS:
        raise EgoError("INVALID_CONFIG", f"Unknown settings section: {name}", 2)
    defaults = package_defaults()[name]
    values = copy.deepcopy(defaults)
    path = root / ".ego" / "defaults.toml" if root is not None else None
    if path is None or not path.exists():
        return values
    try:
        override = tomllib.loads(path.read_text(encoding="utf-8")).get(name, {})
    except (OSError, ValueError) as exc:
        raise EgoError("INVALID_CONFIG", str(exc), 2) from exc
    if not isinstance(override, dict):
        raise EgoError("INVALID_CONFIG", f"{name} must be a table", 2)
    for key, value in override.items():
        if key not in defaults:
            raise EgoError("INVALID_CONFIG", f"Unknown {name} key: {key}", 2)
        values[key] = _checked_setting(name, key, value, defaults[key])
    return values
