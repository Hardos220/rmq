"""Local HTTP server: the dashboard (static files) and the JSON API for agents.

Listens on 127.0.0.1 only. The dashboard authenticates with the admin token;
agents introduce themselves with an X-RMQ-Agent header (the `rmq` CLI does it
automatically) and are registered on first contact. Custom headers cannot be sent
by web pages without a CORS preflight, which is never granted, and the Host
header is checked, so websites in the browser cannot reach the API.
"""
from __future__ import annotations

import json
import logging
import pathlib
import re
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .manager import ApiError, Identity, Manager
from .netbase import NetError

log = logging.getLogger("rmq")
STATIC = Path(__file__).with_name("static")
MAX_BODY = 4 << 20

ROUTES: list[tuple[str, re.Pattern, str]] = []


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn.__name__))
        return fn
    return deco


# Types of the dashboard's own files. Python's mimetypes reads them from the Windows registry, where other software
# sometimes registers .js as text/plain; the browser then refuses to run the dashboard's script (nosniff).
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml; charset=utf-8", ".ico": "image/x-icon",
    ".png": "image/png", ".txt": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8",
}


def content_type(name: str) -> str:
    return CONTENT_TYPES.get(pathlib.PurePath(name).suffix.lower(), "application/octet-stream")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # no second instance on the same port
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    server_version = f"rmq/{__version__}"
    protocol_version = "HTTP/1.1"
    manager: Manager = None  # set in serve()
    allowed_hosts: set[str] = set()
    shutdown_cb = None

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # ------------------------------------------------------------ plumbing
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data):
        self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _dispatch(self, method: str):
        host = (self.headers.get("Host") or "").lower()
        if host not in self.allowed_hosts:
            return self._json(403, {"error": "неверный Host"})
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        if method == "GET" and not path.startswith("/api/"):
            return self._static(path)
        for m, rx, name in ROUTES:
            match = rx.match(path)
            if m == method and match:
                break
        else:
            return self._json(404, {"error": "нет такого адреса API"})
        try:
            body = {}
            if method in ("POST", "PATCH"):
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    raise ApiError(413, "слишком большой запрос")
                raw = self.rfile.read(n) if n else b""
                if raw:
                    body = json.loads(raw.decode("utf-8"))
                    if not isinstance(body, dict):
                        raise ApiError(400, "тело запроса должно быть JSON-объектом")
            ident = None
            if name not in ("health", "guide"):
                auth = self.headers.get("Authorization") or ""
                token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
                agent = unquote(self.headers.get("X-RMQ-Agent") or "")
                info = unquote(self.headers.get("X-RMQ-Agent-Info") or "")
                ident = self.manager.identify(token, agent, info)
                if ident is None:
                    raise ApiError(401, "представьтесь: заголовок X-RMQ-Agent: <имя агента> "
                                        "(команда rmq делает это сама); дашборду нужен админ-токен")
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            result = getattr(self, name)(ident, body, query, *match.groups())
            if result is not None:
                self._json(200, result)
        except (ApiError, NetError) as e:
            self._json(e.status, {"error": e.message})
        except json.JSONDecodeError:
            self._json(400, {"error": "тело запроса — не JSON"})
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # the browser closed the page or gave up waiting: nobody to answer
        except Exception:
            log.exception("API error on %s %s", method, self.path)
            self._json(500, {"error": "внутренняя ошибка, подробности в data/manager.log"})

    def _static(self, path: str):
        name = "index.html" if path == "/" else path.lstrip("/")
        if name.startswith("static/"):
            name = name[len("static/"):]
        target = (STATIC / name).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            return self._json(404, {"error": "не найдено"})
        self._send(200, target.read_bytes(), content_type(target.name), {
            "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:",
        })

    @staticmethod
    def _admin(ident: Identity):
        if not ident.admin:
            raise ApiError(403, "только для владельца ПК (админ-токен дашборда)")

    @staticmethod
    def _int(q: dict, key: str, default: int) -> int:
        try:
            return int(q.get(key, default))
        except ValueError:
            raise ApiError(400, f"{key}: нужно целое число") from None

    # ------------------------------------------------------------ endpoints
    @route("GET", "/api/health")
    def health(self, ident, body, q):
        return {"ok": True, "version": __version__}

    @route("GET", "/api/whoami")
    def whoami(self, ident, body, q):
        a = ident.agent
        return {"admin": ident.admin, "agent": a["name"], "weight": a["weight"],
                "max_priority": {0: "low", 1: "normal", 2: "high"}[a["max_priority"]],
                "max_running": a["max_running"], "max_queued": a["max_queued"]}

    @route("GET", "/api/guide")
    def guide(self, ident, body, q):
        from .install import render_guide
        try:
            text = render_guide()
        except OSError:
            raise ApiError(404, "руководство AGENTS.md не найдено") from None
        self._send(200, text.encode("utf-8"), "text/markdown; charset=utf-8")
        return None

    @route("GET", "/api/state")
    def state(self, ident, body, q):
        if ident.admin:
            self.manager.dashboard_seen()
        return self.manager.get_state(ident)

    @route("GET", "/api/metrics")
    def metrics(self, ident, body, q):
        try:
            rng, since = float(q.get("range", 900)), float(q.get("since", 0))
        except ValueError:
            raise ApiError(400, "range/since: числа") from None
        return self.manager.metrics.series(max(10.0, min(86400.0, rng)), since)

    @route("GET", "/api/tasks")
    def tasks(self, ident, body, q):
        m = self.manager
        agent_id = ident.agent["id"] if q.get("mine") in ("1", "true") else None
        rows = m.db.tasks_list(state=q.get("state"), agent_id=agent_id, limit=self._int(q, "limit", 100))
        agents = {a["id"]: a for a in m.db.agents()}
        with m.lock:
            return {"tasks": [m.task_json(r, agents) for r in rows]}

    @route("POST", "/api/tasks")
    def submit(self, ident, body, q):
        return self.manager.submit(ident, body)

    @route("GET", r"/api/tasks/(\d+)")
    def task(self, ident, body, q, tid):
        return self.manager.get_task(ident, int(tid))

    @route("GET", r"/api/tasks/(\d+)/log")
    def task_log(self, ident, body, q, tid):
        return self.manager.read_log(ident, int(tid), self._int(q, "offset", 0), self._int(q, "limit", 65536))

    @route("POST", r"/api/tasks/(\d+)/cancel")
    def cancel(self, ident, body, q, tid):
        return self.manager.cancel(ident, int(tid))

    @route("POST", r"/api/tasks/(\d+)/suspend")
    def suspend(self, ident, body, q, tid):
        return self.manager.suspend_task(ident, int(tid))

    @route("POST", r"/api/tasks/(\d+)/resume")
    def resume(self, ident, body, q, tid):
        return self.manager.resume_task(ident, int(tid))

    @route("POST", r"/api/tasks/(\d+)/priority")
    def priority(self, ident, body, q, tid):
        return self.manager.set_priority(ident, int(tid), body.get("priority"))

    @route("POST", "/api/mode")
    def mode(self, ident, body, q):
        self._admin(ident)
        return self.manager.set_mode(str(body.get("mode", "")))

    @route("GET", "/api/settings")
    def settings_get(self, ident, body, q):
        return self.manager.settings.snapshot()

    @route("PATCH", "/api/settings")
    def settings_patch(self, ident, body, q):
        self._admin(ident)
        return self.manager.update_settings(body)

    @route("POST", "/api/settings/reset")
    def settings_reset(self, ident, body, q):
        self._admin(ident)
        return self.manager.reset_settings()

    @route("GET", "/api/agents")
    def agents(self, ident, body, q):
        return self.manager.get_state(ident)["agents"]

    @route("PATCH", r"/api/agents/(\d+)")
    def agent_update(self, ident, body, q, aid):
        self._admin(ident)
        return self.manager.update_agent(int(aid), body)

    @route("GET", "/api/agent-rules")
    def agent_rules(self, ident, body, q):
        self._admin(ident)
        from .install import AGENT_FILES, BEGIN, render_rules
        installed = []
        for tool, folder, rel in AGENT_FILES:
            path = folder / rel
            try:
                if BEGIN[:14] in path.read_text(encoding="utf-8"):
                    installed.append({"tool": tool, "path": str(path)})
            except OSError:
                pass
        return {"text": render_rules(), "installed": installed}

    @route("GET", "/api/stats")
    def stats(self, ident, body, q):
        return self.manager.get_stats(q.get("period", "7d"))

    # ------------------------------------------------------------ the compute network
    @route("GET", "/api/nodes")
    def nodes(self, ident, body, q):
        return {"nodes": self.manager.nodes(), "mode": self.manager.netstore.mode}

    @route("GET", "/api/network")
    def network(self, ident, body, q):
        self._admin(ident)
        return self.manager.net.view()

    @route("POST", "/api/network/create")
    def network_create(self, ident, body, q):
        self._admin(ident)
        return self.manager.net.create(str(body.get("name") or ""))

    @route("POST", "/api/network/invite")
    def network_invite(self, ident, body, q):
        self._admin(ident)
        return {"code": self.manager.net.invite()}

    @route("POST", "/api/network/join")
    def network_join(self, ident, body, q):
        self._admin(ident)
        return self.manager.net.join(str(body.get("code") or ""))

    @route("POST", "/api/network/leave")
    def network_leave(self, ident, body, q):
        self._admin(ident)
        return self.manager.net.leave()

    @route("POST", "/api/network/mode")
    def network_mode(self, ident, body, q):
        self._admin(ident)
        return self.manager.net.set_mode(str(body.get("mode") or ""))

    @route("POST", "/api/network/rename")
    def network_rename(self, ident, body, q):
        self._admin(ident)
        return self.manager.net.rename(str(body.get("name") or ""))

    @route("POST", "/api/update/check")
    def update_check(self, ident, body, q):
        self._admin(ident)
        up = self.manager.updater
        threading.Thread(target=up.check_and_install, name="rmq-update", daemon=True).start()
        return {k: v for k, v in up.status.items() if k != "release"}

    @route("GET", "/api/chronicle")
    def chronicle(self, ident, body, q):
        try:
            days = float(q.get("days", 7))
        except ValueError:
            raise ApiError(400, "days: число") from None
        return self.manager.get_chronicle(max(0.04, min(30.0, days)))

    @route("GET", r"/api/stats/files/([\w.-]+)")
    def stats_file(self, ident, body, q, name):
        self._admin(ident)
        path = self.manager.stats.file_path(name)
        if path is None:
            raise ApiError(404, "нет такого файла статистики")
        ctype = "text/csv; charset=utf-8" if name.endswith(".csv") else "text/plain; charset=utf-8"
        self._send(200, path.read_bytes(), ctype, {"Content-Disposition": f'attachment; filename="{name}"'})
        return None

    @route("GET", "/api/events")
    def events(self, ident, body, q):
        return {"events": self.manager.db.events(after_id=self._int(q, "after", 0),
                                                 limit=min(500, self._int(q, "limit", 100)))}

    @route("POST", "/api/shutdown")
    def shutdown(self, ident, body, q):
        self._admin(ident)
        result = self.manager.prepare_shutdown(str(body.get("tasks", "freeze")))
        if Handler.shutdown_cb:
            threading.Timer(0.3, Handler.shutdown_cb).start()
        result["note"] = {"freeze": "менеджер выключен; задачи заморожены и продолжат после запуска",
                          "keep": "менеджер выключен; задачи продолжают работать и будут подхвачены при запуске",
                          "cancel": "менеджер выключен; задачи отменены"}[result["tasks"]]
        return result


