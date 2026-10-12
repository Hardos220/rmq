"""Building blocks of the compute network that need no manager: identity, the network file, request signing,
invite codes, the Tailscale address, and packing a project for another PC (and its results back)."""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

PROTOCOL = 1
MODES = ("two_way", "one_way", "offline")
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
CLOCK_SKEW_S = 300
# folders never worth sending to another PC
SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "env", ".env", "node_modules", "__pycache__", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", ".ipynb_checkpoints", ".idea", ".vscode", ".tox", "dist", "build",
             ".rmq"}
# what a task leaves behind that is not a result
RESULT_SKIP = {".rmq", "__pycache__", ".ipynb_checkpoints"}


class NetError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- the network file (data/network.json)
def computer_name() -> str:
    return (os.environ.get("COMPUTERNAME") or socket.gethostname() or "PC")[:40]


class NetStore:
    """This node's identity, the chosen mode, and the network it belongs to (with the members it knows)."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        data = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self.node_id: str = data.get("node_id") or "n-" + secrets.token_hex(6)
        self.node_name: str = data.get("node_name") or computer_name()
        self.mode: str = data.get("mode") if data.get("mode") in MODES else "offline"
        self.network: dict | None = data.get("network")
        self.members: dict[str, dict] = data.get("members") or {}
        if not path.exists():
            self.save()

    @property
    def key(self) -> bytes | None:
        return base64.urlsafe_b64decode(self.network["key"]) if self.network else None

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"node_id": self.node_id, "node_name": self.node_name, "mode": self.mode,
                                       "network": self.network, "members": self.members},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def active_members(self) -> dict[str, dict]:
        with self._lock:
            return {nid: dict(m) for nid, m in self.members.items() if not m.get("left_at") and nid != self.node_id}

    def merge(self, records: dict[str, dict]) -> bool:
        """Fold another node's view of the membership into ours: the newest record of each node wins."""
        changed = False
        with self._lock:
            for nid, rec in records.items():
                if nid == self.node_id or not isinstance(rec, dict):
                    continue
                mine = self.members.get(nid)
                if mine is None or rec.get("updated_at", 0) > mine.get("updated_at", 0):
                    clean = {k: rec.get(k) for k in ("name", "address", "joined_at", "updated_at", "left_at")}
                    if mine != clean:
                        self.members[nid] = clean
                        changed = True
        if changed:
            self.save()
        return changed


def member_record(name: str, address: str | None, joined_at: float, left_at: float | None = None) -> dict:
    return {"name": name, "address": address, "joined_at": joined_at, "updated_at": time.time(), "left_at": left_at}


# ---------------------------------------------------------------- invites
def make_invite(network: dict, address: str, by: str) -> str:
    payload = {"v": PROTOCOL, "n": network["id"], "name": network["name"], "k": network["key"], "a": address, "by": by}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "rmq1-" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_invite(code: str) -> dict:
    code = "".join((code or "").split())
    if not code.startswith("rmq1-"):
        raise NetError("это не код приглашения в сеть менеджера ресурсов (он начинается с rmq1-)")
    body = code[5:]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
        assert data["n"] and data["k"] and data["a"]
        base64.urlsafe_b64decode(data["k"])
    except Exception:
        raise NetError("код приглашения повреждён: скопируйте его целиком") from None
    return data


# ---------------------------------------------------------------- signing
def body_hash(data: bytes = b"") -> str:
    return hashlib.sha256(data).hexdigest()


def signature(key: bytes, method: str, path: str, ts: str, nonce: str, bhash: str) -> str:
    msg = "\n".join((method.upper(), path, ts, nonce, bhash)).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def sign_headers(key: bytes, network_id: str, node_id: str, method: str, path: str, bhash: str) -> dict:
    ts, nonce = str(int(time.time())), secrets.token_hex(12)
    return {"X-RMQ-Net": network_id, "X-RMQ-Node": node_id, "X-RMQ-Time": ts, "X-RMQ-Nonce": nonce,
            "X-RMQ-Body": bhash, "X-RMQ-Sign": signature(key, method, path, ts, nonce, bhash)}


class NonceCache:
    """Each signed request is accepted once: a replayed one is refused."""

    def __init__(self, ttl: float = CLOCK_SKEW_S * 2):
        self.ttl = ttl
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def fresh(self, nonce: str, now: float) -> bool:
        with self._lock:
            if len(self._seen) > 50000 or (self._seen and min(self._seen.values()) < now - self.ttl):
                self._seen = {n: t for n, t in self._seen.items() if t >= now - self.ttl}
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True


def verify(headers, key: bytes, network_id: str, method: str, path: str, nonces: NonceCache) -> str:
    """The sender's node id if the request is signed with the network key, else NetError(401)."""
    if headers.get("X-RMQ-Net") != network_id:
        raise NetError("чужая сеть", 401)
    ts, nonce, bhash = headers.get("X-RMQ-Time", ""), headers.get("X-RMQ-Nonce", ""), headers.get("X-RMQ-Body", "")
    sign, node = headers.get("X-RMQ-Sign", ""), headers.get("X-RMQ-Node", "")
    try:
        skew = abs(time.time() - int(ts))
    except ValueError:
        raise NetError("нет подписи", 401) from None
    if skew > CLOCK_SKEW_S:
        raise NetError("часы этого ПК и отправителя расходятся больше чем на 5 минут", 401)
    if not (node and len(nonce) >= 16 and len(bhash) == 64):
        raise NetError("нет подписи", 401)
    if not hmac.compare_digest(sign, signature(key, method, path, ts, nonce, bhash)):
        raise NetError("подпись не сходится: другой ключ сети", 401)
    if not nonces.fresh(nonce, time.time()):
        raise NetError("повтор запроса", 401)
    return node


