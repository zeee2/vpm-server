from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.packager import build_package_zip, discover_package_roots, watch_should_ignore

logger = logging.getLogger(__name__)


def find_package_root_for_path(projects_dir: Path, path: Path) -> Path | None:
    """Resolve which direct child of projects_dir owns this path (must contain package.json)."""
    projects_dir = projects_dir.resolve()
    path = Path(path)
    try:
        rel = path.resolve().relative_to(projects_dir)
    except ValueError:
        return None
    if not rel.parts:
        return None
    root = projects_dir / rel.parts[0]
    if (root / "package.json").is_file():
        return root
    return None


class _DebouncedRebuild(FileSystemEventHandler):
    def __init__(
        self,
        projects_dir: Path,
        zips_dir: Path,
        debounce_sec: float,
        auto_bump_patch: bool,
        ignore_meta: bool,
        on_rebuild_done: Callable[[], None],
    ) -> None:
        super().__init__()
        self.projects_dir = projects_dir
        self.zips_dir = zips_dir
        self.debounce_sec = debounce_sec
        self.auto_bump_patch = auto_bump_patch
        self.ignore_meta = ignore_meta
        self.on_rebuild_done = on_rebuild_done
        self._pending: set[Path] = set()
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def _schedule(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_sec, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            roots = list(self._pending)
            self._pending.clear()
            self._timer = None
        for root in roots:
            try:
                build_package_zip(root, self.zips_dir, bump_patch=self.auto_bump_patch)
            except Exception:
                logger.exception("Rebuild failed for %s", root)
        try:
            self.on_rebuild_done()
        except Exception:
            logger.exception("on_rebuild_done failed")

    def _consider(self, path_str: str) -> None:
        if watch_should_ignore(path_str, self.ignore_meta):
            return
        root = find_package_root_for_path(self.projects_dir, Path(path_str))
        if root is None:
            return
        with self._lock:
            self._pending.add(root)
        self._schedule()

    def _handle_file_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._consider(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle_file_event(event)

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle_file_event(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._handle_file_event(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._handle_file_event(event)
        if getattr(event, "dest_path", None):
            self._consider(event.dest_path)


def start_package_observer(
    projects_dir: Path,
    zips_dir: Path,
    *,
    debounce_sec: float,
    auto_bump_patch: bool,
    ignore_meta: bool,
    on_rebuild_done: Callable[[], None],
) -> Observer | None:
    if not projects_dir.is_dir():
        logger.warning("Projects dir missing; watcher not started: %s", projects_dir)
        return None
    if not discover_package_roots(projects_dir):
        logger.warning("No package.json under %s; watcher idle", projects_dir)

    handler = _DebouncedRebuild(
        projects_dir,
        zips_dir,
        debounce_sec,
        auto_bump_patch,
        ignore_meta,
        on_rebuild_done,
    )
    observer = Observer()
    observer.schedule(handler, str(projects_dir), recursive=True)
    observer.start()
    logger.info("Watching %s (debounce=%ss, auto_bump=%s)", projects_dir, debounce_sec, auto_bump_patch)
    return observer
