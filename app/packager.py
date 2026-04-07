from __future__ import annotations

import json
import logging
import os
import re
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Skip these path segments when packing (English comments only).
_SKIP_DIR_PARTS = frozenset({".git", "__pycache__", "node_modules"})


def bump_patch_version(version: str) -> str:
    """Increment patch for simple SemVer x.y.z (optional -label/+metadata kept separate)."""
    raw = version.strip()
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(.*)$", raw)
    if not m:
        return raw
    major, minor, patch, rest = m.groups()
    if rest and not rest.startswith("-") and not rest.startswith("+"):
        return raw
    try:
        p = int(patch) + 1
    except ValueError:
        return raw
    return f"{major}.{minor}.{p}{rest}"


def read_package_json(package_root: Path) -> dict:
    path = package_root / "package.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_package_json(package_root: Path, data: dict) -> None:
    path = package_root / "package.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _should_skip_file(abs_path: Path, package_root: Path) -> bool:
    try:
        rel = abs_path.relative_to(package_root)
    except ValueError:
        return True
    for part in rel.parts:
        if part in _SKIP_DIR_PARTS or part.startswith("."):
            return True
    return False


def zip_package_folder(package_root: Path, dest_zip: Path) -> None:
    """Create a zip whose root matches the UPM package (package.json at archive root)."""
    package_root = package_root.resolve()
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in package_root.rglob("*"):
            if path.is_dir():
                continue
            if _should_skip_file(path, package_root):
                continue
            arcname = path.relative_to(package_root).as_posix()
            zf.write(path, arcname)


def build_package_zip(
    package_root: Path,
    zips_dir: Path,
    *,
    bump_patch: bool,
) -> Path:
    """
    Read package.json, optionally bump patch version, write zip to zips_dir/{name}-{version}.zip.
    Returns path to the created zip file.
    """
    package_root = package_root.resolve()
    if not (package_root / "package.json").is_file():
        raise FileNotFoundError(f"No package.json under {package_root}")

    manifest = read_package_json(package_root)
    if bump_patch:
        old_ver = str(manifest.get("version", "")).strip()
        new_ver = bump_patch_version(old_ver)
        if new_ver != old_ver:
            manifest["version"] = new_ver
            write_package_json(package_root, manifest)
            logger.info("Bumped %s version %s -> %s", package_root.name, old_ver, new_ver)

    name = str(manifest.get("name", "")).strip()
    version = str(manifest.get("version", "")).strip()
    if not name or not version:
        raise ValueError(f"package.json missing name or version in {package_root}")

    filename = f"{name}-{version}.zip"
    dest = (zips_dir / filename).resolve()
    zip_package_folder(package_root, dest)
    logger.info("Wrote %s", dest)
    return dest


def discover_package_roots(projects_dir: Path) -> list[Path]:
    if not projects_dir.is_dir():
        return []
    roots: list[Path] = []
    for child in sorted(projects_dir.iterdir()):
        if child.is_dir() and (child / "package.json").is_file():
            roots.append(child)
    return roots


def build_all_packages(
    projects_dir: Path,
    zips_dir: Path,
    *,
    bump_patch: bool,
) -> list[Path]:
    out: list[Path] = []
    for root in discover_package_roots(projects_dir):
        try:
            out.append(build_package_zip(root, zips_dir, bump_patch=bump_patch))
        except OSError as e:
            logger.warning("Skip %s: %s", root, e)
    return out


def watch_should_ignore(
    path: str,
    ignore_meta: bool,
    *,
    ignore_package_json: bool = False,
) -> bool:
    base = os.path.basename(path)
    if base.startswith("."):
        return True
    if ignore_meta and path.endswith(".meta"):
        return True
    # When AUTO_BUMP_PATCH writes package.json, ignore that event or we get a rebuild loop.
    if ignore_package_json and base == "package.json":
        return True
    return False
