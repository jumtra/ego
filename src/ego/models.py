"""Model catalog discovery that never runs in Fast mode.

Models are not software packages; refresh updates advisory model IDs and
aliases, never installs a model or silently rewrites a pinned task model.
"""
from __future__ import annotations
import re
import shutil
import subprocess
import time
from pathlib import Path
from .util import read_json, write_json, EgoError
from .config import load_config

CACHE_TTL_SECONDS = 24 * 3600
CATALOG_PATH = Path(".ego/state/model_catalog.json")
OPEN_CODE_ID = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./:@+\-]+$")
AGY_MODEL_LINE = re.compile(r"^([a-z0-9][a-z0-9._-]+)\t+\S.*$")
HOSTS = ("claude", "codex", "opencode", "agy")


def parse_opencode_models(output: str) -> list[str]:
    # Deliberately accept only explicit provider/model identifiers.
    return sorted({line.strip() for line in output.splitlines() if OPEN_CODE_ID.fullmatch(line.strip())})


def parse_agy_models(output: str) -> list[str]:
    return sorted({m.group(1) for line in output.splitlines() if (m := AGY_MODEL_LINE.fullmatch(line.strip()))})


def refresh_catalog(root: Path, *, hosts: list[str] | None = None, timeout: int = 12) -> dict:
    """Explicit/TTL refresh. Never invoked by ordinary ego work execution."""
    requested = hosts or list(HOSTS)
    if any(h not in HOSTS for h in requested):raise EgoError("UNKNOWN_HOST", "Invalid refresh host",2)
    cache = read_json(root/CATALOG_PATH, {}) or {}
    now=time.time()
    cache.setdefault("schema_version",1)
    cache.setdefault("hosts", {})
    cache["hosts"]["claude"] = {
        "source":"vendor-stable-aliases", "models":["auto", "sonnet", "opus", "haiku"],
        "examples":["auto", "sonnet", "opus", "haiku", "sonnet-3.5", "sonnet-3.7", "sonnet-5.0", "sonnet-5.5"],
        "availability":"not-verified; use /model to check account", "fetched_at":now,
    }
    cache["hosts"]["codex"] = {
        "source":"native-cli-default", "models":["auto"],
        "examples":["auto", "gpt-5-codex"],
        "availability":"model IDs and account access managed by Codex; use /model in TUI", "fetched_at":now,
    }
    cache["hosts"]["agy"] = {
        "source":"manual-only", "models":["auto"],
        "examples":["auto", "gemini-2.5-pro", "gemini-2.5-flash"],
        "availability":"run agy models to list account models", "fetched_at":now,
    }
    if "agy" in requested:
        exe=shutil.which("agy")
        if exe:
            try:
                completed=subprocess.run([exe,"models"],text=True,capture_output=True,timeout=timeout,check=False)
                output=completed.stdout[:24000]
                # Antigravity's models command may be rendered as an interactive
                # table. Keep it as advisory text; never guess or pin model IDs.
                cache["hosts"]["agy"]={"source":"agy models","models":parse_agy_models(output),"raw_listing":output,
                    "availability":"listed by agy models; account access not verified", "fetched_at":now,
                    "verified": completed.returncode==0}
            except (OSError,subprocess.TimeoutExpired):
                cache["hosts"]["agy"]={"source":"agy models","models":[],"availability":"unavailable", "fetched_at":now}
    if "opencode" in requested:
        exe=shutil.which("opencode")
        if exe:
            try:
                result=subprocess.run([exe,"models","--refresh"],text=True,capture_output=True,timeout=timeout,check=False)
                models=parse_opencode_models(result.stdout)
                if result.returncode == 0 and models:
                    cache["hosts"]["opencode"]={"source":"opencode models --refresh", "models":models,
                        "availability":"catalog only; account/provider access must be validated by host", "fetched_at":now}
                else:
                    cache["hosts"].setdefault("opencode", {"source":"unavailable", "models":[]})
                    cache["hosts"]["opencode"]["error"]="CLI returned no usable models; retained old catalog"
            except (OSError,subprocess.TimeoutExpired):
                cache["hosts"].setdefault("opencode", {"source":"unavailable", "models":[]})
                cache["hosts"]["opencode"]["error"]="CLI unavailable or timed out; retained old catalog"
        else:
            cache["hosts"].setdefault("opencode", {"source":"unavailable", "models":[]})
            cache["hosts"]["opencode"]["error"]="opencode CLI not installed"
    write_json(root/CATALOG_PATH,cache)
    return cache


def list_models(root: Path, *, host: str | None = None, refresh_if_stale: bool = True) -> dict:
    if host is not None and host not in HOSTS:raise EgoError("UNKNOWN_HOST",host,2)
    cache = read_json(root/CATALOG_PATH, {}) or {}
    rows=cache.get("hosts",{})
    required=[host] if host else list(HOSTS)
    stale=any(not isinstance(rows.get(h),dict) or
              float(rows[h].get("fetched_at",0)) + CACHE_TTL_SECONDS <= time.time() for h in required)
    if refresh_if_stale and stale:
        cache=refresh_catalog(root, hosts=required)
        rows=cache.get("hosts",{})
    cfg = load_config(root)
    return {"schema_version":1,"ttl_seconds":CACHE_TTL_SECONDS,
            "configured_models":cfg.models,
            "model_policy":"auto delegates to each host's current default; explicit model IDs (e.g. sonnet, sonnet-5.5) remain pinned",
            "hosts":{h:rows.get(h,{"source":"unavailable","models":[],"availability":"refresh required"}) for h in required}}
