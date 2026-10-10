"""Detects that the user is playing: a known game process or a fullscreen Direct3D app."""
from __future__ import annotations

import os

import psutil

from . import winapi


class GameDetector:
    SCAN_EVERY_S = 3.0

    def __init__(self):
        self.active = False
        self.source: str | None = None      # what to show: the game's .exe name
        self.key: str | None = None         # lower-case .exe name, used for remembered game memory
        self.pids: set[int] = set()         # processes of the detected game
        self._last_scan = 0.0
        self._last_seen = -1e18
        self._exe_cache: dict[tuple[int, float], str] = {}

    def poll(self, now: float, cfg: dict, managed_pids: set[int]) -> tuple[bool, str | None]:
        if now - self._last_scan < self.SCAN_EVERY_S:
            return self.active, self.source
        self._last_scan = now
        found = self._scan(cfg, managed_pids)
        if found:
            self._last_seen = now
            self.active, self.source = True, found[0]
            self.key, self.pids = found[0].lower(), found[1]
        elif self.active and now - self._last_seen >= cfg["release_after_s"]:
            self.active, self.source, self.key, self.pids = False, None, None, set()
        return self.active, self.source

    def _scan(self, cfg: dict, managed: set[int]) -> tuple[str, set[int]] | None:
        patterns = [p.lower() for p in cfg["path_patterns"]]
        ignored_paths = [p.lower() for p in cfg["ignore_path_patterns"]]
        names = {n.lower() for n in cfg["process_names"]}
        ignored = {n.lower() for n in cfg["ignore_names"]}
        seen = set()
        hits: dict[str, set[int]] = {}
        for proc in psutil.process_iter(["name", "create_time"]):
            info = proc.info
            key = (proc.pid, info.get("create_time") or 0.0)
            seen.add(key)
            if proc.pid in managed:
                continue
            name = info.get("name") or ""
            if not name or name.lower() in ignored:
                continue
            if name.lower() in names:
                hits.setdefault(name, set()).add(proc.pid)
                continue
            exe = self._exe_cache.get(key)
            if exe is None:
                try:
                    exe = (proc.exe() or "").lower()
                except psutil.Error:
                    exe = ""
                self._exe_cache[key] = exe
            if exe and any(p in exe for p in patterns) and not any(p in exe for p in ignored_paths):
                hits.setdefault(os.path.basename(exe), set()).add(proc.pid)
        self._exe_cache = {k: v for k, v in self._exe_cache.items() if k in seen}
        if hits:
            name = max(hits, key=lambda n: len(hits[n]))
            return name, set().union(*hits.values())
        if cfg["detect_fullscreen"] and winapi.user_notification_state() == winapi.QUNS_RUNNING_D3D_FULL_SCREEN:
            pid = winapi.foreground_pid()
            if pid and pid not in managed:
                try:
                    return psutil.Process(pid).name(), {pid}
                except psutil.Error:
                    pass
            return "полноэкранное приложение", set()
        return None
