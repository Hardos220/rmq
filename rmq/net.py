"""The compute network: several PCs running RMQ lend each other their spare capacity.

Transport: plain HTTP between managers over Tailscale (a free encrypted private network that works through
any router). The network listener binds to this PC's Tailscale address only, so nothing listens on the
internet or on the local network. Every request is signed with the network's shared key (HMAC-SHA256 over
the method, path, time, a one-time nonce and the body hash); unsigned or replayed requests are refused.

Modes of this PC:
  two_way  - my agents may use other PCs, and members' tasks may run here (low priority, write-protected)
  one_way  - my agents may use other PCs; this PC is mine alone: members' tasks here are frozen
  offline  - no network at all; the manager works on its own, as before

Membership: the creator makes the network and hands out invite codes (its address + the key). A joiner
registers with the inviter and gets the member list; then every node says hello to every other one every
30 s, exchanging its live state (free resources, mode, version) and the member list (newest record wins).

Tasks for another PC ("proxy" tasks here, "hosted" tasks there): the origin packs the project folder,
sends the task and the archive, follows its state, and when it ends brings back the files the task created
or changed, then tells the host to delete its copy.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .netbase import (MODES, PROTOCOL, NetError, NetStore, NonceCache, body_hash, collect_files, file_sha256,
                      make_invite, make_zip, member_record, parse_invite, safe_extract, sign_headers,
                      tailscale_address, verify)

log = logging.getLogger("rmq")

# peers are reached directly over Tailscale: never through a system proxy (and no reverse-DNS lookups that
# urllib does to decide whether to bypass one, which cost up to seconds per request)
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HELLO_EVERY_S = 30
SYNC_EVERY_S = 3
ONLINE_S = 95          # a member is "in the network" if it answered within this time
MAX_JSON = 1 << 20


class PeerError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status
        self.message = message


class NetService:
    """Membership, heartbeats, the listener, and the life of tasks sent to other PCs."""

    def __init__(self, manager):
        self.m = manager
        self.store: NetStore = manager.netstore
        self.nonces = NonceCache()
        self.live: dict[str, dict] = {}       # node id -> {"seen", "info", "rtt_ms", "error"}
        self.uploads: dict[str, dict] = {}    # upload id -> accepted task spec waiting for its archive
        self.server: ThreadingHTTPServer | None = None
        self.address: str | None = None       # "100.x.y.z:8778" while listening
        self.status = "offline"               # offline | no_network | no_tailscale | listening | error
        self.error = ""
        self.pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="rmq-net")
        self._busy: set[int] = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_hello = 0.0
        self._last_sync = 0.0

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="rmq-net", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._stop_server()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def poke(self) -> None:
        self._last_hello = 0.0
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("network tick failed")
            self._wake.wait(1.0)
            self._wake.clear()

    def _tick(self) -> None:
        cfg = self.m.settings.section("network")
        want = self.store.mode != "offline" and self.store.network is not None
        if not want:
            self._stop_server()
            self.status = "offline" if self.store.mode == "offline" else "no_network"
            return
        self._ensure_server(cfg)
        now = time.time()
        if self.server and now - self._last_hello >= HELLO_EVERY_S:
            self._last_hello = now
            for nid, rec in self.store.active_members().items():
                if rec.get("address"):
                    self.pool.submit(self._hello, nid, rec)
        if now - self._last_sync >= SYNC_EVERY_S:
            self._last_sync = now
            self._sync_proxies()
            self._expire_uploads(now)

    # ------------------------------------------------------------ the listener
    def bind_ip(self, cfg: dict) -> str | None:
        bind = (cfg.get("bind") or "tailscale").strip()
        return tailscale_address() if bind == "tailscale" else bind

    def _ensure_server(self, cfg: dict) -> None:
        ip = self.bind_ip(cfg)
        if not ip:
            self._stop_server()
            self.status, self.error = "no_tailscale", ""
            return
        address = f"{ip}:{int(cfg['port'])}"
        if self.server and self.address == address:
            return
        self._stop_server()
        try:
            srv = NetHTTPServer((ip, int(cfg["port"])), NetHandler)
        except OSError as e:
            self.status, self.error = "error", f"не удалось открыть {address}: {e}"
            return
        srv.service = self
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5}, name="rmq-net-http",
                         daemon=True).start()
        self.server, self.address, self.status, self.error = srv, address, "listening", ""
        self._update_self_record(address)
        log.info("network listener on %s", address)

    def _stop_server(self) -> None:
        srv, self.server, self.address = self.server, None, None
        if srv:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass

    def _update_self_record(self, address: str | None) -> None:
        st = self.store
        with st._lock:
            me = st.members.get(st.node_id)
            if me is None or me.get("address") != address or me.get("name") != st.node_name or me.get("left_at"):
                joined = me.get("joined_at") if me else time.time()
                st.members[st.node_id] = member_record(st.node_name, address, joined)
                st.save()

    # ------------------------------------------------------------ what this node tells the others
    def self_info(self) -> dict:
        st = self.m.state or {}
        sysd = st.get("system") or {}
        cpu, ram, vram = sysd.get("cpu") or {}, sysd.get("ram") or {}, sysd.get("vram") or {}
        free = {"cpu": max(0.0, cpu.get("pool_cores", 0) - cpu.get("held", 0)),
                "ram": max(0.0, ram.get("pool", 0) - ram.get("held", 0)),
                "vram": max(0.0, vram.get("pool", 0) - vram.get("held", 0)) if vram else 0.0}
        counts = dict(st.get("_counts") or {})
        th = st.get("thermal") or {}
        return {
            "node_id": self.store.node_id, "name": self.store.node_name, "address": self.address,
            "version": __version__, "protocol": PROTOCOL, "mode": self.store.mode,
            "accepting": self.store.mode == "two_way" and self.server is not None,
            "hw": self.m.hardware(), "free": {k: round(v, 2) for k, v in free.items()},
            "busy": {"game": bool((st.get("game") or {}).get("active")), "paused": st.get("mode") == "paused",
                     "heat": th.get("state", "normal"),
                     "disk_low": any(d.get("low") for d in (st.get("disk") or {}).get("drives", []))},
            "tasks": counts, "hosted": len(self.m.hosted_active()),
        }

    # ------------------------------------------------------------ talking to a peer
    def _request(self, address: str, method: str, path: str, *, body: bytes | None = None, data_file: Path | None = None,
                 bhash: str | None = None, timeout: float = 15, out_file: Path | None = None) -> bytes | dict:
        st = self.store
        if not st.network:
            raise PeerError("этот ПК не в сети")
        if data_file is not None:
            size = data_file.stat().st_size
            payload = open(data_file, "rb")
        else:
            payload = body or b""
            size = len(payload)
            bhash = body_hash(payload)
        headers = sign_headers(st.key, st.network["id"], st.node_id, method, path, bhash)
        headers["Content-Length"] = str(size)
        if body is not None or data_file is not None:
            headers["Content-Type"] = "application/octet-stream" if data_file is not None else "application/json"
        req = urllib.request.Request(f"http://{address}{path}", data=payload if size or method in ("POST", "PUT") else None,
                                     method=method, headers=headers)
        try:
            with DIRECT.open(req, timeout=timeout) as r:
                if out_file is not None:
                    tmp = out_file.with_suffix(".part")
                    with open(tmp, "wb") as f:
                        shutil.copyfileobj(r, f, 1 << 20)
                    os.replace(tmp, out_file)
                    return {}
                raw = r.read()
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error", str(e))
            except ValueError:
                msg = str(e)
            raise PeerError(msg, e.code) from None
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            raise PeerError(f"нет связи с {address}: {getattr(e, 'reason', e)}") from None
        finally:
            if data_file is not None:
                payload.close()
        if r.headers.get("Content-Type", "").startswith("application/json"):
            return json.loads(raw.decode("utf-8"))
        return raw

    def call(self, node_id: str, method: str, path: str, payload: dict | None = None, **kw):
        rec = self.store.members.get(node_id)
        if not rec or not rec.get("address"):
            raise PeerError("адрес этого ПК неизвестен")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        return self._request(rec["address"], method, path, body=body, **kw)

    def _hello(self, nid: str, rec: dict) -> None:
        t0 = time.monotonic()
        try:
            r = self._request(rec["address"], "POST", "/net/v1/hello",
                              body=json.dumps({"info": self.self_info(), "members": self.store.members},
                                              ensure_ascii=False).encode("utf-8"), timeout=10)
        except PeerError as e:
            with self._lock:
                self.live.setdefault(nid, {})["error"] = e.message
            return
        rtt = (time.monotonic() - t0) * 1000
        self._note_peer(r.get("info") or {}, rtt)
        if self.store.merge(r.get("members") or {}):
            self.poke()

    def _note_peer(self, info: dict, rtt: float | None = None) -> None:
        nid = info.get("node_id")
        if not nid or nid == self.store.node_id:
            return
        with self._lock:
            cur = self.live.setdefault(nid, {})
            cur.update(seen=time.time(), info=info, error="")
            if rtt is not None:
                cur["rtt_ms"] = round(rtt)

    def online(self, nid: str) -> bool:
        with self._lock:
            seen = self.live.get(nid, {}).get("seen", 0)
        return time.time() - seen <= ONLINE_S

    # ------------------------------------------------------------ owner actions
    def create(self, name: str) -> dict:
        st = self.store
        if st.network:
            raise NetError("этот ПК уже в сети: сначала выйдите из неё")
        name = (name or "").strip()[:60] or f"Сеть {st.node_name}"
        with st._lock:
            st.network = {"id": "net-" + secrets.token_hex(6), "name": name,
                          "key": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
                          "created_by": st.node_id, "created_at": time.time()}
            st.members = {st.node_id: member_record(st.node_name, self.address, time.time())}
            if st.mode == "offline":
                st.mode = "two_way"
            st.save()
        self.m.db.event("net", f"Создана вычислительная сеть «{name}»")
        self.poke()
        return self.view()

    def invite(self) -> str:
        st = self.store
        if not st.network:
            raise NetError("сначала создайте сеть или присоединитесь к ней")
        if not self.address:
            raise NetError("этот ПК сейчас не на связи: нужен запущенный Tailscale и режим не «офлайн»")
        return make_invite(st.network, self.address, st.node_name)

    def join(self, code: str) -> dict:
        st = self.store
        inv = parse_invite(code)
        if st.network and st.network["id"] != inv["n"]:
            raise NetError("этот ПК уже в другой сети: сначала выйдите из неё")
        cfg = self.m.settings.section("network")
        ip = self.bind_ip(cfg)
        if not ip:
            raise NetError("не найден адрес Tailscale: установите Tailscale, войдите в него и повторите")
        my_address = f"{ip}:{int(cfg['port'])}"
        with st._lock:
            st.network = {"id": inv["n"], "name": inv.get("name") or "Сеть", "key": inv["k"],
                          "created_by": None, "created_at": time.time()}
            if st.mode == "offline":
                st.mode = "two_way"
            st.members[st.node_id] = member_record(st.node_name, my_address, time.time())
        try:
            r = self._request(inv["a"], "POST", "/net/v1/join",
                              body=json.dumps({"info": self.self_info() | {"address": my_address},
                                               "record": st.members[st.node_id]}, ensure_ascii=False).encode("utf-8"),
                              timeout=20)
        except PeerError as e:
            with st._lock:
                st.network, st.members = None, {}
                st.save()
            raise NetError(f"не удалось присоединиться: {e.message}") from None
        with st._lock:
            st.network["created_by"] = r.get("network", {}).get("created_by")
            st.save()
        st.merge(r.get("members") or {})
        self._note_peer(r.get("info") or {})
        self.m.db.event("net", f"ПК присоединился к сети «{st.network['name']}» (пригласил {inv.get('by', '?')})")
        self.poke()
        return self.view()

    def leave(self) -> dict:
        st = self.store
        if not st.network:
            raise NetError("этот ПК не в сети")
        name = st.network["name"]
        self.m.net_cancel_all("этот ПК вышел из сети")
        for nid, rec in st.active_members().items():
            if rec.get("address"):
                try:
                    self._request(rec["address"], "POST", "/net/v1/leave", body=b"{}", timeout=5)
                except PeerError:
                    pass
        with st._lock:
            st.network, st.members = None, {}
            st.save()
        with self._lock:
            self.live.clear()
        self._stop_server()
        self.m.db.event("net", f"ПК вышел из сети «{name}»")
        return self.view()

    def set_mode(self, mode: str) -> dict:
        if mode not in MODES:
            raise NetError("режим: two_way, one_way или offline")
        st = self.store
        with st._lock:
            st.mode = mode
            st.save()
        self.m.db.event("net", {"two_way": "Сеть: двусторонний режим — ПК доступен участникам",
                                "one_way": "Сеть: односторонний режим — ПК только ваш, задачи участников заморожены",
                                "offline": "Сеть: офлайн — все соединения выключены"}[mode])
        self.poke()
        self.m.wake.set()
        return self.view()

    def rename(self, name: str) -> dict:
        name = " ".join((name or "").split())[:40]
        if not name:
            raise NetError("имя не может быть пустым")
        st = self.store
        with st._lock:
            st.node_name = name
            st.save()
        self._update_self_record(self.address)
        self.poke()
        return self.view()

    def view(self) -> dict:
        """Everything the "Сеть" tab shows."""
        st = self.store
        me = self.self_info()
        members = []
        with self._lock:
            live = {k: dict(v) for k, v in self.live.items()}
        for nid, rec in sorted(st.members.items(), key=lambda kv: (kv[0] != st.node_id, kv[1].get("joined_at") or 0)):
            if nid == st.node_id:
                members.append({"node_id": nid, "name": st.node_name, "self": True, "online": self.server is not None,
                                "status": self.status, "info": me, "address": self.address, "left": False,
                                "joined_at": rec.get("joined_at")})
                continue
            lv = live.get(nid, {})
            members.append({"node_id": nid, "name": rec.get("name"), "self": False, "address": rec.get("address"),
                            "left": bool(rec.get("left_at")), "left_at": rec.get("left_at"),
                            "online": not rec.get("left_at") and time.time() - lv.get("seen", 0) <= ONLINE_S,
                            "seen": lv.get("seen"), "rtt_ms": lv.get("rtt_ms"), "error": lv.get("error"),
                            "info": lv.get("info"), "joined_at": rec.get("joined_at")})
        net = st.network
        return {"node_id": st.node_id, "node_name": st.node_name, "mode": st.mode, "status": self.status,
                "error": self.error, "address": self.address,
                "tailscale": tailscale_address(),
                "network": {"id": net["id"], "name": net["name"], "created_by": net.get("created_by"),
                            "created_at": net.get("created_at")} if net else None,
                "members": members}

    # ------------------------------------------------------------ tasks sent to other PCs (origin side)
    def best_node(self, need: dict) -> str | None:
        """The online member that can start this task right now with the most CPU to spare."""
        best, best_cpu = None, -1.0
        with self._lock:
            live = {k: dict(v) for k, v in self.live.items()}
        for nid in self.store.active_members():
            lv = live.get(nid, {})
            info = lv.get("info") or {}
            if time.time() - lv.get("seen", 0) > ONLINE_S or not info.get("accepting"):
                continue
            busy = info.get("busy") or {}
            if busy.get("paused") or busy.get("heat") == "hot" or busy.get("disk_low"):
                continue
            free = info.get("free") or {}
            if free.get("ram", 0) + 1e-6 < need["ram"] or free.get("vram", 0) + 1e-6 < need["vram"]:
                continue
            if need["vram"] > 0 and busy.get("game"):
                continue
            cpu = free.get("cpu", 0)
            if cpu >= min(need["cpu"], 1.0) and cpu > best_cpu:
                best, best_cpu = nid, cpu
        return best

    def node_name(self, nid: str | None) -> str:
        if not nid:
            return "?"
        rec = self.store.members.get(nid) or {}
        return rec.get("name") or nid

    def _sync_proxies(self) -> None:
        for row in self.m.db.tasks_remote():
            tid = row["id"]
            with self._lock:
                if tid in self._busy:
                    continue
                self._busy.add(tid)
            self.pool.submit(self._sync_one, tid)
        self.m.net_retry_cancels()

    def _sync_one(self, tid: int) -> None:
        try:
            row = self.m.db.task(tid)
            if not row or row["state"] != "remote":
                return
            phase = row["state_reason"] or "net:staging"
            if phase == "net:staging":
                self._send(row)
            else:
                self._follow(row)
        except Exception:
            log.exception("network task #%d", tid)
        finally:
            with self._lock:
                self._busy.discard(tid)

    def _send(self, row: dict) -> None:
        m, tid, node = self.m, row["id"], row["remote_node"]
        spec = json.loads(row["net_spec"] or "{}")
        cfg = m.settings.section("network")
        if not self.online(node):
            m.net_remote_update(tid, {"phase_text": f"ждёт связи с {self.node_name(node)}"})
            return
        out = m.net_dir("out") / f"bundle-{tid}.zip"
        try:
            files = collect_files(Path(row["cwd"]), spec.get("files"), max_bytes=int(cfg["max_bundle_mb"]) << 20)
            m.net_remote_update(tid, {"phase_text": f"упаковываем {len(files)} файлов"})
            sha = make_zip(Path(row["cwd"]), files, out)
            size = out.stat().st_size
            if spec.get("requirements") and not any(f[0] == spec["requirements"] for f in files):
                raise NetError(f"файл зависимостей {spec['requirements']} не попал в отправку")
            payload = {"origin_task": tid, "agent": spec.get("agent"), "name": row["name"],
                       "argv": json.loads(row["argv"]) if row["argv"] else None, "command": row["command"],
                       "cpu": row["cpu"], "ram": row["ram_gb"], "vram": row["vram_gb"], "time_s": row["time_s"],
                       "time_limit_s": row["time_limit_s"], "preempt": row["preempt"], "env": spec.get("env") or {},
                       "requirements": spec.get("requirements"), "results": spec.get("results"),
                       "bundle": {"size": size, "sha256": sha, "files": len(files)}}
            r = self.call(node, "POST", "/net/v1/tasks", payload, timeout=30)
            m.net_remote_update(tid, {"phase_text": f"отправляем {size / 2**20:.1f} МБ"})
            up = self._request(self.store.members[node]["address"], "PUT", f"/net/v1/uploads/{r['upload_id']}",
                               data_file=out, bhash=sha, timeout=max(60.0, size / 2**19))
        except NetError as e:
            m.net_proxy_failed(tid, e.message)
            return
        except PeerError as e:
            if e.status in (400, 403, 404, 409, 413, 507):
                m.net_proxy_failed(tid, e.message)
            else:
                m.net_remote_update(tid, {"phase_text": f"повторим отправку: {e.message}"})
            return
        finally:
            out.unlink(missing_ok=True)
        m.net_proxy_sent(tid, int(up["task_id"]))

    def _follow(self, row: dict) -> None:
        m, tid, node, rid = self.m, row["id"], row["remote_node"], row["remote_id"]
        if node not in self.store.members or self.store.members[node].get("left_at"):
            m.net_proxy_failed(tid, f"ПК {self.node_name(node)} вышел из сети")
            return
        try:
            st = self.call(node, "GET", f"/net/v1/tasks/{rid}", timeout=10)
        except PeerError as e:
            if e.status == 404:
                m.net_proxy_failed(tid, f"на {self.node_name(node)} этой задачи больше нет")
            else:
                m.net_remote_update(tid, {"phase_text": f"нет связи с {self.node_name(node)}"})
            return
        m.net_remote_update(tid, {"remote": st, "phase_text": ""})
        if not st.get("final"):
            return
        # the task ended there: bring back its log and the files it created or changed, then clean up there
        m.net_remote_update(tid, {"phase_text": "забираем результаты"})
        tdir = m.task_dir(tid)
        tdir.mkdir(parents=True, exist_ok=True)
        try:
            self.call(node, "GET", f"/net/v1/tasks/{rid}/log?all=1", timeout=60, out_file=tdir / "output.log")
            res = m.net_dir("res") / f"results-{tid}.zip"
            self.call(node, "GET", f"/net/v1/tasks/{rid}/results", timeout=600, out_file=res)
        except PeerError as e:
            m.net_remote_update(tid, {"phase_text": f"не удалось забрать результаты: {e.message}"})
            return
        try:
            manifest = safe_extract(res, Path(row["cwd"]))
        except NetError as e:
            m.net_proxy_failed(tid, f"результаты не распакованы: {e.message}")
            return
        finally:
            res.unlink(missing_ok=True)
        try:
            self.call(node, "DELETE", f"/net/v1/tasks/{rid}", timeout=10)
        except PeerError:
            pass
        m.net_proxy_finished(tid, st, len(manifest), sum(v[0] for v in manifest.values()))

    def _expire_uploads(self, now: float) -> None:
        with self._lock:
            for uid in [u for u, v in self.uploads.items() if v["expires"] < now]:
                self.uploads.pop(uid, None)


# ---------------------------------------------------------------- the listener (host side)
class NetHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    service: NetService = None


class NetHandler(BaseHTTPRequestHandler):
    server_version = f"rmq-net/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _send(self, status: int, data: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    def _dispatch(self, method: str) -> None:
        svc: NetService = self.server.service
        st = svc.store
        try:
            if not st.network:
                raise NetError("этот ПК не в сети", 403)
            node = verify(self.headers, st.key, st.network["id"], method, self.path, svc.nonces)
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            if parts[:2] != ["net", "v1"]:
                raise NetError("нет такого адреса", 404)
            parts = parts[2:]
            rec = st.members.get(node)
            if rec and rec.get("left_at") and parts != ["join"]:
                raise NetError("этот ПК вышел из сети: присоединитесь заново по приглашению", 403)
            if method == "PUT" and len(parts) == 2 and parts[0] == "uploads":
                return self._upload(svc, node, parts[1])
            body = self._read_json()
            if method == "POST" and parts == ["hello"]:
                return self._hello(svc, node, body)
            if method == "POST" and parts == ["join"]:
                return self._join(svc, node, body)
            if method == "POST" and parts == ["leave"]:
                svc.m.net_member_left(node)
                return self._json(200, {"ok": True})
            if method == "POST" and parts == ["tasks"]:
                return self._json(201, svc.m.net_accept_task(node, body))
            if len(parts) >= 2 and parts[0] == "tasks" and parts[1].isdigit():
                tid = int(parts[1])
                rest = parts[2:]
                if method == "GET" and not rest:
                    return self._json(200, svc.m.net_hosted_status(node, tid))
                if method == "GET" and rest == ["log"]:
                    if q.get("all"):
                        return self._send(200, svc.m.net_hosted_log_all(node, tid), "text/plain; charset=utf-8")
                    return self._json(200, svc.m.net_hosted_log(node, tid, int(q.get("offset", 0)),
                                                                int(q.get("limit", 65536))))
                if method == "POST" and rest == ["cancel"]:
                    return self._json(200, svc.m.net_hosted_cancel(node, tid))
                if method == "GET" and rest == ["results"]:
                    return self._file(svc.m.net_hosted_results(node, tid))
                if method == "DELETE" and not rest:
                    return self._json(200, svc.m.net_hosted_delete(node, tid))
            raise NetError("нет такого адреса", 404)
        except NetError as e:
            self._json(e.status, {"error": e.message})
        except Exception:
            log.exception("network request %s %s", method, self.path)
            self._json(500, {"error": "внутренняя ошибка ПК-хозяина"})

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_JSON:
            raise NetError("слишком большой запрос", 413)
        raw = self.rfile.read(n) if n else b""
        if body_hash(raw) != self.headers.get("X-RMQ-Body"):
            raise NetError("тело запроса не совпадает с подписью", 401)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise NetError("тело запроса — не JSON") from None
        if not isinstance(data, dict):
            raise NetError("тело запроса должно быть объектом")
        return data

    def _hello(self, svc: NetService, node: str, body: dict) -> None:
        info = body.get("info") or {}
        if info.get("node_id") != node:
            raise NetError("узел представился чужим именем", 401)
        svc.store.merge(body.get("members") or {})
        if node not in svc.store.members and info.get("address"):  # joined through another member
            svc.store.merge({node: {"name": info.get("name"), "address": info.get("address"),
                                    "joined_at": time.time(), "updated_at": time.time(), "left_at": None}})
        svc._note_peer(info)
        self._json(200, {"info": svc.self_info(), "members": svc.store.members})

    def _join(self, svc: NetService, node: str, body: dict) -> None:
        info, rec = body.get("info") or {}, body.get("record") or {}
        if info.get("node_id") != node or not info.get("address"):
            raise NetError("неполная заявка на вступление")
        st = svc.store
        with st._lock:
            st.members[node] = {"name": str(info.get("name") or node)[:40], "address": info["address"],
                                "joined_at": time.time(), "updated_at": time.time(), "left_at": None}
            st.save()
        svc._note_peer(info)
        svc.m.db.event("net", f"К сети присоединился ПК {info.get('name') or node}")
        svc.poke()
        self._json(200, {"network": {"id": st.network["id"], "name": st.network["name"],
                                     "created_by": st.network.get("created_by")},
                         "members": st.members, "info": svc.self_info()})

    def _upload(self, svc: NetService, node: str, upload_id: str) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        with svc._lock:
            slot = svc.uploads.get(upload_id)
        if not slot or slot["node"] != node:
            raise NetError("нет такой отправки (она устарела): начните заново", 404)
        if n != slot["size"]:
            raise NetError("размер архива не совпадает с заявленным")
        target = svc.m.net_dir("in") / f"{upload_id}.zip"
        h = hashlib.sha256()
        left = n
        with open(target, "wb") as f:
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                left -= len(chunk)
        try:
            if left or h.hexdigest() != self.headers.get("X-RMQ-Body") or h.hexdigest() != slot["sha256"]:
                raise NetError("архив пришёл повреждённым: отправьте заново")
            with svc._lock:
                svc.uploads.pop(upload_id, None)
            tid = svc.m.net_finish_upload(node, upload_id, slot, target)
        finally:
            target.unlink(missing_ok=True)
        self._json(200, {"task_id": tid})

    def _file(self, path: Path) -> None:
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)


def file_hash(path: Path) -> str:
    return file_sha256(path)
