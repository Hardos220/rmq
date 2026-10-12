"""Automatic updates from the author's GitHub releases.

A release is a GitHub release tagged vX.Y.Z with two files: rmq-X.Y.Z.zip and SHA256SUMS.txt. Only the owner of
the repository can publish one. A developer checkout (a folder with .git) never updates itself.

Every few hours, when the internet is there, the manager asks GitHub for the latest release. A newer one is
downloaded, checked against its SHA-256 and unpacked into data/update. Then a small helper process installs it:
the manager freezes its tasks and stops, the helper copies the new files over the old ones (never touching
data/ and .venv/), updates the Python packages if requirements.txt changed, and starts the manager again.
The frozen tasks go on where they stopped.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import __repo__, __version__

log = logging.getLogger("rmq")
ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com/repos/{repo}/releases/latest"
APPLY = Path(__file__).with_name("update_apply.py")


def parse_version(v: str) -> tuple[int, ...]:
    v = (v or "").strip().lstrip("vV")
    parts = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0] * (3 - len(parts)))


def is_newer(remote: str, local: str) -> bool:
    return parse_version(remote) > parse_version(local)


def is_dev_checkout() -> bool:
    return (ROOT / ".git").exists()


def _get(url: str, timeout: float = 20, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"rmq/{__version__}", "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_release(repo: str = __repo__) -> dict:
    """{"version", "zip_url", "sums_url", "notes", "url"} of the newest published release."""
    data = json.loads(_get(API.format(repo=repo)).decode("utf-8"))
    version = str(data.get("tag_name", "")).lstrip("vV")
    assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
    return {"version": version, "zip_url": assets.get(f"rmq-{version}.zip"), "sums_url": assets.get("SHA256SUMS.txt"),
            "notes": (data.get("body") or "")[:4000], "url": data.get("html_url")}


class Updater:
    def __init__(self, manager):
        self.m = manager
        self.dir = manager.home / "update"
        self.status: dict = {"state": "dev" if is_dev_checkout() else "idle", "version": __version__,
                             "latest": None, "checked_at": None, "error": "", "repo": __repo__}
        self._busy = threading.Lock()
        self._last_try = 0.0
        self._report_finished_update()
        if self.status["state"] != "dev":  # the owner of a developer copy makes his own shortcut
            threading.Thread(target=self._ensure_shortcut, name="rmq-shortcut", daemon=True).start()

    def _ensure_shortcut(self) -> None:
        try:
            from .install import ensure_shortcut
            done = ensure_shortcut(self.m.home)
            if done:
                log.info("%s", done)
        except (Exception, SystemExit) as e:
            log.warning("could not make the desktop shortcut: %s", e)

    def _report_finished_update(self) -> None:
        for stage in self.dir.glob("*"):  # unpacked releases left from an earlier update
            if stage.is_dir():
                shutil.rmtree(stage, ignore_errors=True)
        mark = self.dir / "last.json"
        try:
            info = json.loads(mark.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if info.get("reported"):
            return
        if info.get("ok"):
            self.m.db.event("update", f"Менеджер обновлён до версии {info.get('version')}")
            try:  # the agents' rules may have changed: refresh the blocks where the owner keeps them
                from .install import install_agent_rules
                install_agent_rules(only_present=True)
            except Exception as e:
                log.warning("could not refresh the agents' rules after the update: %s", e)
            if not self.m.db.kv_get("dashboard_seen"):
                # the dashboard never worked here (version 1.1.1 could fail to show it): show it once, now fixed
                self.m.open_dashboard_once = True
        else:
            self.m.db.event("update", f"Обновление до {info.get('version')} не удалось: {info.get('error')}")
        info["reported"] = True
        mark.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")

    def tick(self, now: float) -> None:
        """Called by the manager now and then: checks every few hours, in the background."""
        cfg = self.m.settings.section("update")
        if self.status["state"] == "dev":
            return
        if not cfg["auto"]:
            self.status["state"] = "disabled" if self.status["state"] in ("idle", "disabled") else self.status["state"]
            return
        if self.status["state"] == "disabled":
            self.status["state"] = "idle"
        period = max(1.0, float(cfg["check_every_h"])) * 3600
        retry = 1800 if self.status["state"] == "error" else period
        if now - self._last_try >= retry:
            self._last_try = now
            threading.Thread(target=self.check_and_install, name="rmq-update", daemon=True).start()

    def check(self) -> dict:
        dev = self.status["state"] == "dev"  # a developer copy learns the latest version but keeps its state
        self.status.update(error="")
        if not dev:
            self.status["state"] = "checking"
        try:
            rel = latest_release()
        except urllib.error.HTTPError as e:
            if e.code == 404:  # the repository has no published release yet
                self.status.update(latest=None, checked_at=time.time(), error="")
                if not dev:
                    self.status["state"] = "idle"
                return self.status
            self.status.update(error=f"GitHub ответил ошибкой {e.code}", checked_at=time.time())
            log.warning("update check: %s", self.status["error"])
            if not dev:
                self.status["state"] = "error"
            return self.status
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            self.status.update(error=f"нет связи с GitHub: {getattr(e, 'reason', e)}", checked_at=time.time())
            log.warning("update check: %s (next try in 30 minutes)", self.status["error"])
            if not dev:
                self.status["state"] = "error"
            return self.status
        self.status.update(latest=rel["version"], checked_at=time.time(), url=rel.get("url"))
        if dev:
            return self.status
        if is_newer(rel["version"], __version__) and rel["zip_url"] and rel["sums_url"]:
            self.status.update(state="available", release=rel)
        else:
            self.status["state"] = "idle"
        return self.status

    def check_and_install(self, force_install: bool = False) -> dict:
        if not self._busy.acquire(blocking=False):
            return self.status
        try:
            st = self.check()
            if self.status["state"] == "dev" and not force_install:
                return self.status
            if st["state"] == "available":
                self.install(st["release"])
            return self.status
        finally:
            self._busy.release()

    def install(self, rel: dict) -> None:
        version = rel["version"]
        self.status.update(state="downloading", error="")
        stage = self.dir / version
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            zpath = self.dir / f"rmq-{version}.zip"
            zpath.write_bytes(_get(rel["zip_url"], timeout=300, accept="application/octet-stream"))
            sums = _get(rel["sums_url"], timeout=60, accept="application/octet-stream").decode("utf-8", "replace")
            want = next((line.split()[0] for line in sums.splitlines()
                         if line.strip().endswith(f"rmq-{version}.zip")), None)
            got = hashlib.sha256(zpath.read_bytes()).hexdigest()
            if not want or want.lower() != got:
                raise RuntimeError("контрольная сумма архива не совпала: обновление отклонено")
            shutil.rmtree(stage, ignore_errors=True)
            with zipfile.ZipFile(zpath) as z:
                for name in z.namelist():
                    if name.startswith(("/", "\\")) or ".." in Path(name).parts or ":" in name:
                        raise RuntimeError("в архиве обновления недопустимые пути")
                z.extractall(stage)
            zpath.unlink(missing_ok=True)
            root = stage / f"rmq-{version}" if (stage / f"rmq-{version}").is_dir() else stage
            if not (root / "rmq" / "__init__.py").exists():
                raise RuntimeError("в архиве обновления нет программы")
        except Exception as e:
            self.status.update(state="error", error=str(e))
            log.warning("update to %s failed: %s", version, e)
            return
        self.status.update(state="installing")
        self.m.db.event("update", f"Устанавливается обновление {version}: менеджер перезапустится, задачи продолжат")
        helper = self.dir / "apply.py"
        shutil.copy2(APPLY, helper)  # the helper must not run from the folder it overwrites
        python = Path(sys.executable)
        if python.name.lower() == "pythonw.exe" and python.with_name("python.exe").exists():
            python = python.with_name("python.exe")
        subprocess.Popen([str(python), "-I", str(helper), str(ROOT), str(root), str(self.m.home), str(os.getpid()),
                          version], cwd=str(self.dir), creationflags=0x00000008 | 0x00000200 | 0x08000000,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        self.m.request_restart()