# ---------------------------------------------------------------- where this PC is reachable
def tailscale_address() -> str | None:
    """This PC's Tailscale IPv4 (100.64.0.0/10), if Tailscale is installed and connected. Tailscale's own adapter
    comes first: a provider's shared (CGNAT) address on another adapter can fall in the same range."""
    try:
        import psutil
        stats = psutil.net_if_stats()
        found = []
        for name, addrs in psutil.net_if_addrs().items():
            if name in stats and not stats[name].isup:
                continue
            for a in addrs:
                if a.family == socket.AF_INET and ipaddress.ip_address(a.address) in TAILSCALE_NET:
                    found.append((0 if "tailscale" in name.lower() else 1, a.address))
    except Exception:
        return None
    return min(found)[1] if found else None


# ---------------------------------------------------------------- packing a project for another PC
def _git_files(root: Path) -> list[str] | None:
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
                             capture_output=True, timeout=30, creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.decode("utf-8", errors="replace").split("\0") if p]


def collect_files(root: Path, patterns: list[str] | None, *, max_files: int = 20000,
                  max_bytes: int = 1 << 30) -> list[tuple[str, int]]:
    """Relative paths (with /) and sizes of what to send: the given globs, else the git repo's files
    (tracked and new, without the ignored), else the folder without virtualenvs, caches and VCS data."""
    root = root.resolve()
    rels: set[str] = set()
    if patterns:
        for pat in patterns:
            pat = pat.replace("\\", "/").lstrip("/")
            hits = list(root.glob(pat))
            if not hits:
                raise NetError(f"ничего не найдено по шаблону --files {pat}")
            for h in hits:
                items = [h] if h.is_file() else [x for x in h.rglob("*") if x.is_file()]
                for x in items:
                    rels.add(x.resolve().relative_to(root).as_posix())
    else:
        listed = _git_files(root)
        if listed is not None:
            rels = {p for p in listed if (root / p).is_file()}
        else:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
                for f in filenames:
                    rels.add((Path(dirpath) / f).relative_to(root).as_posix())
                if len(rels) > max_files:
                    break
    if len(rels) > max_files:
        raise NetError(f"в папке больше {max_files} файлов: укажите, что отправлять, через --files")
    out, total = [], 0
    for rel in sorted(rels):
        size = (root / rel).stat().st_size
        total += size
        out.append((rel, size))
    if total > max_bytes:
        raise NetError(f"проект весит {total / 2**20:.0f} МБ, а другим ПК можно отправить не больше "
                       f"{max_bytes / 2**20:.0f} МБ: укажите нужное через --files")
    return out


def make_zip(root: Path, files: list[tuple[str, int]], out: Path) -> str:
    """Pack the files and return the archive's SHA-256."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for rel, _size in files:
            z.write(root / rel, rel)
    os.replace(tmp, out)
    return file_sha256(out)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_name(rel: str) -> str | None:
    """A member name of an archive as a relative path that stays inside the target folder, or None."""
    rel = rel.replace("\\", "/")
    if not rel or rel.endswith("/") or rel.startswith("/") or ":" in rel:
        return None
    parts = PurePosixPath(rel).parts
    if any(p in ("", ".", "..") or p.split(".")[0].lower() in _RESERVED for p in parts):
        return None
    return "/".join(parts)


def safe_extract(zip_path: Path, dest: Path, *, max_bytes: int = 4 << 30) -> dict[str, list]:
    """Unpack only members that stay inside `dest`; returns {relative path: [size, mtime_ns]}."""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, list] = {}
    total = 0
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            rel = safe_name(info.filename)
            if rel is None:
                raise NetError(f"в архиве недопустимый путь: {info.filename}")
            total += info.file_size
            if total > max_bytes:
                raise NetError("архив распаковывается больше чем в допустимый объём")
            target = (dest / rel).resolve()
            if dest not in target.parents:
                raise NetError(f"в архиве путь за пределы папки: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
            st = target.stat()
            manifest[rel] = [st.st_size, st.st_mtime_ns]
    return manifest


def changed_files(root: Path, before: dict[str, list], limit_bytes: int) -> tuple[list[tuple[str, int]], list[str]]:
    """Files the task created or changed in its folder (its results), within `limit_bytes`; and the ones
    that did not fit."""
    root = root.resolve()
    out, skipped, total = [], [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in RESULT_SKIP)
        for f in sorted(filenames):
            p = Path(dirpath) / f
            rel = p.relative_to(root).as_posix()
            try:
                st = p.stat()
            except OSError:
                continue
            old = before.get(rel)
            if old and old[0] == st.st_size and old[1] == st.st_mtime_ns:
                continue
            if f.endswith((".pyc", ".pyo")):
                continue
            if total + st.st_size > limit_bytes:
                skipped.append(rel)
                continue
            total += st.st_size
            out.append((rel, st.st_size))
    return out, skipped


def matches_any(rel: str, patterns: list[str]) -> bool:
    rel = rel.replace("\\", "/")
    for pat in patterns:
        pat = pat.replace("\\", "/").strip("/")
        if fnmatch.fnmatch(rel, pat) or rel == pat or rel.startswith(pat + "/"):
            return True
    return False


def requirements_hash(text: str, python: str) -> str:
    norm = "\n".join(sorted(line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")))
    return hashlib.sha256(f"{python}\n{norm}".encode("utf-8")).hexdigest()[:16]
