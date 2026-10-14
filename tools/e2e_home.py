"""End-to-end check of "Мои ПК" and the dashboard before a release.

Two throwaway managers on this PC join their own "Мои ПК" (over 127.0.0.1 only), run a few sleeping tasks, act on
each other through the network and are opened in a headless Microsoft Edge:

    python tools/e2e_home.py           # all checks; exit code 0 when every one passes
    python tools/e2e_home.py --keep    # leave both managers running to look at (A: http://127.0.0.1:8790)

The real manager is not touched: other ports, data folders in %TEMP% and a fake user profile (so the agent rules in
the owner's home folder are never rewritten). Needs this checkout's .venv; the dashboard checks need Microsoft Edge.
The test tasks only sleep, so the run is light (about two minutes).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
CLI = ROOT / "rmq_cli.py"
EDGES = [Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
         Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe"]
NO_WINDOW = 0x08000000
SLEEP = 'python -c "import time; time.sleep(900)"'
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))

results: list[tuple[bool, str, str]] = []


def check(ok: bool, what: str, detail: str = "") -> bool:
    results.append((bool(ok), what, detail))
    print(f"  {'ok  ' if ok else 'FAIL'} {what}{'' if ok or not detail else f': {detail}'}", flush=True)
    return bool(ok)


def wait_for(cond, timeout: float, every: float = 1.0):
    """The first truthy value of cond() within `timeout` seconds, or None."""
    end = time.monotonic() + timeout
    while True:
        try:
            v = cond()
        except Exception:
            v = None
        if v or time.monotonic() >= end:
            return v
        time.sleep(every)


class Instance:
    def __init__(self, name: str, label: str, port: int, net_port: int, base: Path):
        self.name, self.label, self.port = name, label, port
        self.home = base / name
        self.home.mkdir(parents=True)
        (self.home / "settings.json").write_text(json.dumps(
            {"server": {"port": port}, "update": {"auto": False}, "network": {"bind": "127.0.0.1", "port": net_port}}),
            encoding="utf-8")
        self.env = dict(os.environ, RMQ_HOME=str(self.home), RMQ_URL=f"http://127.0.0.1:{port}",
                        USERPROFILE=str(base / "profile"))
        self.env.pop("RMQ_AGENT", None)
        (base / "profile" / "Desktop").mkdir(parents=True, exist_ok=True)
        self.log = open(base / f"{name}.out", "w", encoding="utf-8")
        self.proc = subprocess.Popen([str(PY), str(CLI), "serve"], cwd=str(ROOT), env=self.env, stdout=self.log,
                                     stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        self.token = None
        self.node = None

    def api(self, method: str, path: str, body=None, timeout: float = 40):
        if self.token is None:
            self.token = (self.home / "admin_token.txt").read_text(encoding="utf-8").strip()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with DIRECT.open(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            except ValueError:
                return e.code, None

    def ok(self, method: str, path: str, body=None, timeout: float = 40):
        status, data = self.api(method, path, body, timeout)
        if status != 200 and status != 201:
            raise RuntimeError(f"{method} {path}: {status} {data}")
        return data

    def healthy(self) -> bool:
        try:
            with DIRECT.open(f"http://127.0.0.1:{self.port}/api/health", timeout=2) as r:
                return r.status == 200
        except OSError:
            return False

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(self.env, PYTHONIOENCODING="utf-8")
        return subprocess.run([str(PY), str(CLI), *args], cwd=str(ROOT), env=env, capture_output=True,
                              encoding="utf-8", errors="replace", timeout=120, creationflags=NO_WINDOW)

    def stop(self) -> None:
        if self.healthy():
            self.cli("stop", "--tasks", "cancel")
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


def task_state(inst: Instance, tid: int) -> str | None:
    st = inst.ok("GET", "/api/state")
    return next((t["state"] for t in st["tasks"] if t["id"] == tid), None)


def network_checks(a: Instance, b: Instance, project: Path) -> None:
    print("сеть «Мои ПК»:")
    a.ok("POST", "/api/network/rename", {"name": a.label})
    b.ok("POST", "/api/network/rename", {"name": b.label})
    a.ok("POST", "/api/network/home/create", {"name": "Тестовые ПК"})
    code = a.ok("POST", "/api/network/home/invite", {})["code"]
    b.ok("POST", "/api/network/join", {"code": code})
    seen = wait_for(lambda: next((m for m in a.ok("GET", "/api/network")["home"]["members"]
                                  if not m["self"] and m["online"]), None), 40)
    if not check(seen, "Б присоединился к «Моим ПК» А и на связи"):
        raise RuntimeError("без сети дальше проверять нечего")
    b.node = seen["node_id"]
    a.node = next(m["node_id"] for m in b.ok("GET", "/api/network")["home"]["members"] if not m["self"])

    for i in (1, 2):
        b.ok("POST", "/api/tasks", {"command": SLEEP, "cpu": 0.1, "ram": 0.1, "time": "15m", "name": f"сон-{i}",
                                    "cwd": str(project)})
    a.ok("POST", "/api/tasks", {"command": SLEEP, "cpu": 0.1, "ram": 0.1, "time": "15m", "name": "сон-А",
                                "cwd": str(project)})
    check(wait_for(lambda: task_state(b, 1) == "running" and task_state(b, 2) == "running", 30), "задачи на Б запустились")

    # the main page of A: what runs on B right now (from B's hellos, every 10 s)
    def running_on_b():
        pcs = a.ok("GET", "/api/state").get("home_running") or []
        pc = next((p for p in pcs if p["node_id"] == b.node), None)
        return pc if pc and len(pc["tasks"]) == 2 else None
    pc = wait_for(running_on_b, 40, 2)
    check(pc and {t["name"] for t in pc["tasks"]} == {"сон-1", "сон-2"}, "главная А видит выполняющиеся задачи Б")

    base = f"/api/home/{b.node}"
    st = a.ok("GET", f"{base}/state")
    check(sum(t["state"] == "running" for t in st["tasks"]) == 2, "А читает состояние Б")
    m = a.ok("GET", f"{base}/metrics?range=300&since=0")
    check(len(m.get("t") or []) > 3 and m.get("task_names"), "А читает графики Б")
    t = a.ok("GET", f"{base}/tasks/1")
    check(t.get("name") == "сон-1", "А открывает карточку задачи Б")
    check("data" in a.ok("GET", f"{base}/tasks/1/log?offset=-1000"), "А читает лог задачи Б")
    a.ok("POST", f"{base}/tasks/2/suspend", {})
    check(wait_for(lambda: task_state(b, 2) == "suspended", 15), "А (основной) ставит задачу Б на паузу")
    a.ok("POST", f"{base}/tasks/2/resume", {})
    check(wait_for(lambda: task_state(b, 2) == "running", 15), "А продолжает задачу Б")
    status, _ = b.api("POST", f"/api/home/{a.node}/tasks/1/suspend", {})
    check(status == 403, "Б (не основной) не может управлять задачами А", f"код {status}")
    status, _ = b.api("GET", f"/api/home/{a.node}/state")
    check(status == 200, "Б может смотреть состояние А", f"код {status}")
    status, _ = b.api("GET", f"/api/home/{a.node}/settings")
    check(status == 403, "Б не может менять настройки А", f"код {status}")

    # a task sent from A to B comes back with its files
    rid = a.ok("POST", "/api/tasks", {"argv": ["python", "job.py"], "cpu": 0.5, "ram": 0.5, "time": "2m",
                                      "where": b.label, "cwd": str(project), "name": "через-сеть",
                                      "files": ["job.py"]})["id"]
    done = wait_for(lambda: task_state(a, rid) is None, 120, 2)
    out = project / "out" / "hello.txt"
    check(done and out.exists() and out.read_text(encoding="utf-8").strip() == "привет с Б",
          "задача, отправленная с А на Б, вернула свои файлы")

    # statistics and the chronicle of both PCs
    rep = a.ok("GET", "/api/stats?period=24h&pc=all", timeout=90)
    check([p["pc"] for p in rep.get("per_pc") or []] == [a.label, b.label], "общая статистика А и Б")
    rep = a.ok("GET", f"/api/stats?period=24h&pc={urllib.parse.quote(b.label)}", timeout=90)
    check(rep.get("pc") == b.label, "статистика Б с А")
    ch = a.ok("GET", "/api/chronicle?days=1&pc=all", timeout=90)
    pcs = {t.get("pc") for t in ch.get("tasks") or []}
    check({a.label, b.label} <= pcs, "общая хроника А и Б", str(pcs))
    sent = [t for t in ch["tasks"] if t.get("name") == "через-сеть"]
    check(len(sent) == 1 and sent[0]["pc"] == b.label, "задача через сеть в общей хронике один раз, на Б", str(len(sent)))

    # agents read the statistics
    r = a.cli("stats", "--period", "24h", "--pc", "all")
    check(r.returncode == 0 and "по ПК:" in r.stdout and b.label in r.stdout, "rmq stats --pc all", r.stderr[-300:])
    r = a.cli("stats", "--json")
    try:
        check(r.returncode == 0 and "total" in json.loads(r.stdout), "rmq stats --json")
    except ValueError as e:
        check(False, "rmq stats --json", str(e))

    # updates: the main PC asks the others
    r = a.ok("POST", "/api/network/home/update", {})
    check(b.label in r.get("asked", []), "«Обновить все мои ПК» доходит до Б", str(r))


def dashboard_checks(a: Instance, base: Path) -> None:
    edge = next((p for p in EDGES if p.exists()), None)
    if edge is None:
        print("дашборд: Microsoft Edge не найден — пропущено")
        return
    print("дашборд (Edge без окна):")
    shots = Path(tempfile.gettempdir()) / "rmq-e2e-shots"  # kept after the run, replaced by the next one
    shots.mkdir(exist_ok=True)
    pages = {"main": "tab=overview", "home": "tab=network&net=home", "stats": "tab=stats", "chronicle": "tab=chronicle"}
    for name, hash_ in pages.items():
        url = f"http://127.0.0.1:{a.port}/#token={a.token}&{hash_}"
        flags = ["--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={base / 'edge'}",
                 "--window-size=1400,2600", "--virtual-time-budget=10000"]
        r = subprocess.run([str(edge), *flags, "--enable-logging=stderr", "--v=0", f"--screenshot={shots / (name + '.png')}",
                            url], capture_output=True, encoding="utf-8", errors="replace", timeout=120)
        errors = [ln for ln in r.stderr.splitlines() if "CONSOLE" in ln and f"127.0.0.1:{a.port}" in ln
                  and re.search(r"Uncaught|Error|error", ln)]
        dom = subprocess.run([str(edge), *flags, "--dump-dom", url], capture_output=True, encoding="utf-8",
                             errors="replace", timeout=120).stdout
        check(not errors and len(dom) > 20000, f"страница «{name}» без ошибок в консоли", "; ".join(errors)[:300])
        if name == "main":
            seg = dom[dom.find('id="t-home-running"'):][:4000]
            check("сон-1" in seg, "на главной список задач других ПК")
        if name == "home":
            check('id="pc-active"' in dom and "сон-2" in dom, "во «Внутренней сети» задачи Б")
        if name == "stats":
            check('id="stats-pc"' in dom and "Все мои ПК" in dom, "в статистике выбор ПК")
    print(f"  снимки страниц: {shots}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep", action="store_true", help="не останавливать тестовые менеджеры")
    ap.add_argument("--port", type=int, default=8790, help="порт дашборда А (Б — следующий)")
    args = ap.parse_args()
    if not PY.exists():
        print(f"нет окружения {PY}: запустите setup.cmd", file=sys.stderr)
        return 2
    base = Path(tempfile.mkdtemp(prefix="rmq-e2e-"))
    project = base / "project"
    project.mkdir()
    (project / "job.py").write_text("import os\nos.makedirs('out', exist_ok=True)\n"
                                    "open('out/hello.txt', 'w', encoding='utf-8').write('привет с Б')\n", encoding="utf-8")
    print(f"папка теста: {base}")
    a = Instance("A", "Тест-А", args.port, 8781, base)
    b = Instance("B", "Тест-Б", args.port + 1, 8782, base)
    try:
        for inst in (a, b):
            if not check(wait_for(inst.healthy, 40), f"менеджер {inst.label} запустился"):
                raise RuntimeError(f"менеджер {inst.label} не запустился: {base / (inst.name + '.out')}")
        network_checks(a, b, project)
        dashboard_checks(a, base)
    except Exception as e:  # a check that could not even run
        check(False, "тест прервался", f"{type(e).__name__}: {e}")
    finally:
        if args.keep:
            print(f"менеджеры оставлены: А http://127.0.0.1:{a.port}/#token={a.token}  Б http://127.0.0.1:{b.port}")
        else:
            for inst in (a, b):
                inst.stop()
            shutil.rmtree(base, ignore_errors=True)
    failed = [w for ok, w, _ in results if not ok]
    print(f"\nитог: {len(results) - len(failed)} из {len(results)} проверок прошли"
          + (f"; не прошли: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