def setup_logging(home: Path, console: bool) -> None:
    root = logging.getLogger("rmq")
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(home / "manager.log", maxBytes=1 << 20, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(fh)
    if console and sys.stderr:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        root.addHandler(ch)


def serve(home: Path, open_browser: bool = False) -> int:
    home.mkdir(parents=True, exist_ok=True)
    setup_logging(home, console=sys.stdout is not None)
    manager = Manager(home)
    s = manager.settings.section("server")
    host, port = s["host"], int(s["port"])
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("server.host=%s: the API is meant to be local only; using 127.0.0.1", host)
        host = "127.0.0.1"
    try:
        httpd = Server((host, port), Handler)
    except OSError as e:
        log.error("port %d is busy (%s). Is the manager already running? Open http://127.0.0.1:%d", port, e, port)
        return 2
    Handler.manager = manager
    Handler.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    Handler.shutdown_cb = httpd.shutdown
    manager.restart_cb = httpd.shutdown
    manager.start()
    url = f"http://127.0.0.1:{port}/#token={manager.admin_token}"
    (home / "dashboard_url.txt").write_text(url, encoding="utf-8")
    log.info("RMQ %s is running. Dashboard: http://127.0.0.1:%d (full link with the token: %s)",
             __version__, port, home / "dashboard_url.txt")
    if open_browser or manager.open_dashboard_once:
        webbrowser.open(url)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("stopping; its tasks (frozen or still running) are picked up again on the next start")
        manager.stop()
        httpd.server_close()
    return 0
