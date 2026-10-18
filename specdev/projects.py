"""Projects and the library they are kept in (see PROJECTS.md).

A project is a folder: its programs, its conversation and a little metadata.
A Library is one person's projects, with a copy of each in S3 when a bucket is
configured; S3 is then the record and the local folder the working copy.
"""

from __future__ import annotations

import json
import secrets
import shutil
import sys
import threading
import time
from pathlib import Path

SOURCE_TYPES = (".bas", ".asm", ".inc")
UNNAMED = "New project"
MAX_VERSIONS = 50                  # versions kept per project; the oldest go first


class Project:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.programs = folder / "programs"
        self.programs.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        try:
            self.meta = json.loads((folder / "project.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            now = time.time()
            self.meta = {"name": UNNAMED, "created": now, "updated": now, "main": None, "session": None, "cost": 0}
            self.save()

    @property
    def id(self) -> str:
        return self.folder.name

    def save(self) -> None:
        with self._lock:
            (self.folder / "project.json").write_text(json.dumps(self.meta), encoding="utf-8")

    def events(self) -> list[dict]:
        """The conversation so far, as the page events it was shown as."""
        try:
            lines = (self.folder / "chat.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        events = []
        for line in lines:
            try:
                events.append({**json.loads(line), "n": len(events)})
            except ValueError:
                pass                        # a line cut short by a crash
        return events

    def add_event(self, event: dict) -> None:
        with self._lock, open(self.folder / "chat.jsonl", "a", encoding="utf-8") as chat:
            chat.write(json.dumps(event) + "\n")

    # -- history ------------------------------------------------------------
    # A version is a copy of the project's source files, kept in history/<n>/ with a line in
    # history.jsonl saying when and why. One is kept after each request to the agent and each
    # hand-made save, and before a restore, so a restore can itself be undone.

    def _sources(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in sorted(self.programs.glob("*"))
                if p.is_file() and p.suffix.lower() in SOURCE_TYPES}

    def versions(self) -> list[dict]:
        """The versions kept, oldest first: {"n", "at", "why", "files"}."""
        try:
            lines = (self.folder / "history.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                version = json.loads(line)
            except ValueError:
                continue
            if (self.folder / "history" / str(version["n"])).is_dir():
                out.append(version)
        return out

    def keep_version(self, why: str) -> dict | None:
        """Copy the source files as a new version, unless they are the same as the last one kept."""
        with self._lock:
            files = self._sources()
            versions = self.versions()
            if not files or (versions and self._version_files(versions[-1]["n"]) == files):
                return None
            n = versions[-1]["n"] + 1 if versions else 1
            folder = self.folder / "history" / str(n)
            folder.mkdir(parents=True, exist_ok=True)
            for name, data in files.items():
                (folder / name).write_bytes(data)
            version = {"n": n, "at": time.time(), "why": why, "files": sorted(files)}
            kept = [*versions, version][-MAX_VERSIONS:]
            for gone in versions[: len(versions) + 1 - len(kept)]:
                shutil.rmtree(self.folder / "history" / str(gone["n"]), ignore_errors=True)
            (self.folder / "history.jsonl").write_text("".join(json.dumps(v) + "\n" for v in kept), encoding="utf-8")
            return version

    def _version_files(self, n: int) -> dict[str, bytes]:
        folder = self.folder / "history" / str(int(n))
        return {p.name: p.read_bytes() for p in sorted(folder.glob("*")) if p.is_file()} if folder.is_dir() else {}

    def version_file(self, n: int, name: str) -> bytes | None:
        return self._version_files(n).get(name)

    def restore(self, n: int) -> list[str]:
        """Put version ``n``'s source files back (after keeping the current ones); return the names changed."""
        wanted = self._version_files(n)
        if not wanted:
            raise KeyError(n)
        self.keep_version(f"before going back to version {n}")
        current = self._sources()
        with self._lock:
            for name in current.keys() - wanted.keys():
                (self.programs / name).unlink(missing_ok=True)
            for name, data in wanted.items():
                if current.get(name) != data:
                    (self.programs / name).write_bytes(data)
        return sorted(name for name in current.keys() | wanted.keys() if current.get(name) != wanted.get(name))

    def summary(self) -> dict:
        files = sorted(p.name for p in self.programs.glob("*") if p.suffix.lower() in SOURCE_TYPES)
        return {"id": self.id, "name": self.meta["name"], "updated": self.meta["updated"], "files": files,
                "cost": round(self.meta.get("cost", 0), 4), "thumb": (self.folder / "thumb.png").is_file()}


class Library:
    def __init__(self, home: Path, bucket: str = "", prefix: str = "") -> None:
        self.home, self.bucket, self.prefix = home, bucket, prefix.strip("/")
        self.root = home / "projects"
        self.root.mkdir(parents=True, exist_ok=True)
        self._s3 = None
        self._publishing = threading.Lock()
        self._sent: dict[str, tuple[float, int]] = {}     # key -> (mtime, size) of what is in S3
        if not any(self.root.iterdir()):
            self._fetch()
        self.projects = {p.name: Project(p) for p in sorted(self.root.iterdir()) if p.is_dir()}
        self._adopt(home / "programs")
        try:
            wanted = (home / "current").read_text().strip()
        except OSError:
            wanted = ""
        newest = max(self.projects.values(), key=lambda p: p.meta["updated"], default=None)
        self.current = self.projects.get(wanted) or newest or self.new()

    # -- projects -----------------------------------------------------------

    def new(self) -> Project:
        project = Project(self.root / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}")
        self.projects[project.id] = project
        return self.select(project.id)

    def select(self, project_id: str) -> Project:
        self.current = self.projects[project_id]
        (self.home / "current").write_text(self.current.id)
        return self.current

    def summaries(self) -> list[dict]:
        return sorted((p.summary() for p in self.projects.values()), key=lambda s: s["updated"], reverse=True)

    def _adopt(self, old: Path) -> None:
        """Programs from before there were projects become a project of their own."""
        if old.is_dir() and any(old.iterdir()):
            project = Project(self.root / f"{time.strftime('%Y%m%d-%H%M%S')}-0000")
            shutil.rmtree(project.programs)
            old.rename(project.programs)
            sources = [p for p in project.programs.glob("*.bas")]
            newest = max(sources, key=lambda p: p.stat().st_mtime, default=None)
            project.meta.update(name="Earlier programs", main=newest.name if newest else None)
            project.save()
            self.projects[project.id] = project
            self.publish(project)

    # -- the copy in S3 -------------------------------------------------------

    def _client(self):
        if self._s3 is None:
            import boto3

            self._s3 = boto3.client("s3")
        return self._s3

    def publish(self, project: Project) -> None:
        """Copy a project to the library in S3, in the background; a failure is logged, not raised."""
        if self.bucket:
            threading.Thread(target=self._publish, args=(project,), daemon=True, name="specdev-library").start()

    def _publish(self, project: Project) -> None:
        with self._publishing:
            try:
                s3 = self._client()
                for path in project.folder.rglob("*"):
                    if path.is_file():
                        key = f"{self.prefix}/{project.id}/{path.relative_to(project.folder).as_posix()}"
                        stat = path.stat()
                        if self._sent.get(key) != (stat.st_mtime, stat.st_size):
                            s3.upload_file(str(path), self.bucket, key)
                            self._sent[key] = (stat.st_mtime, stat.st_size)
            except Exception as exc:
                print(f"library: could not publish {project.id}: {exc}", file=sys.stderr, flush=True)

    def _fetch(self) -> None:
        """Bring this person's library back from S3 (a new or rebuilt instance)."""
        if not self.bucket:
            return
        try:
            s3 = self._client()
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self.prefix + "/"):
                for item in page.get("Contents", []):
                    target = (self.root / item["Key"][len(self.prefix) + 1:]).resolve()
                    if self.root.resolve() in target.parents and not item["Key"].endswith("/"):
                        target.parent.mkdir(parents=True, exist_ok=True)
                        s3.download_file(self.bucket, item["Key"], str(target))
        except Exception as exc:
            print(f"library: could not fetch {self.prefix}: {exc}", file=sys.stderr, flush=True)
