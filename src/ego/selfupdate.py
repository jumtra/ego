"""Conservative self-update via publisher's GitHub Release wheel.

No arbitrary update URL, no shell interpolation, no dependency resolution, and
no changes to project .ego state. Only stable newer versions with an
API-provided SHA256 digest may be installed. Release publishing is external.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import zipfile
from .util import SEHError

REPO = "jumtra/ego-standalone"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
PACKAGE = "ego-standalone"
MAX_DOWNLOAD = 30*1024*1024
DIGEST_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
VERS_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
ALLOWED_DOWNLOAD_HOSTS={"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
SYSTEM_CA_BUNDLES=("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt")


def _ssl_context() -> ssl.SSLContext:
    ctx=ssl.create_default_context()
    for candidate in SYSTEM_CA_BUNDLES:
        if Path(candidate).is_file():
            try:ctx.load_verify_locations(cafile=candidate)
            except ssl.SSLError:continue
            break
    return ctx


def _version_tuple(v: str) -> tuple[int,int,int]:
    match=VERS_RE.fullmatch(v)
    if not match:raise SEHError("INVALID_RELEASE",f"Expected a stable semantic version: {v}",3)
    return tuple(map(int,match.groups()))


def _read_url(url: str, limit: int, timeout: int=12) -> bytes:
    parsed=urlparse(url)
    if parsed.scheme!="https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS|{"api.github.com"}:
        raise SEHError("UNTRUSTED_UPDATE_URL",f"Untrusted release domain: {parsed.hostname}",3)
    req=Request(url,headers={"User-Agent":"ego-updater","Accept":"application/vnd.github+json"})
    try:
        with urlopen(req,timeout=timeout,context=_ssl_context()) as response:
            if response.geturl()!=url:
                final=urlparse(response.geturl())
                if final.scheme!="https" or final.hostname not in ALLOWED_DOWNLOAD_HOSTS:
                    raise SEHError("UNTRUSTED_UPDATE_REDIRECT",final.hostname or "",3)
            data=response.read(limit+1)
    except (OSError,ValueError) as exc:
        if "HTTP Error 404" in str(exc) and url==API:
            raise SEHError("NO_RELEASE_PUBLISHED", "No published stable release found; publish a release wheel to enable 'harness update'",3) from exc
        raise SEHError("UPDATE_NETWORK",str(exc),3) from exc
    if len(data)>limit:raise SEHError("UPDATE_TOO_LARGE","Release exceeds configured size limit",3)
    return data


def latest_release(current_version: str) -> dict:
    try:release=json.loads(_read_url(API,1024*1024).decode("utf8"))
    except (ValueError,UnicodeError) as exc:raise SEHError("UPDATE_INVALID_RESPONSE",str(exc),3) from exc
    tag=str(release.get("tag_name", ""))
    newer=_version_tuple(tag)>_version_tuple(current_version)
    if release.get("draft") or release.get("prerelease"):
        raise SEHError("UPDATE_INVALID_RELEASE","Latest release is not stable",3)
    wheels=[a for a in release.get("assets", []) if isinstance(a,dict) and
            a.get("name", "").startswith("ego_standalone-") and
            a.get("name", "").endswith("-py3-none-any.whl")]
    wheels=[a for a in wheels if a["name"].split("-")[1]==tag.lstrip("v")]
    asset=wheels[0] if wheels else None
    digest=asset.get("digest", "") if asset else ""
    usable=bool(asset and isinstance(digest,str) and DIGEST_RE.fullmatch(digest))
    return {"current":current_version,"latest":tag.lstrip("v"),"update_available":newer,
            "can_install":newer and usable,"asset":asset if usable else None,
            "reason":"ready" if usable else "No version-matching release wheel with GitHub SHA256 digest",
            "repo":REPO,"release_url":release.get("html_url")}


def validate_wheel(data: bytes, version: str) -> None:
    import io
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            targets=[name for name in z.namelist() if name.endswith(".dist-info/METADATA")]
            if len(targets)!=1:raise SEHError("INVALID_WHEEL","Wheel needs one distribution METADATA",3)
            metadata_text=z.read(targets[0]).decode("utf8",errors="strict")
            lines=dict(line.split(": ",1) for line in metadata_text.splitlines() if line.startswith(("Name: ","Version: ")) and ": " in line)
            if lines.get("Name", "").lower().replace("_","-") != PACKAGE or lines.get("Version") != version:
                raise SEHError("INVALID_WHEEL","Distribution name/version mismatch",3)
            for name in z.namelist():
                p=Path(name)
                if p.is_absolute() or ".." in p.parts:
                    raise SEHError("INVALID_WHEEL","Unsafe wheel member",3)
    except (zipfile.BadZipFile,UnicodeError) as exc:
        raise SEHError("INVALID_WHEEL",str(exc),3) from exc


def fetch_verified_wheel(info: dict, target: Path) -> Path:
    if not info.get("can_install") or not info.get("asset"):
        raise SEHError("UPDATE_NOT_AVAILABLE",info.get("reason","No usable new release"),3)
    asset=info["asset"]
    url=str(asset.get("browser_download_url",""))
    if not url.startswith(f"https://github.com/{REPO}/releases/download/"):
        raise SEHError("UNTRUSTED_UPDATE_URL",url,3)
    data=_read_url(url,MAX_DOWNLOAD)
    actual=hashlib.sha256(data).hexdigest()
    expected=DIGEST_RE.fullmatch(asset["digest"]).group(1)
    if actual!=expected:raise SEHError("UPDATE_DIGEST_MISMATCH","Release digest mismatch",3)
    validate_wheel(data,info["latest"])
    target.parent.mkdir(parents=True,exist_ok=True)
    from .util import atomic_bytes
    atomic_bytes(target,data)
    return target


def install_wheel(path: Path, version: str, *, approved: bool=False) -> dict:
    if not approved:
        raise SEHError("APPROVAL_REQUIRED","Use --yes or confirm interactively to install an update",3)
    if sys.prefix==getattr(sys,"base_prefix",sys.prefix):
        raise SEHError("VENV_REQUIRED","Upgrade via a virtual environment or pipx; system Python is not modified",3)
    proc=subprocess.run([sys.executable,"-m","pip","install","--no-deps","--no-index","--force-reinstall",str(path)],
                        capture_output=True,text=True,timeout=180,check=False)
    if proc.returncode:
        raise SEHError("UPDATE_INSTALL_FAILED",(proc.stderr or proc.stdout)[-1600:],3)
    check=subprocess.run([sys.executable,"-m","ego","--version"],capture_output=True,text=True,timeout=15,check=False)
    good=check.returncode==0 and version in check.stdout
    if not good:
        raise SEHError("UPDATE_HEALTHCHECK_FAILED","New package installed but version check failed. Reinstall previous wheel from local backup; project data was not modified.",3)
    return {"status":"INSTALLED","version":version,"restart_required":True,
            "project_data_changed":False,"warning":"The previous wheel should be saved for rollback before updating"}


def self_update(current_version: str, *, apply:bool=False, yes:bool=False, dest:Path|None=None) -> dict:
    info=latest_release(current_version)
    if not apply or not info["update_available"]:return info
    if not info["can_install"]:raise SEHError("UPDATE_UNVERIFIED_RELEASE",info["reason"],3)
    if not yes:
        if not sys.stdin.isatty():raise SEHError("APPROVAL_REQUIRED","Noninteractive install requires --yes",3)
        reply=input(f"Install verified ego {info['latest']}? [y/N] ").strip().lower()
        if reply not in ("y","yes"):raise SEHError("UPDATE_ABORTED","Cancelled",2)
    cache=dest or Path.home()/".cache"/"seh"/"updates"/info["latest"]
    wheel=fetch_verified_wheel(info,cache/info["asset"]["name"])
    return {**info,**install_wheel(wheel,info["latest"],approved=True)}


def update_from_local_wheel(path: Path, digest: str, current_version: str, *, yes: bool=False) -> dict:
    """Install user-supplied offline wheel only with explicit SHA256 and approval."""
    if not yes:
        raise SEHError("APPROVAL_REQUIRED","Local wheel updates require --yes after reviewing the wheel",3)
    if not DIGEST_RE.fullmatch("sha256:"+digest):
        raise SEHError("INVALID_DIGEST","Expected 64 lowercase SHA256 hex characters",3)
    if not path.is_file() or path.stat().st_size>MAX_DOWNLOAD:
        raise SEHError("INVALID_WHEEL","Missing or oversized wheel",3)
    data=path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=digest:
        raise SEHError("UPDATE_DIGEST_MISMATCH","Offline wheel digest mismatch",3)
    name=path.name
    match=re.fullmatch(r"ego_standalone-(\d+\.\d+\.\d+)-py3-none-any\.whl",name)
    if not match:
        raise SEHError("INVALID_WHEEL","Unexpected wheel filename",3)
    version=match.group(1)
    if _version_tuple(version)<=_version_tuple(current_version):
        raise SEHError("NOT_NEWER","Local wheel must be a newer stable version",3)
    validate_wheel(data,version)
    return install_wheel(path,version,approved=True)
