"""The compute network: PCs running RMQ lend each other their spare capacity.

A PC can belong to two networks at once:

  "Мои ПК" (home)          the owner's own PCs, with their own key that only these PCs know. One of them is the
                           main PC: it adds the others, manages their settings, and decides which of them other
                           people may see. Tasks between one's own PCs run like local ones: normal priority, full
                           weight, no guest restrictions.
  "Сеть с друзьями" (ext)  PCs of several people, with a key they share. Only the PCs their owners let in are
                           there; a PC left out is not visible to other people at all.

Transport: plain HTTP between managers over Tailscale (a free encrypted private network that works through
any router). The listener binds to this PC's Tailscale address only, so nothing listens on the internet or on
the local network. Every request is signed with the key of the network it is for (HMAC-SHA256 over the method,
path, time, a one-time nonce and the body hash); unsigned or replayed requests are refused. So a member of the
network with other people can never reach the routes only the owner's own PCs may use (settings, management).

Modes of this PC (for the network with other people; "offline" switches every connection off):
  two_way  - my agents may use other people's PCs, and their tasks may run here (low priority, write-protected)
  one_way  - my agents may use other people's PCs; their tasks here are frozen
  offline  - no network at all; the manager works on its own, as before

Membership: a PC creates a network and hands out invite codes (its address + the key + which network). A joiner
registers with the inviter and gets the member list; then every node says hello to every other one every 30 s,
exchanging its live state (free resources, mode, version) and the member list (newest record wins); in "Мои
ПК" also which PC is the main one.

Tasks for another PC ("proxy" tasks here, "hosted" tasks there): the origin packs the project folder, sends the
task and the archive over the network the host shares with it (one's own PCs first), follows its state, and
when it ends brings back the files the task created or changed, then tells the host to delete its copy.
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
from urllib.parse import parse_qs, urlencode, urlparse

from . import __version__, firewall
from .netbase import (KINDS, MODES, PROTOCOL, NetError, NetStore, NonceCache, body_hash, collect_files, file_sha256,
                      make_invite, make_zip, member_record, parse_invite, safe_extract, sign_headers,
                      tailscale_address, verify)

log = logging.getLogger("rmq")

# peers are reached directly over Tailscale: never through a system proxy (and no reverse-DNS lookups that
# urllib does to decide whether to bypass one, which cost up to seconds per request)
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HELLO_EVERY_S = 30
HOME_HELLO_EVERY_S = 10  # one's own PCs: their running tasks show on each other's main page
SYNC_EVERY_S = 3
ONLINE_S = 95          # a member is "in the network" if it answered within this time
REACH_S = 150          # "this PC gets through to that one" if one of its own requests got there within this time
MAX_JSON = 1 << 20
# how the main PC lets one of its PCs into the network with other people ("hidden": not there at all)
EXT_STATES = {"open": "two_way", "use": "one_way"}


def explain(address: str, err) -> str:
    """A connection failure in words: which PC to look at and what usually causes it."""
    code = getattr(err, "winerror", None) or getattr(getattr(err, "reason", None), "winerror", None)
    why = {
        10013: "Windows на этом ПК не дала менеджеру подключиться: исходящие соединения блокирует VPN (функция "
               "«блокировать трафик вне VPN»), антивирус или правило брандмауэра. Отключите VPN на время или добавьте "
               "в нём исключение для Tailscale (адреса 100.64.0.0/10)",
        10060: "тот ПК не ответил: на нём брандмауэр Windows не пускает Python, ПК выключен или не подключён к Tailscale",
        10061: "тот ПК отказал в подключении: менеджер на нём не запущен или в режиме «Офлайн»",
        10065: "до того ПК нет маршрута: проверьте, что Tailscale подключён на обоих ПК",
        10051: "до того ПК нет маршрута: проверьте, что Tailscale подключён на обоих ПК",
    }.get(code)
    detail = getattr(err, "reason", err)
    return f"нет связи с {address}: {why}" if why else f"нет связи с {address}: {detail}"


class PeerError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status
        self.message = message


class NetService:
    """Membership, heartbeats, the listener, the main PC's management, and the life of tasks sent to other PCs."""

    def __init__(self, manager):
        self.m = manager
        self.store: NetStore = manager.netstore
        self.nonces = NonceCache()
        self.live: dict[str, dict] = {}       # node id -> {"seen", "info", "info_by": {kind: info}, "rtt_ms", ...}
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
        self._last_hello: dict[str, float] = {}  # network -> when its members were greeted last
        self._last_sync = 0.0
        self.firewall: dict = {"state": "unknown"}  # can other PCs get through Windows Firewall to the listener
        self._fw_at = 0.0

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
        self._last_hello.clear()
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
        st = self.store
        want = st.mode != "offline" and (st.network is not None or st.home is not None)
        if not want:
            self._stop_server()
            self.status = "offline" if st.mode == "offline" else "no_network"
            return
        self._ensure_server(cfg)
        now = time.time()
        for kind in KINDS if self.server else ():
            every = HOME_HELLO_EVERY_S if kind == "home" else HELLO_EVERY_S
            if not st.net(kind) or now - self._last_hello.get(kind, 0.0) < every:
                continue
            self._last_hello[kind] = now
            for nid, rec in st.active_members(kind).items():
                if rec.get("address"):
                    self.pool.submit(self._hello, nid, rec, kind)
        if now - self._last_sync >= SYNC_EVERY_S:
            self._last_sync = now
            self._sync_proxies()
            self._expire_uploads(now)
        if self.server and now - self._fw_at >= 300:
            self._fw_at = now
            self.pool.submit(self.check_firewall)

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
        self._fw_at = 0.0  # check the firewall right away for the new listener
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
            changed = False
            for kind in KINDS:
                if not st.net(kind):
                    continue
                book = st.book(kind)
                me = book.get(st.node_id)
                if me is None or me.get("address") != address or me.get("name") != st.node_name or me.get("left_at"):
                    joined = me.get("joined_at") if me else time.time()
                    book[st.node_id] = member_record(st.node_name, address, joined)
                    changed = True
            if changed:
                st.save()

    def _my_address(self) -> str:
        cfg = self.m.settings.section("network")
        ip = self.bind_ip(cfg)
        if not ip:
            raise NetError("не найден адрес Tailscale: установите Tailscale, войдите в него и повторите")
        return f"{ip}:{int(cfg['port'])}"

    # ------------------------------------------------------------ Windows Firewall in front of the listener
    @staticmethod
    def _program() -> str:
        """The program that really listens (a venv's pythonw.exe starts the base interpreter's)."""
        import psutil
        return psutil.Process().exe()

    def check_firewall(self) -> dict:
        port = int(self.m.settings.section("network")["port"])
        try:
            result = firewall.check(self._program(), port)
        except Exception as e:
            log.warning("firewall check failed: %s", e)
            result = {"state": "unknown", "category": None}
        result.update(port=port, checked_at=time.time())
        self.firewall = result
        return result

    def allow_firewall(self) -> dict:
        """The owner pressed "Разрешить в брандмауэре": one narrow rule, with Windows asking for consent."""
        port = int(self.m.settings.section("network")["port"])
        category = self.firewall.get("category") or "Private"
        ok, message = firewall.allow(self._program(), port, category)
        self.check_firewall()
        if ok:
            self.m.db.event("net", f"Брандмауэр Windows: разрешены подключения других ПК к менеджеру (порт {port})")
        return {"ok": ok, "message": message, "view": self.view()}

    # ------------------------------------------------------------ what this node tells the others
    def channel(self, nid: str) -> str:
        """The network to reach a PC over: one's own PCs through "Мои ПК", everyone else through the shared one."""
        st = self.store
        return "home" if st.home and nid in st.active_members("home") else "ext"

    def self_info(self, kind: str = "ext") -> dict:
        st = self.m.state or {}
        store = self.store
        sysd = st.get("system") or {}
        cpu, ram, vram = sysd.get("cpu") or {}, sysd.get("ram") or {}, sysd.get("vram") or {}
        free = {"cpu": max(0.0, cpu.get("pool_cores", 0) - cpu.get("held", 0)),
                "ram": max(0.0, ram.get("pool", 0) - ram.get("held", 0)),
                "vram": max(0.0, vram.get("pool", 0) - vram.get("held", 0)) if vram else 0.0}
        counts = dict(st.get("_counts") or {})
        th = st.get("thermal") or {}
        info = {
            "node_id": store.node_id, "name": store.node_name, "address": self.address,
            "version": __version__, "protocol": PROTOCOL, "mode": store.mode,
            # one's own PCs always take each other's tasks; other people's only in two_way
            "accepting": self.server is not None and (kind == "home" or (store.mode == "two_way"
                                                                         and store.network is not None)),
            "hw": self.m.hardware(), "free": {k: round(v, 2) for k, v in free.items()},
            "busy": {"game": bool((st.get("game") or {}).get("active")), "paused": st.get("mode") == "paused",
                     "heat": th.get("state", "normal"),
                     "disk_low": any(d.get("low") for d in (st.get("disk") or {}).get("drives", []))},
            "tasks": counts, "hosted": len(self.m.hosted_active()),
        }
        if kind == "home":  # what the main PC shows and manages; never told to other people
            from .settings import PRESETS, active_preset
            preset = active_preset(self.m.settings.snapshot())
            info["home"] = {"main": store.is_main, "preset": PRESETS[preset][0] if preset else None,
                            "external": {"in": store.network is not None, "mode": store.mode,
                                         "network": store.network["name"] if store.network else None}}
            info["running"] = self.m.running_brief()
            up = getattr(self.m, "updater", None)
            if up is not None:
                info["home"]["update"] = {k: up.status.get(k) for k in ("state", "latest", "checked_at")}
        return info

    def _home_meta(self) -> dict | None:
        h = self.store.home
        return {"main": h.get("main"), "main_at": h.get("main_at"), "name": h.get("name")} if h else None

    # ------------------------------------------------------------ talking to a peer
    def _request(self, address: str, method: str, path: str, *, kind: str = "ext", body: bytes | None = None,
                 data_file: Path | None = None, bhash: str | None = None, timeout: float = 15,
                 out_file: Path | None = None) -> bytes | dict:
        st = self.store
        net = st.net(kind)
        if not net:
            raise PeerError("этот ПК не в этой сети")
        if data_file is not None:
            size = data_file.stat().st_size
            payload = open(data_file, "rb")
        else:
            payload = body or b""
            size = len(payload)
            bhash = body_hash(payload)
        headers = sign_headers(st.key_of(kind), net["id"], st.node_id, method, path, bhash)
        headers["Content-Length"] = str(size)
        if body is not None or data_file is not None:
            headers["Content-Type"] = "application/octet-stream" if data_file is not None else "application/json"
        req = urllib.request.Request(f"http://{address}{path}", data=payload if size or method in ("POST", "PUT", "PATCH") else None,
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
            raise PeerError(explain(address, e)) from None
        finally:
            if data_file is not None:
                payload.close()
        if r.headers.get("Content-Type", "").startswith("application/json"):
            return json.loads(raw.decode("utf-8"))
        return raw

    def call(self, node_id: str, method: str, path: str, payload: dict | None = None, kind: str | None = None, **kw):
        kind = kind or self.channel(node_id)
        rec = self.store.book(kind).get(node_id)
        if not rec or not rec.get("address"):
            raise PeerError("адрес этого ПК неизвестен")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        return self._request(rec["address"], method, path, kind=kind, body=body, **kw)

    def _hello(self, nid: str, rec: dict, kind: str) -> None:
        st = self.store
        t0 = time.monotonic()
        with self._lock:
            self.live.setdefault(nid, {})["out_try"] = time.time()
        body = {"info": self.self_info(kind), "members": st.book(kind)}
        if kind == "home":
            body["home"] = self._home_meta()
        try:
            r = self._request(rec["address"], "POST", "/net/v1/hello", kind=kind,
                              body=json.dumps(body, ensure_ascii=False).encode("utf-8"), timeout=10)
        except PeerError as e:
            with self._lock:
                self.live.setdefault(nid, {})["error"] = e.message
            return
        rtt = (time.monotonic() - t0) * 1000
        self._note_peer(r.get("info") or {}, rtt, kind)
        with self._lock:  # whether that PC gets through to this one (None: an older version that does not say)
            self.live.setdefault(nid, {})["they_reach"] = r.get("reached")
        changed = st.merge(r.get("members") or {}, kind)
        if kind == "home":
            changed = st.merge_home_meta(r.get("home")) or changed
        if changed:
            self.poke()

    def _note_peer(self, info: dict, rtt: float | None = None, kind: str = "ext") -> None:
        nid = info.get("node_id")
        if not nid or nid == self.store.node_id:
            return
        with self._lock:
            cur = self.live.setdefault(nid, {})
            cur.update(seen=time.time(), info=info)
            cur.setdefault("info_by", {})[kind] = info
            if rtt is not None:  # this PC's own request got through (its hellos to us say nothing about that)
                cur.update(rtt_ms=round(rtt), error="", out_ok=time.time())

    def info_of(self, nid: str, kind: str | None = None) -> dict:
        with self._lock:
            lv = self.live.get(nid) or {}
            return dict((lv.get("info_by") or {}).get(kind or self.channel(nid)) or lv.get("info") or {})

    def online(self, nid: str) -> bool:
        with self._lock:
            seen = self.live.get(nid, {}).get("seen", 0)
        return time.time() - seen <= ONLINE_S

    def reaches(self, nid: str) -> bool | None:
        """Whether this PC's requests get through to that one lately; None while it has not tried.

        A PC can hear from a member and still be unable to reach it: the member's firewall may block incoming
        connections, or in Tailscale it may be shared only one way. Each side tells the other in its hello answer,
        so the dashboard can say on which PC the problem is."""
        with self._lock:
            lv = dict(self.live.get(nid, {}))
        now = time.time()
        if now - lv.get("out_ok", 0) <= REACH_S:
            return True
        if now - lv.get("out_try", 0) <= REACH_S:
            return False
        return None

    # ------------------------------------------------------------ the network with other people
    def create(self, name: str) -> dict:
        st = self.store
        if st.network:
            raise NetError("этот ПК уже в сети с друзьями: сначала выйдите из неё")
        name = (name or "").strip()[:60] or f"Сеть {st.node_name}"
        with st._lock:
            st.network = {"id": "net-" + secrets.token_hex(6), "name": name,
                          "key": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
                          "created_by": st.node_id, "created_at": time.time()}
            st.members = {st.node_id: member_record(st.node_name, self.address, time.time())}
            if st.mode == "offline":
                st.mode = "two_way"
            st.save()
        self.m.db.event("net", f"Создана сеть с друзьями «{name}»")
        self.poke()
        return self.view()

    def invite(self) -> str:
        st = self.store
        if not st.network:
            raise NetError("сначала создайте сеть с друзьями или присоединитесь к ней")
        if not self.address:
            raise NetError("этот ПК сейчас не на связи: нужен запущенный Tailscale и режим не «офлайн»")
        return make_invite(st.network, self.address, st.node_name, kind="ext")

    def join(self, code: str) -> dict:
        """A code from another PC: into "Мои ПК" or into the network with other people, as the code says."""
        inv = parse_invite(code)
        return self._home_join(inv) if inv["t"] == "home" else self._ext_join(inv)

    def _ext_join(self, inv: dict, mode: str | None = None) -> dict:
        st = self.store
        if st.network and st.network["id"] != inv["n"]:
            raise NetError("этот ПК уже в другой сети с друзьями: сначала выйдите из неё")
        my_address = self._my_address()
        with st._lock:
            st.network = {"id": inv["n"], "name": inv.get("name") or "Сеть", "key": inv["k"],
                          "created_by": None, "created_at": time.time()}
            if mode:
                st.mode = mode
            elif st.mode == "offline":
                st.mode = "two_way"
            st.members[st.node_id] = member_record(st.node_name, my_address, time.time())
        try:
            r = self._request(inv["a"], "POST", "/net/v1/join", kind="ext",
                              body=json.dumps({"info": self.self_info("ext") | {"address": my_address},
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
        st.merge(r.get("members") or {}, "ext")
        self._note_peer(r.get("info") or {}, None, "ext")
        self.m.db.event("net", f"ПК присоединился к сети с друзьями «{st.network['name']}» "
                               f"(пригласил {inv.get('by', '?')})")
        self.poke()
        return self.view()

    def leave(self) -> dict:
        """Leave the network with other people; "Мои ПК" stay as they are."""
        st = self.store
        if not st.network:
            raise NetError("этот ПК не в сети с друзьями")
        name = st.network["name"]
        self.m.net_cancel_all("этот ПК вышел из сети с друзьями", "ext")
        for nid, rec in st.active_members("ext").items():
            if rec.get("address"):
                try:
                    self._request(rec["address"], "POST", "/net/v1/leave", kind="ext", body=b"{}", timeout=5)
                except PeerError:
                    pass
        with st._lock:
            own = set(st.home_members) if st.home else set()
            st.network, st.members = None, {}
            st.save()
        with self._lock:
            for nid in [n for n in self.live if n not in own]:
                del self.live[nid]
            for lv in self.live.values():
                (lv.get("info_by") or {}).pop("ext", None)
        if not st.home:
            self._stop_server()
        self.m.db.event("net", f"ПК вышел из сети с друзьями «{name}»")
        return self.view()

    def set_mode(self, mode: str) -> dict:
        if mode not in MODES:
            raise NetError("режим: two_way, one_way или offline")
        st = self.store
        with st._lock:
            st.mode = mode
            st.save()
        self.m.db.event("net", {"two_way": "Сеть: ПК открыт для друзей — их задачи могут выполняться здесь",
                                "one_way": "Сеть: только для себя — задачи друзей здесь заморожены",
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

    # ------------------------------------------------------------ "Мои ПК": the owner's own PCs
    def home_create(self, name: str) -> dict:
        st = self.store
        if st.home:
            raise NetError("этот ПК уже в «Моих ПК»")
        name = (name or "").strip()[:60] or "Мои ПК"
        now = time.time()
        with st._lock:
            st.home = {"id": "home-" + secrets.token_hex(6), "name": name,
                       "key": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
                       "main": st.node_id, "main_at": now, "created_at": now}
            st.home_members = {st.node_id: member_record(st.node_name, self.address, now)}
            if st.mode == "offline":
                st.mode = "two_way"
            st.save()
        self.m.db.event("net", f"Созданы «Мои ПК» «{name}»; этот ПК основной")
        self.poke()
        return self.view()

    def home_invite(self) -> str:
        st = self.store
        if not st.home:
            raise NetError("сначала создайте «Мои ПК» на основном ПК")
        if not st.is_main:
            raise NetError("добавлять ПК в «Мои ПК» можно только с основного ПК")
        if not self.address:
            raise NetError("этот ПК сейчас не на связи: нужен запущенный Tailscale и режим не «офлайн»")
        return make_invite(st.home, self.address, st.node_name, kind="home")

    def _home_join(self, inv: dict) -> dict:
        st = self.store
        if st.home and st.home["id"] != inv["n"]:
            raise NetError("этот ПК уже в других «Моих ПК»: сначала выйдите из них")
        my_address = self._my_address()
        with st._lock:
            st.home = {"id": inv["n"], "name": inv.get("name") or "Мои ПК", "key": inv["k"],
                       "main": None, "main_at": 0.0, "created_at": time.time()}
            st.home_members[st.node_id] = member_record(st.node_name, my_address, time.time())
            if st.mode == "offline":
                st.mode = "two_way"
        try:
            r = self._request(inv["a"], "POST", "/net/v1/join", kind="home",
                              body=json.dumps({"info": self.self_info("home") | {"address": my_address},
                                               "record": st.home_members[st.node_id]},
                                              ensure_ascii=False).encode("utf-8"), timeout=20)
        except PeerError as e:
            with st._lock:
                st.home, st.home_members = None, {}
                st.save()
            raise NetError(f"не удалось присоединиться: {e.message}") from None
        st.merge(r.get("members") or {}, "home")
        st.merge_home_meta(r.get("home"))
        with st._lock:
            st.save()
        self._note_peer(r.get("info") or {}, None, "home")
        self.m.db.event("net", f"ПК присоединился к «Моим ПК» «{st.home['name']}» (добавил {inv.get('by', '?')})")
        self.poke()
        return self.view()

    def home_leave(self) -> dict:
        st = self.store
        if not st.home:
            raise NetError("этот ПК не в «Моих ПК»")
        name = st.home["name"]
        self.m.net_cancel_all("этот ПК вышел из «Моих ПК»", "home")
        for nid, rec in st.active_members("home").items():
            if rec.get("address"):
                try:
                    self._request(rec["address"], "POST", "/net/v1/leave", kind="home", body=b"{}", timeout=5)
                except PeerError:
                    pass
        with st._lock:
            ext = set(st.members) if st.network else set()
            st.home, st.home_members = None, {}
            st.save()
        with self._lock:
            for nid in [n for n in self.live if n not in ext]:
                del self.live[nid]
            for lv in self.live.values():
                (lv.get("info_by") or {}).pop("home", None)
        if not st.network:
            self._stop_server()
        self.m.db.event("net", f"ПК вышел из «Моих ПК» «{name}»")
        return self.view()

    def find_home(self, who: str) -> str:
        """A PC of "Мои ПК" by its id or name ("" or this PC's own name means this PC)."""
        st = self.store
        key = (who or "").strip().lower()
        if not key or key in (st.node_id.lower(), st.node_name.lower()):
            return st.node_id
        for nid, rec in st.active_members("home").items():
            if nid.lower() == key or str(rec.get("name") or "").lower() == key:
                return nid
        raise NetError(f"среди моих ПК нет «{who}»", 404)

    def make_main(self, nid: str) -> dict:
        """The main PC hands its role to another one; any own PC may take it itself (if the main one is gone)."""
        st = self.store
        if not st.home:
            raise NetError("этот ПК не в «Моих ПК»")
        if nid != st.node_id and not st.is_main:
            raise NetError("назначить основным другой ПК можно только с основного ПК", 403)
        with st._lock:
            st.home.update(main=nid, main_at=time.time())
            st.save()
        self.m.db.event("net", f"Основной ПК в «Моих ПК»: {self.node_name(nid) if nid != st.node_id else st.node_name}")
        self.poke()
        return self.view()

    def set_external(self, nid: str, state: str) -> dict:
        """Which of my PCs other people see: "open" (their tasks run there), "use" (it only sends its own tasks
        out), "hidden" (not in that network at all). The main PC decides for all of them."""
        st = self.store
        if state not in ("open", "use", "hidden"):
            raise NetError("состояние: open, use или hidden")
        if nid == st.node_id:
            self.apply_external(state)
            return self.view()
        if not st.is_main:
            raise NetError("это решает основной ПК", 403)
        if nid not in st.active_members("home"):
            raise NetError("нет такого ПК среди моих ПК", 404)
        if state != "hidden" and not st.network:
            raise NetError("основной ПК сам не в сети с друзьями: сначала создайте её или присоединитесь к ней")
        payload: dict = {"state": state}
        if state != "hidden":
            payload.update(network={k: st.network.get(k) for k in ("id", "name", "key", "created_by")},
                           via=self.address)
        try:
            self.call(nid, "POST", "/net/v1/home/external", payload, kind="home", timeout=40)
        except PeerError as e:
            raise NetError(f"ПК {self.node_name(nid)}: {e.message}", e.status or 502) from None
        self.poke()
        return self.view()

    def apply_external(self, state: str, network: dict | None = None, via: str | None = None) -> None:
        st = self.store
        if state not in ("open", "use", "hidden"):
            raise NetError("состояние: open, use или hidden")
        if state == "hidden":
            if st.network:
                self.leave()
            return
        mode = EXT_STATES[state]
        if st.network is None:
            if not (isinstance(network, dict) and network.get("id") and network.get("key") and via):
                raise NetError("этот ПК не в сети с друзьями, а основной ПК не передал её данные")
            self._ext_join({"n": network["id"], "name": network.get("name"), "k": network["key"], "a": via,
                            "by": "основной ПК"}, mode)
        elif network and network.get("id") != st.network["id"]:
            raise NetError("этот ПК уже в другой сети с друзьями")
        elif st.mode != mode:
            self.set_mode(mode)

    def manage(self, nid: str, method: str, path: str, payload: dict | None = None):
        """The main PC acts on one of its PCs: settings, presets, the name."""
        st = self.store
        if not st.is_main:
            raise NetError("управлять другими ПК можно только с основного ПК", 403)
        if nid not in st.active_members("home"):
            raise NetError("нет такого ПК среди моих ПК", 404)
        try:
            return self.call(nid, method, f"/net/v1/home/{path}", payload, kind="home", timeout=30)
        except PeerError as e:
            raise NetError(e.message, e.status or 502) from None

    # ------------------------------------------------------------ looking at one's other PCs
    def home_running(self) -> list[dict]:
        """What runs right now on each of one's other PCs, as its last hello said (they come every few seconds)."""
        st = self.store
        if not st.home:
            return []
        with self._lock:
            live = {k: dict(v) for k, v in self.live.items()}
        now = time.time()
        out = []
        for nid, rec in st.active_members("home").items():
            lv = live.get(nid) or {}
            info = (lv.get("info_by") or {}).get("home") or {}
            fresh = now - lv.get("seen", 0) <= 3.5 * HOME_HELLO_EVERY_S
            tasks = [dict(t, here=t.get("origin_node") == st.node_id) for t in info.get("running") or []] if fresh else []
            out.append({"node_id": nid, "name": rec.get("name") or info.get("name") or nid, "online": fresh,
                        "at": lv.get("seen"), "known": "running" in info, "tasks": tasks})
        return out

    def update_all(self) -> dict:
        """The main PC asks each of its PCs to look for an update right now (each one installs it itself)."""
        st = self.store
        if not st.is_main:
            raise NetError("обновлять другие ПК можно только с основного ПК", 403)
        asked, failed = [], []
        for nid, rec in st.active_members("home").items():
            name = rec.get("name") or nid
            if not self.online(nid):
                failed.append(f"{name}: не в сети")
                continue
            try:
                self.call(nid, "POST", "/net/v1/home/update", {}, kind="home", timeout=15)
                asked.append(name)
            except PeerError as e:
                failed.append(f"{name}: " + ("менеджер старой версии — обновится сам, в течение нескольких часов"
                                             if e.status == 404 else e.message))
        self.m.check_updates_now()
        return {"asked": asked, "failed": failed}

    def peek(self, nid: str, what: str, query: dict | None = None):
        """Read one of one's other PCs: its dashboard state or its charts. Any of one's PCs may look; only the
        main PC manages."""
        st = self.store
        if not st.home or nid not in st.active_members("home"):
            raise NetError("нет такого ПК среди моих ПК", 404)
        path = f"/net/v1/home/{what}" + (f"?{urlencode(query)}" if query else "")
        try:
            return self.call(nid, "GET", path, kind="home", timeout=20)
        except PeerError as e:
            if e.status == 404:
                raise NetError("на том ПК менеджер старой версии: обновите его, и здесь появятся его задачи "
                               "и графики", 409) from None
            raise NetError(e.message, e.status or 502) from None

    # ------------------------------------------------------------ what the "Сеть" tab shows
    def _member_views(self, kind: str, live: dict) -> list[dict]:
        st = self.store
        out = []
        main = (st.home or {}).get("main") if kind == "home" else None
        own = set(st.home_members) if st.home else set()
        book = st.book(kind)
        for nid, rec in sorted(book.items(), key=lambda kv: (kv[0] != st.node_id, kv[1].get("joined_at") or 0)):
            if nid == st.node_id:
                d = {"node_id": nid, "name": st.node_name, "self": True, "online": self.server is not None,
                     "status": self.status, "info": self.self_info(kind), "address": self.address, "left": False,
                     "joined_at": rec.get("joined_at")}
            else:
                lv = live.get(nid, {})
                d = {"node_id": nid, "name": rec.get("name"), "self": False, "address": rec.get("address"),
                     "left": bool(rec.get("left_at")), "left_at": rec.get("left_at"),
                     "online": not rec.get("left_at") and time.time() - lv.get("seen", 0) <= ONLINE_S,
                     "seen": lv.get("seen"), "rtt_ms": lv.get("rtt_ms"), "error": lv.get("error"),
                     "reaches": self.reaches(nid), "they_reach": lv.get("they_reach"),
                     "info": (lv.get("info_by") or {}).get(kind) or lv.get("info"), "joined_at": rec.get("joined_at")}
            if kind == "home":
                d["main"] = nid == main
            else:
                d["own"] = nid in own
            out.append(d)
        return out

    def view(self) -> dict:
        st = self.store
        with self._lock:
            live = {k: dict(v) for k, v in self.live.items()}
        net, home = st.network, st.home
        main = (home or {}).get("main")
        return {"node_id": st.node_id, "node_name": st.node_name, "mode": st.mode, "status": self.status,
                "error": self.error, "address": self.address, "tailscale": tailscale_address(),
                "firewall": self.firewall if self.server else None,
                "home": {"id": home["id"], "name": home["name"], "main": main, "is_main": st.is_main,
                         "main_name": (st.node_name if main == st.node_id else self.node_name(main)) if main else None,
                         "main_left": bool(main and main != st.node_id
                                           and main not in st.active_members("home")),
                         "members": self._member_views("home", live)} if home else None,
                "network": {"id": net["id"], "name": net["name"], "created_by": net.get("created_by"),
                            "created_at": net.get("created_at")} if net else None,
                "members": self._member_views("ext", live) if net else []}

    # ------------------------------------------------------------ tasks sent to other PCs (origin side)
    def candidates(self) -> list[tuple[str, str]]:
        """(node, network) of every PC a task may go to: one's own PCs first, then other people's."""
        st = self.store
        out = [(nid, "home") for nid in st.active_members("home")] if st.home else []
        if st.network and st.mode != "offline":
            mine = {nid for nid, _ in out}
            out += [(nid, "ext") for nid in st.active_members("ext") if nid not in mine]
        return out

    def best_node(self, need: dict) -> str | None:
        """The online PC that can start this task right now: one's own PCs first, then the most CPU to spare."""
        best, best_key = None, None
        with self._lock:
            live = {k: dict(v) for k, v in self.live.items()}
        for nid, kind in self.candidates():
            lv = live.get(nid, {})
            info = (lv.get("info_by") or {}).get(kind) or {}
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
            if cpu < min(need["cpu"], 1.0):
                continue
            key = (kind == "home", cpu)
            if best_key is None or key > best_key:
                best, best_key = nid, key
        return best

    def node_name(self, nid: str | None) -> str:
        if not nid:
            return "?"
        st = self.store
        rec = st.home_members.get(nid) or st.members.get(nid) or {}
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
        kind = spec.get("net") or self.channel(node)
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
            r = self.call(node, "POST", "/net/v1/tasks", payload, kind=kind, timeout=30)
            m.net_remote_update(tid, {"phase_text": f"отправляем {size / 2**20:.1f} МБ"})
            up = self.call(node, "PUT", f"/net/v1/uploads/{r['upload_id']}", None, kind=kind,
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
        kind = json.loads(row["net_spec"] or "{}").get("net") or self.channel(node)
        rec = self.store.book(kind).get(node)
        if not rec or rec.get("left_at"):
            m.net_proxy_failed(tid, f"ПК {self.node_name(node)} вышел из сети")
            return
        try:
            st = self.call(node, "GET", f"/net/v1/tasks/{rid}", kind=kind, timeout=10)
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
            self.call(node, "GET", f"/net/v1/tasks/{rid}/log?all=1", kind=kind, timeout=60, out_file=tdir / "output.log")
            res = m.net_dir("res") / f"results-{tid}.zip"
            self.call(node, "GET", f"/net/v1/tasks/{rid}/results", kind=kind, timeout=600, out_file=res)
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
            self.call(node, "DELETE", f"/net/v1/tasks/{rid}", kind=kind, timeout=10)
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

    def do_PATCH(self):
        self._dispatch("PATCH")

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
            kind = st.kind_of(self.headers.get("X-RMQ-Net"))
            if kind is None:
                raise NetError("этот ПК не в этой сети", 403)
            node = verify(self.headers, st.key_of(kind), st.net(kind)["id"], method, self.path, svc.nonces)
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            if parts[:2] != ["net", "v1"]:
                raise NetError("нет такого адреса", 404)
            parts = parts[2:]
            rec = st.book(kind).get(node)
            if rec and rec.get("left_at") and parts != ["join"]:
                raise NetError("этот ПК вышел из сети: присоединитесь заново по приглашению", 403)
            if method == "PUT" and len(parts) == 2 and parts[0] == "uploads":
                return self._upload(svc, node, parts[1], kind)
            body = self._read_json()
            if method == "POST" and parts == ["hello"]:
                return self._hello(svc, node, body, kind)
            if method == "POST" and parts == ["join"]:
                return self._join(svc, node, body, kind)
            if method == "POST" and parts == ["leave"]:
                svc.m.net_member_left(node, kind)
                return self._json(200, {"ok": True})
            if method == "POST" and parts == ["tasks"]:
                return self._json(201, svc.m.net_accept_task(node, body, kind))
            if len(parts) >= 2 and parts[0] == "tasks" and parts[1].isdigit():
                tid = int(parts[1])
                rest = parts[2:]
                if method == "GET" and not rest:
                    return self._json(200, svc.m.net_hosted_status(node, tid, kind))
                if method == "GET" and rest == ["log"]:
                    if q.get("all"):
                        return self._send(200, svc.m.net_hosted_log_all(node, tid, kind), "text/plain; charset=utf-8")
                    return self._json(200, svc.m.net_hosted_log(node, tid, int(q.get("offset", 0)),
                                                                int(q.get("limit", 65536)), kind))
                if method == "POST" and rest == ["cancel"]:
                    return self._json(200, svc.m.net_hosted_cancel(node, tid, kind))
                if method == "GET" and rest == ["results"]:
                    return self._file(svc.m.net_hosted_results(node, tid, kind))
                if method == "DELETE" and not rest:
                    return self._json(200, svc.m.net_hosted_delete(node, tid, kind))
            if parts and parts[0] == "home":
                return self._home(svc, node, method, parts[1:], body, kind, q)
            raise NetError("нет такого адреса", 404)
        except NetError as e:
            self._json(e.status, {"error": e.message})
        except Exception as e:
            status, message = getattr(e, "status", None), getattr(e, "message", None)
            if isinstance(status, int) and isinstance(message, str):  # the manager's own errors (bad settings and such)
                return self._json(status, {"error": message})
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

    def _hello(self, svc: NetService, node: str, body: dict, kind: str) -> None:
        info = body.get("info") or {}
        if info.get("node_id") != node:
            raise NetError("узел представился чужим именем", 401)
        st = svc.store
        st.merge(body.get("members") or {}, kind)
        if kind == "home":
            st.merge_home_meta(body.get("home"))
        if node not in st.book(kind) and info.get("address"):  # joined through another member
            st.merge({node: {"name": info.get("name"), "address": info.get("address"),
                             "joined_at": time.time(), "updated_at": time.time(), "left_at": None}}, kind)
        svc._note_peer(info, None, kind)
        out = {"info": svc.self_info(kind), "members": st.book(kind), "reached": svc.reaches(node)}
        if kind == "home":
            out["home"] = svc._home_meta()
        self._json(200, out)

    def _join(self, svc: NetService, node: str, body: dict, kind: str) -> None:
        info = body.get("info") or {}
        if info.get("node_id") != node or not info.get("address"):
            raise NetError("неполная заявка на вступление")
        st = svc.store
        with st._lock:
            st.book(kind)[node] = {"name": str(info.get("name") or node)[:40], "address": info["address"],
                                   "joined_at": time.time(), "updated_at": time.time(), "left_at": None}
            st.save()
        svc._note_peer(info, None, kind)
        who = info.get("name") or node
        svc.m.db.event("net", f"К «Моим ПК» присоединился ПК {who}" if kind == "home"
                       else f"К сети с друзьями присоединился ПК {who}")
        svc.poke()
        net = st.net(kind)
        out = {"network": {"id": net["id"], "name": net["name"], "created_by": net.get("created_by")},
               "members": st.book(kind), "info": svc.self_info(kind)}
        if kind == "home":
            out["home"] = svc._home_meta()
        self._json(200, out)

    def _home(self, svc: NetService, node: str, method: str, rest: list[str], body: dict, kind: str,
              q: dict) -> None:
        """One's other PCs look at this one, and the main PC manages it. Only over "Мои ПК" (other people do not
        have that key)."""
        st = svc.store
        if kind != "home":
            raise NetError("нет такого адреса", 404)
        m = svc.m
        if method == "GET" and rest == ["state"]:
            return self._json(200, m.home_state())
        if method == "GET" and rest == ["metrics"]:
            try:
                rng, since = float(q.get("range", 900)), float(q.get("since", 0))
            except ValueError:
                raise NetError("range/since: числа") from None
            return self._json(200, m.metrics_view(rng, since))
        if method == "GET" and rest == ["stats"]:
            return self._json(200, m.get_stats(str(q.get("period") or "7d")))
        if method == "GET" and rest == ["chronicle"]:
            try:
                days = float(q.get("days", 7))
            except ValueError:
                raise NetError("days: число") from None
            return self._json(200, m.get_chronicle(max(0.04, min(30.0, days))))
        task = int(rest[1]) if len(rest) >= 2 and rest[0] == "tasks" and rest[1].isdigit() else None
        if method == "GET" and task is not None and len(rest) == 2:
            return self._json(200, m.get_task(m.owner(), task))
        if method == "GET" and task is not None and rest[2:] == ["log"]:
            try:
                offset, limit = int(q.get("offset", 0)), int(q.get("limit", 65536))
            except ValueError:
                raise NetError("offset/limit: целые числа") from None
            return self._json(200, m.read_log(m.owner(), task, offset, limit))
        if node != (st.home or {}).get("main"):
            raise NetError("управлять этим ПК может только основной ПК", 403)
        if method == "POST" and task is not None and len(rest) == 3 and rest[2] in ("suspend", "resume", "cancel"):
            act = {"suspend": m.suspend_task, "resume": m.resume_task, "cancel": m.cancel}[rest[2]]
            return self._json(200, act(m.owner(), task))
        if rest == ["update"] and method == "POST":
            return self._json(200, m.check_updates_now())
        if rest == ["settings"] and method == "GET":
            return self._json(200, m.settings_view())
        if rest == ["settings"] and method == "PATCH":
            return self._json(200, m.update_settings(body))
        if rest == ["preset"] and method == "POST":
            return self._json(200, m.apply_preset(str(body.get("preset") or "")))
        if rest == ["reset"] and method == "POST":
            return self._json(200, m.reset_settings())
        if rest == ["rename"] and method == "POST":
            svc.rename(str(body.get("name") or ""))
            return self._json(200, {"ok": True})
        if rest == ["external"] and method == "POST":
            svc.apply_external(str(body.get("state") or ""), body.get("network"), body.get("via"))
            return self._json(200, {"ok": True})
        raise NetError("нет такого адреса", 404)

    def _upload(self, svc: NetService, node: str, upload_id: str, kind: str) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        with svc._lock:
            slot = svc.uploads.get(upload_id)
        if not slot or slot["node"] != node or slot.get("kind", "ext") != kind:
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
