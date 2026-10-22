"""The cache of project files on a PC that runs other PCs' tasks: a series of tasks with the same data sends the data
once.

Before packing a project the sending PC asks which of its big files (MIN_BYTES and more) the host already has, by
their SHA-256, and packs only the rest. The host copies the files it has from the cache into the task's folder and
keeps the big files that came in the archive for the next tasks, each checked against its hash first. Every sending
PC has a shelf of its own, so one member never learns what another one sent. Files are copied, never linked: a task
that changes its own copy cannot spoil the cache. A file nobody asked for in KEEP_S is deleted, and the oldest go first
when the cache outgrows its share of the disk space given to the network.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from .netbase import NetError, file_sha256, safe_name

MIN_BYTES = 256 << 10      # smaller files go in the archive every time: they pack well and cost little to send
MAX_FILES = 2000           # big files of one project that take part (the rest simply go in the archive)
KEEP_S = 7 * 86400         # a cached file nobody used for a week is deleted
HEX64 = re.compile(r"[0-9a-f]{64}")
GONE = "часть файлов проекта пропала из кэша ПК-хозяина: отправьте проект целиком"


class FileCache:
    def __init__(self, root: Path):
        self.root = root

    def shelf(self, node: str, kind: str) -> Path:
        return self.root / re.sub(r"[^\w-]", "_", f"{kind}-{node}")

    def path(self, node: str, kind: str, sha: str) -> Path:
        return self.shelf(node, kind) / sha[:2] / sha

    def have(self, node: str, kind: str, hashes) -> list[str]:
        """Which of these files the cache holds; each found one counts as used now (kept for another KEEP_S)."""
        if not isinstance(hashes, list) or len(hashes) > MAX_FILES:
            raise NetError("неверный список файлов")
        now, out = time.time(), []
        for h in hashes:
            if isinstance(h, str) and HEX64.fullmatch(h):
                p = self.path(node, kind, h)
                try:
                    os.utime(p, (now, now))
                except OSError:
                    continue
                out.append(h)
        return out

    @staticmethod
    def check_lists(cached, stored) -> None:
        """The two lists a task's archive comes with: {relative path: SHA-256}."""
        if not isinstance(cached, dict) or not isinstance(stored, dict) or len(cached) + len(stored) > MAX_FILES:
            raise NetError("неверный список файлов проекта")
        for rel, sha in list(cached.items()) + list(stored.items()):
            if safe_name(str(rel)) is None or not (isinstance(sha, str) and HEX64.fullmatch(sha)):
                raise NetError("неверный список файлов проекта")

    def missing(self, node: str, kind: str, cached: dict) -> list[str]:
        return [rel for rel, sha in cached.items() if not self.path(node, kind, sha).is_file()]

    def fill(self, node: str, kind: str, cached: dict, work: Path, manifest: dict) -> None:
        """Copy the files the sender left out of the archive (they are cached here) into the task's folder, and note
        them in the folder's manifest, so they do not count as the task's results."""
        work = work.resolve()
        now = time.time()
        for rel, sha in cached.items():
            rel = safe_name(rel)
            if rel is None or rel in manifest:
                continue
            dst = (work / rel).resolve()
            if work not in dst.parents:
                raise NetError(f"путь за пределы папки: {rel}")
            src = self.path(node, kind, sha)
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copyfile(src, dst)
                os.utime(src, (now, now))
            except FileNotFoundError:
                raise NetError(GONE, 410) from None
            st = dst.stat()
            manifest[rel] = [st.st_size, st.st_mtime_ns]

    def keep(self, node: str, kind: str, stored: dict, work: Path, manifest: dict) -> int:
        """Cache the big files that came in the archive, each only if it really has the hash it was sent with.
        Returns how many were added."""
        added = 0
        for rel, sha in stored.items():
            rel = safe_name(rel)
            if rel is None or rel not in manifest:
                continue
            target = self.path(node, kind, sha)
            if target.is_file():
                os.utime(target)
                continue
            src = work / rel
            try:
                if file_sha256(src) != sha:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".part")
                shutil.copyfile(src, tmp)
                os.replace(tmp, target)
                added += 1
            except OSError:
                continue
        return added

    def size(self) -> int:
        total = 0
        for dirpath, _dirs, files in os.walk(self.root):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        return total

    def gc(self, now: float, budget: int, keep_s: float = KEEP_S) -> int:
        """Delete what nobody used for keep_s, then the oldest until the cache fits its budget. Returns the count."""
        entries = []
        for dirpath, _dirs, files in os.walk(self.root):
            for f in files:
                p = os.path.join(dirpath, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, p))
        entries.sort()
        total, gone = sum(e[1] for e in entries), 0
        for mtime, size, p in entries:
            if mtime >= now - keep_s and total <= budget:
                break
            try:
                os.remove(p)
            except OSError:
                continue
            total -= size
            gone += 1
        for dirpath, _dirs, _files in os.walk(self.root, topdown=False):  # folders left empty
            if Path(dirpath) != self.root:
                try:
                    os.rmdir(dirpath)  # only an empty one goes
                except OSError:
                    pass
        return gone


def hash_of(path: Path, memo: dict) -> str:
    """A file's SHA-256, remembered by (path, size, mtime): the next task of a series does not read its data again."""
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    sha = memo.get(key)
    if sha is None:
        if len(memo) > 50000:
            memo.clear()
        sha = memo[key] = file_sha256(path)
    return sha
