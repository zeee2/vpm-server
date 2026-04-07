from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import zipfile
from collections.abc import Iterable
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response

from app.packager import build_all_packages
from app.watcher import start_package_observer

_DEFAULT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(_DEFAULT_ROOT / "data"))).resolve()
# Default sibling folder "projects/"; Docker Compose sets PROJECTS_DIR=/data/projects explicitly.
PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", str(_DEFAULT_ROOT / "projects"))).resolve()
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "http://127.0.0.1:8080").rstrip("/")
REPO_CONFIG_PATH = DATA_DIR / "repo.yaml"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _is_under_dir(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _load_repo_config() -> dict[str, Any]:
    if not REPO_CONFIG_PATH.is_file():
        raise FileNotFoundError(
            f"Missing {REPO_CONFIG_PATH}. Mount a repo.yaml (see data/repo.yaml example)."
        )
    with open(REPO_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _read_package_json_from_zip(zip_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        key = "package.json"
        if key not in names:
            prefixed = [n for n in names if n.endswith("/package.json") and n.count("/") == 1]
            if not prefixed:
                raise ValueError(f"No package.json in {zip_path}")
            key = sorted(prefixed)[0]
        raw = zf.read(key)
    return json.loads(raw.decode("utf-8"))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _use_explicit_package_list(cfg: dict[str, Any]) -> bool:
    p = cfg.get("packages")
    if p is None:
        return False
    if isinstance(p, str) and p.strip().lower() == "auto":
        return False
    if isinstance(p, list) and len(p) > 0:
        return True
    return False


def _iter_zip_entries(cfg: dict[str, Any]) -> Iterable[tuple[str, Path, dict[str, Any] | None]]:
    zips_dir = (DATA_DIR / "zips").resolve()
    zips_dir.mkdir(parents=True, exist_ok=True)
    if _use_explicit_package_list(cfg):
        for entry in cfg.get("packages") or []:
            filename = entry.get("file") or entry.get("zip")
            if not filename:
                continue
            zip_path = (zips_dir / filename).resolve()
            if not _is_under_dir(zip_path, zips_dir):
                raise ValueError(f"Invalid zip path (must stay under {zips_dir}): {filename}")
            yield filename, zip_path, entry
        return
    for z in sorted(zips_dir.glob("*.zip")):
        yield z.name, z.resolve(), None


def _build_listing() -> dict[str, Any]:
    cfg = _load_repo_config()
    repo = cfg.get("repo") or {}
    name = repo.get("name", "Unnamed VPM Repo")
    rid = repo.get("id", "com.example.vpm")
    author = repo.get("author", "unknown@example.com")
    public_base = str(repo.get("public_base") or PUBLIC_BASE).rstrip("/")

    packages_out: dict[str, Any] = {}

    for filename, zip_path, entry in _iter_zip_entries(cfg):
        if not zip_path.is_file():
            raise FileNotFoundError(f"Package zip not found: {zip_path}")

        manifest = _read_package_json_from_zip(zip_path)
        if entry and isinstance(entry.get("manifest"), dict):
            manifest = {**manifest, **entry["manifest"]}

        ver = str(manifest.get("version", "")).strip()
        if not ver:
            raise ValueError(f"package.json missing version in {zip_path}")

        pkg_name = manifest.get("name")
        if not pkg_name:
            raise ValueError(f"package.json missing name in {zip_path}")

        manifest["url"] = f"{public_base}/packages/{filename}"
        manifest["zipSHA256"] = _sha256_file(zip_path)

        if pkg_name not in packages_out:
            packages_out[pkg_name] = {"versions": {}}
        packages_out[pkg_name]["versions"][ver] = manifest

    listing: dict[str, Any] = {
        "name": name,
        "author": author,
        "id": rid,
        "url": f"{public_base}/index.json",
        "packages": packages_out,
    }
    return listing


@lru_cache(maxsize=1)
def _cached_listing_body() -> bytes:
    return json.dumps(_build_listing(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def invalidate_listing_cache() -> None:
    _cached_listing_body.cache_clear()


def _allowed_zip_filenames(cfg: dict[str, Any]) -> set[str] | None:
    """None = allow any *.zip present on disk under zips/."""
    if _use_explicit_package_list(cfg):
        return {
            (e.get("file") or e.get("zip"))
            for e in (cfg.get("packages") or [])
            if e.get("file") or e.get("zip")
        }
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "zips").mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: build_all_packages(PROJECTS_DIR, DATA_DIR / "zips", bump_patch=False),
    )
    invalidate_listing_cache()
    logger.info(
        "Startup zip build done (projects=%s, bump on watch=%s)",
        PROJECTS_DIR,
        os.environ.get("AUTO_BUMP_PATCH", "true"),
    )

    debounce = float(os.environ.get("WATCH_DEBOUNCE_SEC", "2"))
    auto_bump = os.environ.get("AUTO_BUMP_PATCH", "true").lower() in ("1", "true", "yes")
    ignore_meta = os.environ.get("WATCH_IGNORE_META", "true").lower() in ("1", "true", "yes")

    observer = start_package_observer(
        PROJECTS_DIR,
        DATA_DIR / "zips",
        debounce_sec=debounce,
        auto_bump_patch=auto_bump,
        ignore_meta=ignore_meta,
        on_rebuild_done=invalidate_listing_cache,
    )
    yield
    if observer is not None:
        observer.stop()
        observer.join(timeout=5)


app = FastAPI(title="VPM Repo Server", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_class=PlainTextResponse)
def health() -> str:
    return "ok"


@app.get("/index.json")
def index_json() -> Response:
    try:
        body = _cached_listing_body()
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    except (ValueError, json.JSONDecodeError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"Invalid repo data: {e}") from e
    return Response(content=body, media_type="application/json")


@app.get("/packages/{filename}")
def download_package(filename: str) -> FileResponse:
    cfg = _load_repo_config()
    allowed = _allowed_zip_filenames(cfg)
    if allowed is not None and filename not in allowed:
        raise HTTPException(status_code=404, detail="Package not in repo list")

    zips_dir = (DATA_DIR / "zips").resolve()
    path = (zips_dir / filename).resolve()
    if not _is_under_dir(path, zips_dir):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File missing")

    return FileResponse(path, filename=filename, media_type="application/zip")


@app.post("/__reload")
def reload_cache() -> dict[str, str]:
    """Clear listing cache (e.g. after manual zip edits)."""
    invalidate_listing_cache()
    return {"status": "ok"}
