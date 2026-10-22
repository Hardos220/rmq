"""A report for the author (rmq report, the "Отчёт для автора" button): what helps to understand a problem, without
what is private.

Inside: the version, the hardware and Windows, the sensors, the manager's state and the settings the owner changed,
the manager's log, the journal and the latest tasks. Never inside: the dashboard's token, the network keys, the
owner's code word, task commands, their folders, environments and logs. Names and addresses are replaced on the way
out: the user's folder becomes %USERPROFILE%, the user and the PC <user> and <pc>, the PCs of the network ПК-1,
ПК-2…, IP addresses x.x.x.x, and long strings that may be keys or tokens <скрыто>. The owner looks it over before
sending it anyway: README.txt in the archive says what to check.
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from pathlib import Path

from . import __version__

LOG_LINES = 5000
EVENTS = 1000
TASKS = 300

README = """Отчёт менеджера ресурсов RMQ для автора / RMQ report for the author

Что внутри:
  summary.json  — версия, железо, Windows, датчики, состояние менеджера, изменённые вами настройки
  manager.log   — журнал работы менеджера (последние строки)
  events.txt    — события из вкладки «Журнал»
  tasks.txt     — последние задачи: состояние, заявка, время, код выхода (без команд, папок и логов задач)

Чего здесь нет: токена дашборда, ключей сети, кодового слова, команд задач, их папок, окружения и логов.
Пути к вашей папке пользователя заменены на %USERPROFILE%, имя пользователя и ПК — на <user> и <pc>,
имена ПК сети — на ПК-1, ПК-2…, IP-адреса — на x.x.x.x, длинные строки, похожие на ключи, — на <скрыто>.

Перед отправкой просмотрите файлы: если найдёте то, чем не хотите делиться, удалите это.
Прислать: issue на https://github.com/Hardos220/rmq/issues (приложите архив) или письмом автору.

Inside: version, hardware, sensors, state, changed settings, the manager's log, the journal, the latest tasks.
Not inside: tokens, network keys, the code word, task commands, folders and logs. Look it over before sending.
"""


class Scrubber:
    """Replaces what is private in a text: the user's folder, names of the user, of this PC and of the network's
    PCs, IP addresses, e-mails, tokens and keys, the owner's code word."""

    LONG = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-+/]{32,}={0,2}")  # tokens, keys, hashes
    SECRET = re.compile(r"(?i)(token=|bearer\s+|\"(?:key|token|k)\"\s*:\s*\")[^\s\"&#]+")
    EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
    IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
    # compressed (fd7a:115c:a1e0::1) or full; times like 10:31:04 are neither
    IPV6 = re.compile(r"(?i)(?<![\w:])(?:(?:[0-9a-f]{1,4}:){1,7}:(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?"
                      r"|(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4})(?![\w:])")

    def __init__(self, home: str = "", user: str = "", pc: str = "", names: dict[str, str] | None = None,
                 words: list[str] | None = None):
        self.paths: list[re.Pattern] = []
        if home:
            h = home.rstrip("\\/")
            forms = {h, h.replace("\\", "/")}
            if len(h) > 2 and h[1] == ":":
                forms.add("/" + h[0].lower() + h[2:].replace("\\", "/"))
                forms.add("/" + h[0].upper() + h[2:].replace("\\", "/"))
            self.paths = [re.compile(re.escape(f), re.I) for f in sorted(forms, key=len, reverse=True)]
        self.names = [(re.compile(re.escape(n), re.I), alias) for n, alias in
                      sorted((names or {}).items(), key=lambda kv: -len(kv[0])) if n and len(n) >= 2]
        self.words = [re.compile(re.escape(w), re.I) for w in (words or []) if w]
        self.user = re.compile(rf"(?<!\w){re.escape(user)}(?!\w)", re.I) if len(user) >= 3 else None
        self.pc = re.compile(rf"(?<!\w){re.escape(pc)}(?!\w)", re.I) if len(pc) >= 3 else None

    def __call__(self, text: str) -> str:
        if not text:
            return text
        for rx in self.paths:
            text = rx.sub("%USERPROFILE%", text)
        for rx in self.words:
            text = rx.sub("<кодовое слово>", text)
        text = self.SECRET.sub(lambda m: m.group(1) + "<скрыто>", text)
        text = self.LONG.sub("<скрыто>", text)
        text = self.EMAIL.sub("<почта>", text)
        text = self.IPV4.sub(lambda m: m.group(0) if m.group(1) in ("127", "0") else "x.x.x.x", text)
        text = self.IPV6.sub("x:x::x", text)
        for rx, alias in self.names:
            text = rx.sub(alias, text)
        if self.pc:
            text = self.pc.sub("<pc>", text)
        if self.user:
            text = self.user.sub("<user>", text)
        return text

    def tree(self, value):
        """The same over every string of a JSON-like value (keys too)."""
        if isinstance(value, str):
            return self(value)
        if isinstance(value, dict):
            return {self(str(k)): self.tree(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.tree(v) for v in value]
        return value


def changed_settings(current: dict, defaults: dict) -> dict:
    """Only what differs from the built-in defaults; the owner's code word is never shown, only whether it is set."""
    out: dict = {}
    for sec, vals in current.items():
        if not isinstance(vals, dict) or sec.startswith("_"):
            continue
        base = defaults.get(sec) or {}
        diff = {k: v for k, v in vals.items() if base.get(k) != v}
        if diff:
            out[sec] = diff
    code = (out.get("agents") or {}).get("priority_code")
    if code is not None:
        out["agents"]["priority_code"] = "<задано>" if code else ""
    return out


def _names(m) -> dict[str, str]:
    """The names of the network's PCs and of the networks themselves, with what they become in the report."""
    st = m.netstore
    out: dict[str, str] = {}
    if st.node_name:
        out[st.node_name] = "этот ПК"
    n = 0
    for kind in ("home", "ext"):
        for nid, rec in sorted(st.book(kind).items()):
            name = (rec or {}).get("name")
            if name and name not in out and nid != st.node_id:
                n += 1
                out[name] = f"ПК-{n}"
    if st.home and st.home.get("name"):
        out.setdefault(st.home["name"], "<мои ПК>")
    if st.network and st.network.get("name"):
        out.setdefault(st.network["name"], "<сеть>")
    return out


def _tail(path: Path, lines: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - lines * 400))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])


def _when(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"


def build(m) -> tuple[str, bytes]:
    """(file name, the zip's bytes) for manager `m`."""
    from .settings import DEFAULTS
    snap = m.settings.snapshot()
    code = (snap.get("agents") or {}).get("priority_code") or ""
    scrub = Scrubber(home=os.path.expanduser("~"), user=os.environ.get("USERNAME", ""),
                     pc=os.environ.get("COMPUTERNAME", ""), names=_names(m), words=[code] if code else [])
    now = time.time()
    st = m.state or {}
    summary = {
        "generated": _when(now), "version": __version__, "dev_checkout": (Path(__file__).parent.parent / ".git").exists(),
        "hardware": m.hardware(), "mode": st.get("mode"), "game": st.get("game"), "power": st.get("power"),
        "thermal": st.get("thermal"), "disk": st.get("disk"), "system": st.get("system"), "counts": st.get("_counts"),
        "update": st.get("update"), "network": {**m.net_summary(), "network": None, "home": None},
        "nodes": [{k: v for k, v in n.items() if k not in ("free", "tasks")} for n in m.nodes()],
        "settings_changed": changed_settings(snap, DEFAULTS),
    }
    agents = {a["id"]: a["name"] for a in m.db.agents()}
    events = [f"{_when(e['ts'])}  {e['kind']:<10} " + (f"#{e['task_id']} " if e.get("task_id") else "")
              + (f"[{e.get('agent_name')}] " if e.get("agent_name") else "") + (e.get("message") or "")
              for e in m.db.events(limit=EVENTS)]
    tasks = []
    for r in m.db.tasks_list(limit=TASKS):
        tasks.append("  ".join(str(x) for x in (
            f"#{r['id']}", r["state"], r.get("state_reason") or "-", agents.get(r.get("agent_id"), r.get("agent_id")),
            r.get("name") or "-", f"cpu {r['cpu']}", f"ram {r['ram_gb']}", f"vram {r['vram_gb']}",
            f"time {r.get('time_s')}", f"run {round(r.get('run_s') or 0)}s", f"exit {r.get('exit_code')}",
            f"restarts {r.get('restarts', 0)}", f"peak_ram {r.get('peak_ram_gb')}", f"preempt {r.get('preempt')}",
            f"submitted {_when(r.get('submitted_at'))}", f"finished {_when(r.get('finished_at'))}",
            "remote" if r.get("state") == "remote" else "", "hosted" if r.get("origin_node") else "")))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", README)
        z.writestr("summary.json", json.dumps(scrub.tree(summary), ensure_ascii=False, indent=2, default=str))
        z.writestr("manager.log", scrub(_tail(m.home / "manager.log", LOG_LINES)))
        z.writestr("events.txt", scrub("\n".join(events)))
        z.writestr("tasks.txt", scrub("\n".join(tasks)))
    name = f"rmq-report-{__version__}-{time.strftime('%Y%m%d-%H%M', time.localtime(now))}.zip"
    return name, buf.getvalue()
