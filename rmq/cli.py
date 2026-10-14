"""`rmq` command line: what agents use to put computations into the queue.

Agents need no keys: the CLI works out who is calling (the AI tool and its project)
and introduces itself to the manager, which registers new agents automatically.
The owner's admin token is used only by the hidden launcher commands (desktop
shortcut, stop.cmd), never for agents' tasks.

Standard library only (psutil is used when available to recognise the calling tool).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
FINAL = {"done", "failed", "cancelled", "timeout", "lost"}
EXIT_ERROR, EXIT_DOWN, EXIT_TIMEOUT, EXIT_INTERRUPTED = 2, 3, 124, 130
EPILOG = """\
примеры:
  rmq run --cpu 4 --ram 8 --time 2h -- python train.py --epochs 10
  rmq submit --cpu 2 --ram 4 --vram 6 --time 30m --name sweep-1 -- python sweep.py
  rmq wait 12 --timeout 540        (код выхода = код задачи; 124 = ещё не закончилась)
  rmq logs 12 --tail 50
  rmq top                          (сколько ресурсов сейчас доступно задачам)
  rmq stats --period 24h           (статистика менеджера: нагрузка, задачи, температуры)
  rmq history --task 12 --range 6h (сколько брала задача по времени и колебалась ли нагрузка)
  rmq submit --series sweep ...    (задачи серии идут по одной, по порядку; --after 12 — после задачи 12)
  rmq past --name sweep            (прошлые запуски: время, пик ОЗУ, ядра — чтобы точнее заявить следующий)
  rmq retime 12 --time 5h          (поправить заявку времени задачи на ходу)
  rmq guide                        (полное руководство для ИИ-агентов)

ключи не нужны: rmq сам определяет агента (rmq whoami).
коды выхода: код задачи | 2 ошибка | 3 менеджер не запущен (rmq start) | 124 таймаут ожидания | 130 прервано
"""
# (process-name prefix or command-line fragment, tool name); "codex" must come before "code"
KNOWN_TOOLS = [("claude", "claude-code"), ("cursor", "cursor"), ("codex", "codex"), ("windsurf", "windsurf"),
               ("gemini", "gemini"), ("aider", "aider"), ("cline", "cline"), ("continue", "continue"),
               ("copilot", "copilot"), ("code", "vscode")]


def home() -> Path:
    return Path(os.environ.get("RMQ_HOME") or ROOT / "data")


def default_url() -> str:
    if os.environ.get("RMQ_URL"):
        return os.environ["RMQ_URL"].rstrip("/")
    port = 8777
    try:
        port = int(json.loads((home() / "settings.json").read_text(encoding="utf-8"))["server"]["port"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return f"http://127.0.0.1:{port}"


def admin_token() -> str | None:
    try:
        return (home() / "admin_token.txt").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


# the manager is on this PC: never through a system proxy
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CliError(Exception):
    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- who is calling
def _git_root(path: str) -> str | None:
    p = Path(path).resolve()
    for d in [p, *p.parents]:
        if (d / ".git").exists():
            return str(d)
    return None


def _tool_from_ancestry() -> str | None:
    try:
        import psutil
    except ImportError:
        return None
    try:
        for proc in psutil.Process().parents():
            name = proc.name().lower()
            cmd = ""
            if name.startswith(("node", "bun", "deno")):
                try:
                    cmd = " ".join(proc.cmdline()).lower()
                except psutil.Error:
                    pass
            for key, tool in KNOWN_TOOLS:
                if name.startswith(key) or (cmd and key in cmd):
                    return tool
    except psutil.Error:
        pass
    return None


def _proc_cwd(pid: str | None) -> str | None:
    if not pid:
        return None
    try:
        import psutil
        return psutil.Process(int(pid)).cwd()
    except Exception:  # psutil missing, process gone, access denied...
        return None


def agent_identity() -> tuple[str, str]:
    """(name, info): the AI tool and the project it works in, e.g. 'claude-code@my-project'."""
    env = os.environ
    if env.get("RMQ_AGENT", "").strip():
        return env["RMQ_AGENT"].strip(), "имя задано переменной RMQ_AGENT"
    tool, project = None, None
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT"):
        tool, project = "claude-code", _proc_cwd(env.get("CLAUDE_PID"))
    tool = tool or _tool_from_ancestry() or "agent"
    project = project or _git_root(os.getcwd()) or os.getcwd()
    base = os.path.basename(project.rstrip("\\/")) or project
    return f"{tool}@{base}", f"{tool}, {project}"


# ---------------------------------------------------------------- HTTP
class Client:
    def __init__(self, url: str, *, admin: bool = False):
        self.url = url
        self.headers = {"Content-Type": "application/json"}
        if admin:
            tok = admin_token()
            if not tok:
                raise CliError(f"не найден админ-токен ({home() / 'admin_token.txt'}); менеджер запускался?")
            self.headers["Authorization"] = f"Bearer {tok}"
        else:
            name, info = agent_identity()
            self.headers["X-RMQ-Agent"] = quote(name, safe="@-_.()+ ")
            self.headers["X-RMQ-Agent-Info"] = quote(info, safe="@-_.()+ ,:\\/")

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 30, raw: bool = False):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=self.headers)
        try:
            with LOCAL.open(req, timeout=timeout) as r:
                payload = r.read().decode("utf-8")
                return payload if raw else json.loads(payload)
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error", str(e))
            except ValueError:
                msg = str(e)
            raise CliError(msg) from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            raise CliError(f"менеджер ресурсов не отвечает на {self.url} ({reason}). "
                           f"Запусти его командой: rmq start", EXIT_DOWN) from None


def alive(url: str) -> bool:
    try:
        with LOCAL.open(url + "/api/health", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8")).get("ok", False)
    except Exception:
        return False


def spawn_server(url: str, wait_s: float = 25) -> None:
    """Start the manager in the background (no window) and wait until it answers."""
    pyw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not pyw.exists():
        raise CliError(f"окружение менеджера не установлено: владелец ПК должен один раз запустить "
                       f"{ROOT / 'start.cmd'} (или ярлык на рабочем столе)")
    env = {k: v for k, v in os.environ.items() if k not in ("__PYVENV_LAUNCHER__", "RMQ_AGENT")}
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    kw = dict(cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True)
    try:
        subprocess.Popen([str(pyw), str(ROOT / "rmq_cli.py"), "serve"], creationflags=flags | 0x01000000, **kw)
    except OSError:  # cannot leave the caller's job object: start inside it
        subprocess.Popen([str(pyw), str(ROOT / "rmq_cli.py"), "serve"], creationflags=flags, **kw)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if alive(url):
            return
        time.sleep(0.5)
    raise CliError(f"менеджер не ответил за {wait_s:.0f} с; подробности в {home() / 'manager.log'}")


def leave_stray_console() -> None:
    """Started as pythonw.exe (meant to have no window) yet sitting in a console of its own: leave it.

    The venv pythonw.exe of Python 3.13.0 starts the console python.exe, so Windows opened a black window for the
    manager - empty, deaf to keys, and closing it stopped the manager."""
    names = {Path(sys.executable or "").name.lower(), Path((getattr(sys, "orig_argv", None) or [""])[0]).name.lower()}
    if os.name != "nt" or "pythonw.exe" not in names:
        return
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.GetConsoleWindow():
        return

    def on_console(stream) -> bool:
        try:
            return stream is not None and stream.isatty()
        except (ValueError, OSError):
            return False

    stale = [name for name in ("stdin", "stdout", "stderr") if on_console(getattr(sys, name))]
    if kernel32.FreeConsole():
        for name in stale:
            setattr(sys, name, None)  # as under a real pythonw.exe


def swing_text(s: dict | None) -> str:
    from .swing import describe
    return describe(s)


def err(text: str) -> None:
    print(f"[rmq] {text}", file=sys.stderr, flush=True)


def fmt_dur(s) -> str:
    if s is None:
        return "-"
    s = int(round(max(0, s)))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{d}д {h}ч" if d else f"{h}ч {m:02d}м" if h else f"{m}м {s:02d}с" if m else f"{s}с"


# ---------------------------------------------------------------- task commands
def task_body(args) -> dict:
    if args.shell:
        if not args.command:
            raise CliError("после --shell нужна команда")
        body = {"command": " ".join(args.command)}
    else:
        if not args.command:
            raise CliError("нужна команда после --, например: rmq run --cpu 2 -- python script.py")
        body = {"argv": list(args.command)}
    body["cwd"] = os.path.abspath(args.cwd or os.getcwd())
    extra = {}
    for kv in args.env or []:
        k, _, v = kv.partition("=")
        extra[k] = v
    if not args.no_env:
        env = {k: v for k, v in os.environ.items() if k not in ("__PYVENV_LAUNCHER__",)}
        env.update(extra)
        body["env"] = env
    if args.where:  # another PC of the network gets only what was asked for explicitly, never this environment
        body.update(where=args.where, env_extra=extra, files=args.files or None, results=args.results or None,
                    requirements=args.requirements)
    for field in ("name", "cpu", "ram", "vram", "time", "time_limit", "priority", "preempt", "series", "after"):
        v = getattr(args, field)
        if v is not None:
            body[field] = v
    return body


def describe_wait(t: dict) -> str:
    if t["state"] == "remote":
        return t.get("state_text", "в сети") + (f" ({t['reason_text']})" if t.get("reason_text") else "")
    if t["state"] == "queued":
        pos = f"позиция {t['position']}" if t.get("position") else "в очереди"
        return f"{pos}: {t['wait_text']}" if t.get("wait_text") else pos
    if t["state"] == "suspended":
        return t.get("reason_text") or "на паузе"
    if t.get("stopping"):
        return f"{t['reason_text']}; принудительно через {fmt_dur(t['stopping']['left_s'])}"
    return t.get("state_text", t["state"])


def summary(t: dict) -> str:
    pr = t.get("peak_ram") or 0
    peak = (f", пик ОЗУ {pr:.1f} ГБ" if pr >= 1 else f", пик ОЗУ {pr * 1024:.0f} МБ") if pr else ""
    code = f"код {t['exit_code']}" if t.get("exit_code") is not None else t.get("state_text", t["state"])
    note = f" — {t['reason_text']}" if t.get("reason_text") and t["state"] != "done" else ""
    return f"#{t['id']} {t['state_text']}: {code}, время работы {fmt_dur(t.get('run_s'))}{peak}{note}"


def exit_code_of(t: dict) -> int:
    if t["state"] == "done":
        return 0
    if t.get("exit_code") is not None:
        return int(t["exit_code"]) & 0xFF or 1
    return 1


def call_waiting(c: Client, path: str, deadline: float | None, told: list) -> dict:
    """c.call GET, but while the manager does not answer (the PC slept, it is being restarted or updated) keep
    trying until `deadline`; then the "not answering" error goes on."""
    while True:
        try:
            return c.call("GET", path)
        except CliError as e:
            if e.code != EXIT_DOWN or (deadline is not None and time.monotonic() >= deadline):
                raise
            if not told:
                err("менеджер ресурсов не отвечает (ПК спал или менеджер перезапускается) — жду дальше")
                told.append(True)
            time.sleep(5.0)


def follow(c: Client, tid: int, *, stream_log: bool, timeout: float | None, quiet: bool = False) -> int:
    """Wait for the task, optionally streaming its log; returns its exit code (124 on timeout). A manager that
    stops answering for a while (sleep, restart) is waited for, too."""
    offset = 0
    last_status = None
    started = time.monotonic()
    deadline = started + timeout if timeout is not None else None
    told: list = []
    seen = False
    while True:
        try:
            t = call_waiting(c, f"/api/tasks/{tid}", deadline, told)
        except CliError as e:
            if e.code != EXIT_DOWN or not seen:
                raise
            if not quiet:
                err(f"#{tid}: менеджер так и не ответил; продолжить ожидание: rmq wait {tid}")
            return EXIT_TIMEOUT
        seen = True
        if stream_log:
            while True:
                chunk = call_waiting(c, f"/api/tasks/{tid}/log?offset={offset}&limit=262144", deadline, told)
                if chunk["data"]:
                    sys.stdout.write(chunk["data"])
                    sys.stdout.flush()
                offset = chunk["offset"]
                if len(chunk["data"]) < 200_000:
                    break
        status = (t["state"], t.get("reason"), t.get("wait"))
        if status != last_status and not quiet:
            last_status = status
            if t["state"] not in FINAL:
                err(f"#{tid} {t['state_text']}" + (f" — {describe_wait(t)}" if t["state"] != "running" else ""))
        if t["state"] in FINAL:
            if not quiet:
                err(summary(t))
            return exit_code_of(t)
        if timeout is not None and time.monotonic() - started >= timeout:
            if not quiet:
                err(f"#{tid} ещё не завершена ({describe_wait(t)}); продолжить ожидание: rmq wait {tid}")
            return EXIT_TIMEOUT
        time.sleep(1.0 if stream_log else 2.0)


def submit(args) -> tuple[Client, dict]:
    c = Client(args.url)
    r = c.call("POST", "/api/tasks", task_body(args))
    for w in r.get("warnings", []):
        err(f"внимание: {w}")
    return c, r


def cmd_submit(args) -> int:
    _, r = submit(args)
    if args.json:
        print(json.dumps(r, ensure_ascii=False))
    else:
        print(r["id"])
        err(f"задача #{r['id']} поставлена в очередь; дальше: rmq wait {r['id']} --timeout 540 | "
            f"rmq status {r['id']} | дашборд {args.url}/#task={r['id']}")
    return 0


def cmd_run(args) -> int:
    c, r = submit(args)
    tid = r["id"]
    err(f"задача #{tid} поставлена в очередь (если ожидание оборвётся, она продолжит работу: "
        f"rmq wait {tid} / rmq logs {tid} -f; дашборд {args.url}/#task={tid})")
    try:
        return follow(c, tid, stream_log=True, timeout=args.timeout)
    except KeyboardInterrupt:
        if args.detach:
            err(f"отсоединились; задача #{tid} продолжает работу")
            return EXIT_INTERRUPTED
        try:
            c.call("POST", f"/api/tasks/{tid}/cancel")
            err(f"задача #{tid} отменена (Ctrl+C)")
        except CliError as e:
            err(str(e))
        return EXIT_INTERRUPTED


def cmd_wait(args) -> int:
    c = Client(args.url)
    ids = list(dict.fromkeys(args.id))
    if len(ids) == 1:
        return follow(c, ids[0], stream_log=args.log, timeout=args.timeout, quiet=args.quiet)
    return wait_all(c, ids, timeout=args.timeout, quiet=args.quiet)


def wait_all(c: Client, ids: list[int], *, timeout: float | None, quiet: bool) -> int:
    """Wait until every one of the tasks has ended: 0 if all succeeded, otherwise the code of the first one (in the
    order given) that did not; 124 on timeout."""
    deadline = time.monotonic() + timeout if timeout is not None else None
    left, codes, told, seen = list(ids), {}, [], False
    while left:
        for tid in list(left):
            try:
                t = call_waiting(c, f"/api/tasks/{tid}", deadline, told)
            except CliError as e:
                if e.code != EXIT_DOWN or not seen:
                    raise
                left_ids = " ".join(map(str, left))
                if not quiet:
                    err(f"менеджер так и не ответил; продолжить ожидание: rmq wait {left_ids}")
                return EXIT_TIMEOUT
            seen = True
            if t["state"] in FINAL:
                codes[tid] = exit_code_of(t)
                left.remove(tid)
                if not quiet:
                    err(summary(t))
        if not left:
            break
        if deadline is not None and time.monotonic() >= deadline:
            if not quiet:
                err(f"ещё не завершены: {', '.join(f'#{i}' for i in left)}; продолжить ожидание: "
                    f"rmq wait {' '.join(map(str, left))}")
            return EXIT_TIMEOUT
        time.sleep(3.0)
    return next((codes[i] for i in ids if codes[i]), 0)


def cmd_logs(args) -> int:
    c = Client(args.url)
    tail_bytes = 1 << 20
    task = pc_path(args, f"tasks/{args.id}")
    if args.follow:
        t = c.call("GET", task)
        offset = max(0, t.get("log_size", 0) - tail_bytes) if args.tail is not None else 0
        while True:
            chunk = c.call("GET", f"{task}/log?offset={offset}&limit=262144")
            if chunk["data"]:
                sys.stdout.write(chunk["data"])
                sys.stdout.flush()
            offset = chunk["offset"]
            if not chunk["data"]:
                t = c.call("GET", task)
                if t["state"] in FINAL:
                    err(summary(t))
                    return exit_code_of(t)
                time.sleep(1.0)
    start = -tail_bytes if args.tail is not None else 0
    chunk = c.call("GET", f"{task}/log?offset={start}&limit=1048576", timeout=60)
    text = chunk["data"]
    if args.tail is not None:
        text = "\n".join(text.splitlines()[-args.tail:]) + "\n"
    sys.stdout.write(text)
    return 0


def cmd_nodes(args) -> int:
    r = Client(args.url).call("GET", "/api/nodes")
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    if not r["nodes"]:
        print("этот ПК не в вычислительной сети: задачи выполняются только здесь")
        return 0
    if r["mode"] == "offline":
        print("сеть выключена владельцем (офлайн): задачи выполняются только здесь")
    for n in r["nodes"]:
        hw, free, busy = n.get("hw") or {}, n.get("free") or {}, n.get("busy") or {}
        head = f"{n['name']}{' (этот ПК)' if n['self'] else ' (мой ПК)' if n.get('own') else ''}"
        if not n["online"]:
            print(f"{head}: не в сети" + (f" — {n['why']}" if n.get("why") else ""))
            continue
        state = "принимает задачи" if n["accepting"] else ("только для себя" if n.get("mode") == "one_way" else "закрыт")
        p, sp = n.get("path") or {}, n.get("speed")
        if p.get("direct"):
            state += ", связь напрямую"
        elif p.get("relay"):
            state += f", связь через ретранслятор Tailscale ({p['relay']})"
        if sp:
            r = sp["ratio"]
            state += (f", те же команды идут там в {r:.1f} раза дольше, чем здесь" if r >= 1.1 else
                      f", те же команды идут там в {1 / r:.1f} раза быстрее" if r <= 0.9 else ", по скорости как этот ПК")
        flags = ", ".join(x for x, on in (("игра", busy.get("game")), ("пауза", busy.get("paused")),
                                          ("перегрев", busy.get("heat") == "hot"), ("мало места", busy.get("disk_low")))
                          if on)
        print(f"{head}: {state}{f' ({flags})' if flags else ''}; {hw.get('threads', '?')} потоков, "
              f"{hw.get('ram_gb', '?')} ГБ ОЗУ, GPU {hw.get('gpu') or 'нет'} {hw.get('vram_gb') or 0} ГБ; "
              f"свободно сейчас: CPU {free.get('cpu', 0):.1f}, ОЗУ {free.get('ram', 0):.1f} ГБ, "
              f"VRAM {free.get('vram', 0):.1f} ГБ")
    return 0


def cmd_net(args) -> int:
    """Owner commands for the network (the dashboard's "Сеть" tab does the same)."""
    c = Client(args.url, admin=True)
    if args.action == "status":
        r = c.call("GET", "/api/network")
    elif args.action == "create":
        r = c.call("POST", "/api/network/create", {"name": args.value or ""})
    elif args.action == "invite":
        print(c.call("POST", "/api/network/invite", {})["code"])
        return 0
    elif args.action == "join":
        r = c.call("POST", "/api/network/join", {"code": args.value or ""}, timeout=60)
    elif args.action == "leave":
        r = c.call("POST", "/api/network/leave", {}, timeout=60)
    elif args.action == "mode":
        r = c.call("POST", "/api/network/mode", {"mode": args.value or ""})
    elif args.action == "home-create":
        r = c.call("POST", "/api/network/home/create", {"name": args.value or ""})
    elif args.action == "home-invite":
        print(c.call("POST", "/api/network/home/invite", {})["code"])
        return 0
    elif args.action == "home-leave":
        r = c.call("POST", "/api/network/home/leave", {}, timeout=60)
    elif args.action == "home-main":
        r = c.call("POST", "/api/network/home/main", {"node": args.value or ""})
    elif args.action == "home-ext":  # rmq net home-ext "ПК=open|use|hidden"
        node, _, state = (args.value or "").rpartition("=")
        r = c.call("POST", "/api/network/home/external", {"node": node, "state": state}, timeout=90)
    else:
        r = c.call("POST", "/api/network/rename", {"name": args.value or ""})
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def cmd_cancel(args) -> int:
    r = Client(args.url).call("POST", f"/api/tasks/{args.id}/cancel")
    err(f"задача #{r['id']}: {r['note']}" if r.get("note") else f"задача #{r['id']} отменена")
    return 0


def cmd_resume(args) -> int:
    r = Client(args.url).call("POST", f"/api/tasks/{args.id}/resume")
    err(f"задача #{r['id']}: {r.get('note', 'возобновится')}")
    return 0


def cmd_status(args) -> int:
    c = Client(args.url)
    if args.id is None:
        return cmd_queue(args)
    t = c.call("GET", pc_path(args, f"tasks/{args.id}"), timeout=60)
    if args.json:
        print(json.dumps(t, ensure_ascii=False, indent=2))
        return 0
    print(f"#{t['id']} {t['name']}  [{t['state_text']}]  агент: {t['agent']}"
          + (f"  ПК: {t['where']}" if t.get("where") else "") + (f"  из сети: {t['from']}" if t.get("from") else ""))
    print(f"  команда: {t['command']}")
    print(f"  заявка: CPU {t['cpu']:g} яд., ОЗУ {t['ram']:g} ГБ, VRAM {t['vram']:g} ГБ, "
          f"время ~{fmt_dur(t['time_s'])}, приоритет {t['priority']}, при нехватке: {t['preempt']}")
    if t["state"] not in FINAL:
        print(f"  статус: {describe_wait(t)}")
    if t.get("used"):
        u = t["used"]
        print(f"  сейчас: CPU {u['cpu']:.1f} яд., ОЗУ {u['ram']:.1f} ГБ, VRAM {u['vram']:.1f} ГБ, GPU {u['gpu']:.0f}%")
    pr = t.get("progress")
    if pr:
        count = f"{pr['done']:g}/{pr['total']:g}, " if pr.get("total") else ""
        eta = f", осталось ~{fmt_dur(pr['eta_s'])}" if pr.get("eta_s") is not None else ""
        note = f" — {pr['note']}" if pr.get("note") else ""
        print(f"  прогресс: {count}{pr['pct']:g}%{eta}{note} (источник: {pr['source']})")
        if pr.get("eta_conflict"):
            print(f"  ВНИМАНИЕ: по скорости за последние 10 мин осталось ~{fmt_dur(pr['eta_pace_s'])}, а по доле "
                  f"выполненного ~{fmt_dur(pr.get('eta_s'))} — оценки расходятся; проверь, как задача считает прогресс")
        elif pr.get("stalled_s"):
            print(f"  ВНИМАНИЕ: прогресс не менялся {fmt_dur(pr['stalled_s'])} работы")
    order = [f"серия «{t['series']}»" if t.get("series") else "", f"после #{t['after_id']}" if t.get("after_id") else ""]
    if any(order):
        print("  порядок: " + ", ".join(x for x in order if x))
    sw = t.get("swing") or (t.get("remote") or {}).get("swing")
    if sw:
        print(f"  нагрузка {swing_text(sw)} — так бывает, когда у задачи есть фазы ожидания (проверка, "
              f"синхронизация, диск или сеть): она идёт дольше, чем могла бы на своих ядрах")
    if t.get("reason") == "hung" and t["state"] == "suspended":
        print(f"  что делать: посмотри rmq logs {t['id']}; продолжить — rmq resume {t['id']}, отменить — rmq cancel {t['id']}")
    held = t.get("held_s") or 0
    print(f"  время работы: {fmt_dur(t['run_s'])}" + (" (дольше заявленного)" if t.get("overtime") else "")
          + (f", из них {fmt_dur(held)} с урезанным процессором (в сверхурочные не идёт)" if held >= 60 else "")
          + (f", перезапусков: {t['restarts']}" if t.get("restarts") else ""))
    fc = t.get("forecast")
    if fc and t["state"] not in FINAL:
        print(f"  прогноз: {forecast_text(t)} ({FORECAST_BASIS.get(fc.get('basis'), '')}; примерный — считает, что "
              f"владелец пользуется ПК как сейчас)")
    cond = t.get("conditions") or (t.get("remote") or {}).get("conditions")
    if cond:
        print(f"  условия счёта: {conditions_text(cond)}")
        if cond.get("energy_wh") is not None:
            print(f"  энергия: ~{energy_text(cond['energy_wh'])} (оценка: доля мощности процессора и видеокарты по "
                  f"занятому времени)")
    if getattr(args, "events", False):
        print("  события:")
        for e in reversed(t.get("events") or []):
            print(f"    {time.strftime('%d.%m %H:%M:%S', time.localtime(e['ts']))}  {e['message']}")
    if t["state"] in FINAL:
        print(f"  итог: {summary(t)}")
    return 0


FORECAST_BASIS = {"прошлые запуски": "по прошлым запускам этой команды", "заявка": "по заявленному времени",
                  "прогресс": "по прогрессу задачи"}


def eta_at(ts: float) -> str:
    """"15:40", with the date when it is not today."""
    same_day = time.strftime("%Y%m%d", time.localtime(ts)) == time.strftime("%Y%m%d")
    return time.strftime("%H:%M" if same_day else "%d.%m %H:%M", time.localtime(ts))


def forecast_text(t: dict) -> str:
    f = t.get("forecast")
    if not f:
        return ""
    if t["state"] == "running":
        return f"закончится ~{eta_at(f['end'])}"
    if f["start"] - time.time() < 60:
        return f"стартует сейчас, закончится ~{eta_at(f['end'])}"
    return f"старт ~{eta_at(f['start'])}, конец ~{eta_at(f['end'])}"


def energy_text(wh: float) -> str:
    return f"{wh / 1000:.2f} кВт·ч" if wh >= 1000 else f"{wh:.1f} Вт·ч"


def cpu_text(power, clock_p, clock_e) -> str:
    """The CPU's package power and core clocks (LibreHardwareMonitor), as far as they are known."""
    out = [f"{power:.0f} Вт" if power is not None else None,
           f"{'P-ядра' if clock_e is not None else 'ядра'} {clock_p:.2f} ГГц" if clock_p is not None else None,
           f"E-ядра {clock_e:.2f} ГГц" if clock_e is not None else None]
    return ", ".join(x for x in out if x)


def conditions_text(c: dict) -> str:
    """What the task ran under (for its own timings): "чистые" or what disturbed them; the CPU's average power and
    clocks meanwhile, where they were measured."""
    cpu = cpu_text(c.get("cpu_power_w"), c.get("clock_p_ghz"), c.get("clock_e_ghz"))
    tail = f"; процессор в среднем {cpu}" if cpu else ""
    if c.get("clean"):
        return "чистые: без пауз, сна, урезания процессора и соседних задач" + tail
    parts = [(c.get("paused_s"), "на паузе"), (c.get("slept_s"), "ПК спал"), (c.get("held_s"), "урезанный процессор"),
             (c.get("ecores_s"), "только E-ядра (игра)"), (c.get("throttled_s"), "регулятор температуры"),
             (c.get("shared_s"), f"рядом другие задачи (до {c.get('max_neighbours', 0)})")]
    out = [f"{label} {fmt_dur(v)}" for v, label in parts if (v or 0) >= 1]
    if (c.get("owner_cpu_avg") or 0) >= 0.5:
        out.append(f"владелец занимал в среднем {c['owner_cpu_avg']:.1f} яд.")
    return ("; ".join(out) or "чистые") + tail


def cmd_retime(args) -> int:
    r = Client(args.url).call("POST", f"/api/tasks/{args.id}/time", {"time": args.time})
    err(f"задача #{r['id']}: заявка времени ~{fmt_dur(r['old_time_s'])} → ~{fmt_dur(r['time_s'])} "
        f"(уже работает {fmt_dur(r['run_s'])})")
    return 0


def past_rows(c: Client, all_agents: bool) -> list[dict]:
    """Finished tasks, newest first, from tasks.csv (kept for good); an older manager: from its 30-day list."""
    import csv
    import io
    try:
        text = c.call("GET", "/api/stats/files/tasks.csv", timeout=120, raw=True)
    except CliError:
        text = None
    if text is None:
        from urllib.parse import urlencode
        r = c.call("GET", "/api/tasks?" + urlencode({"limit": 500, "mine": "0" if all_agents else "1"}), timeout=60)
        return [t for t in r["tasks"] if t["state"] in FINAL]
    me = None if all_agents else c.call("GET", "/api/whoami")["agent"]
    num = lambda v: float(v) if v not in (None, "") else None
    out = []
    for row in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        if me and row.get("agent") != me:
            continue
        run = (num(row.get("run_min")) or 0) * 60
        out.append({"id": int(row["id"]), "name": row.get("name") or "", "command": row.get("command") or "",
                    "state": row.get("state"), "state_text": STATE_TEXT.get(row.get("state"), row.get("state") or ""),
                    "run_s": run, "time_s": (num(row.get("time_claim_min")) or 0) * 60,
                    "peak_ram": num(row.get("peak_ram_gb")) or 0.0, "ram": num(row.get("ram_claim_gb")) or 0.0,
                    "cpu": num(row.get("cpu_claim")) or 0.0, "cpu_avg": num(row.get("cpu_avg_cores")),
                    "pc": row.get("pc"), "finished": row.get("finished"), "agent": row.get("agent"),
                    "wait_s": (num(row.get("wait_min")) or 0) * 60, "cpu_power_w": num(row.get("cpu_power_w")),
                    "clock_p_ghz": num(row.get("clock_p_ghz")), "clock_e_ghz": num(row.get("clock_e_ghz"))})
    return list(reversed(out))


STATE_TEXT = {"done": "готово", "failed": "ошибка", "cancelled": "отменена", "timeout": "лимит времени",
              "lost": "потеряна"}


def cmd_past(args) -> int:
    """Earlier runs - how long they took, their memory peak and cores - to size the next request."""
    rows = past_rows(Client(args.url), args.all_agents)
    if args.name:
        rows = [t for t in rows if args.name.lower() in (t["name"] or "").lower()]
    if args.command:
        rows = [t for t in rows if args.command.lower() in (t["command"] or "").lower()]
    rows = rows[:args.limit]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("подходящих завершённых задач нет")
        return 0
    clocks = any(t.get("clock_p_ghz") is not None for t in rows)  # the CPU's speed then (LibreHardwareMonitor)
    print(f"{'#':<6}{'задача':<28}{'итог':<12}{'работала':>10}{'заявка':>9}{'пик ОЗУ':>10}{'заявка':>8}"
          f"{'ядер ср.':>10}{'заявка':>8}" + (f"{'ГГц':>7}" if clocks else ""))
    for t in rows:
        run = t.get("run_s") or 0
        cores = t.get("cpu_avg") if "cpu_avg" in t else (t.get("cpu_s") or 0) / run if run > 60 and t.get("cpu_s") else None
        pr = t.get("peak_ram") or 0
        peak = f"{pr:.1f} ГБ" if pr >= 1 else f"{pr * 1024:.0f} МБ"
        ghz = f"{t['clock_p_ghz']:.2f}" if t.get("clock_p_ghz") is not None else "–"
        print(f"#{t['id']:<5}{(t['name'] or '')[:27]:<28}{t['state_text'][:11]:<12}{fmt_dur(run):>10}{fmt_dur(t['time_s']):>9}"
              f"{peak:>10}{t['ram']:>6g} ГБ{(f'{cores:.1f}' if cores is not None else '–'):>10}{t['cpu']:>8g}"
              + (f"{ghz:>7}" if clocks else ""))
    done = [t for t in rows if t["state"] == "done" and (t.get("run_s") or 0) > 0]
    if done:
        runs = sorted(t["run_s"] for t in done)
        peak = max(t.get("peak_ram") or 0 for t in rows)
        print(f"\nуспешных {len(done)} из {len(rows)}: работали ~{fmt_dur(runs[len(runs) // 2])} (медиана), "
              f"пик ОЗУ до {peak:.2f} ГБ — заявляй ОЗУ с запасом ~20% к пику, время — с запасом ~30%")
    return 0


def pc_path(args, rest: str) -> str:
    """An API path on this PC, or (--pc NAME) on another of the owner's PCs ("Мои ПК"), read-only."""
    pc = getattr(args, "pc", None)
    if not pc:
        return f"/api/{rest}"
    return f"/api/home/{quote(pc, safe='')}/{rest}"


def cmd_queue(args) -> int:
    st = Client(args.url).call("GET", pc_path(args, "state"), timeout=60)
    if getattr(args, "pc", None) and not getattr(args, "json", False):
        print(f"ПК {args.pc}:")
    if getattr(args, "json", False):
        print(json.dumps({"tasks": st["tasks"]}, ensure_ascii=False, indent=2))
        return 0
    if not st["tasks"]:
        print("очередь пуста, ничего не выполняется")
    for t in st["tasks"]:
        used = t.get("used") or {}
        pr = t.get("progress")
        extra = (f"CPU {used.get('cpu', 0):.1f}/{t['cpu']:g} ОЗУ {used.get('ram', 0):.1f}/{t['ram']:g}ГБ"
                 + (f" · {pr['pct']:g}%" if pr else "")
                 + (" · сохраняется перед остановкой" if t.get("stopping") else "")
                 + (f" · {swing_text(t['swing'])}" if t.get("swing") else "")
                 + (f" · {forecast_text(t)}" if t.get("forecast") else "")
                 if t["state"] == "running" else describe_wait(t) + (f" · {forecast_text(t)}" if t.get("forecast") else ""))
        print(f"#{t['id']:<5} {t['state_text']:<12} {t['agent'][:24]:<24} {t['name'][:28]:<28} {extra}")
    return 0


def cmd_history(args) -> int:
    """The load over time from the dashboard's charts: the whole PC, or one task (--task)."""
    from .swing import from_metrics
    from .util import parse_duration
    try:
        rng = max(60.0, min(30 * 86400.0, parse_duration(args.range)))
    except ValueError as e:
        raise CliError(f"--range: {e}") from None
    c = Client(args.url)
    m = c.call("GET", pc_path(args, f"metrics?range={rng:.0f}&since=0"), timeout=90)
    if args.json:
        print(json.dumps(m, ensure_ascii=False))
        return 0
    st = c.call("GET", pc_path(args, "state"), timeout=60)
    n_cpu = st["system"]["cpu"]["n"]
    ts = m.get("t") or []
    if len(ts) < 2:
        print("данных за этот срок пока нет")
        return 0
    key = str(args.task) if args.task else None
    claim = None
    if args.task:
        try:
            claim = c.call("GET", pc_path(args, f"tasks/{args.task}"))
        except CliError:
            claim = None
    rows = max(2, min(96, args.rows))
    span = (ts[-1] - ts[0]) / rows
    where = f"ПК {args.pc}" if args.pc else "этот ПК"
    head = f"задача #{args.task}" + (f" «{claim['name']}», заявка {claim['cpu']:g} яд." if claim else "") if key else "весь ПК"
    print(f"{head}, {where}: последние {fmt_dur(ts[-1] - ts[0])}, отрезки по ~{fmt_dur(span)} (средние; °C — максимум"
          f"{'; ГГц — средняя частота P-ядер' if any(x is not None for x in m.get('clock_p') or []) else ''})")
    power = any(x is not None for x in m.get("cpu_power") or [])  # measured (LibreHardwareMonitor)
    cols = ("владелец CPU  задачи CPU  ОЗУ задач  CPU °C" + ("  CPU Вт  ГГц" if power else "")
            + ("   задача: ядер сред/макс  ОЗУ, ГБ" if key else ""))
    print(f"{'время':<{25 if ts[-1] - ts[0] > 86400 else 13}}  {cols}  примечания")
    avg = lambda xs: sum(xs) / len(xs) if xs else None
    for k in range(rows):
        lo, hi = ts[0] + k * span, ts[0] + (k + 1) * span
        idx = [i for i, t in enumerate(ts) if lo <= t < hi or (k == rows - 1 and t == hi)]
        if not idx:
            continue
        pick = lambda col: [m[col][i] for i in idx if m.get(col) and m[col][i] is not None]
        user, tasks, ram = avg(pick("cpu_user")), avg(pick("cpu_tasks")), avg(pick("ram_tasks"))
        temps = pick("temp_cpu")
        notes = []
        if (avg(pick("game")) or 0) > 0.5:
            notes.append("игра")
        heat = max(pick("heat") or [0])
        if heat >= 2:
            notes.append("перегрев — задачи стояли")
        elif heat >= 1:
            notes.append("регулятор урезал")
        when = "%d.%m %H:%M" if ts[-1] - ts[0] > 86400 else "%H:%M"  # longer than a day: with dates
        line = (f"{time.strftime(when, time.localtime(lo))}–{time.strftime(when, time.localtime(hi))}  "
                f"{(user or 0):>11.0f}%  {(tasks or 0):>9.0f}%  {(ram or 0):>6.1f} ГБ  "
                f"{(f'{max(temps):.0f}' if temps else '–'):>6}")
        if power:
            pw, ck = avg(pick("cpu_power")), avg(pick("clock_p"))
            line += f"  {(f'{pw:.0f}' if pw is not None else '–'):>6}  {(f'{ck:.2f}' if ck is not None else '–'):>4}"
        if key:
            per = [(m["tasks"][i] or {}).get(key) for i in idx]
            cores = [v[0] * n_cpu / 100 if v else 0.0 for v in per]
            tram = [v[1] if v else 0.0 for v in per]
            line += f"   {avg(cores):>11.1f} / {max(cores):<5.1f}  {max(tram):>7.1f}"
        print(f"{line}  {', '.join(notes)}")
    if key:
        sw = from_metrics(m, args.task, n_cpu)
        if sw:
            print(f"\nза последние 10 мин нагрузка задачи {swing_text(sw)}: похоже на фазы ожидания "
                  f"(проверка, синхронизация, диск или сеть)")
        elif not any((r or {}).get(key) for r in m.get("tasks") or []):
            print(f"\nзадача #{args.task} в этот срок не выполнялась" + (f" на {where}" if args.pc else ""))
    return 0


def cmd_stats(args) -> int:
    """The manager's own statistics: what the tasks used, how they ended, temperatures, by day and by agent."""
    from urllib.parse import urlencode
    c = Client(args.url)
    if args.file:  # one of the CSV files as it is (this PC's)
        sys.stdout.write(c.call("GET", f"/api/stats/files/{quote(args.file, safe='')}", timeout=120, raw=True))
        return 0
    q = {"period": args.period}
    if args.pc:
        q["pc"] = args.pc
    r = c.call("GET", "/api/stats?" + urlencode(q), timeout=90)
    if args.files:
        print("файлы статистики этого ПК (прочитать: rmq stats --file ИМЯ > копия.csv):")
        for f in r.get("files") or []:
            print(f"  {f['name']:<24} {f['size'] / 1024:>9.0f} КБ")
        return 0
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    num = lambda v, fmt=".1f": format(v or 0, fmt)
    hours = lambda m: f"{(m or 0) / 60:.1f} ч"
    period = {"24h": "24 часа", "7d": "7 дней", "30d": "30 дней", "all": "всё время"}[args.period]
    where = ("все мои ПК: " + ", ".join(x["pc"] for x in r["per_pc"])) if r.get("per_pc") else f"ПК {r.get('pc') or '—'}"
    t = r["total"]
    print(f"статистика за {period} ({where}); ведётся с {r.get('since') or '—'}")
    print(f"менеджер работал {hours(t['observed_min'])}, задачи выполнялись {hours(t['compute_min'])}")
    print(f"задачи агентов: процессор {num(t['tasks_cpu_core_h'])} ядро-ч (в среднем {num(t['tasks_cpu_pct'], '.0f')}% "
          f"процессора), видеокарта {num(t['tasks_gpu_h'])} ч, пик ОЗУ {num(t['tasks_ram_gb_max'])} ГБ")
    print(f"задачи: запущено {num(t['tasks_started'], '.0f')}, готово {num(t['tasks_done'], '.0f')}, "
          f"с ошибкой {num(t['tasks_failed'], '.0f')}, отменено {num(t['tasks_cancelled'], '.0f')}, "
          f"перезапущено {num(t['tasks_requeued'], '.0f')}, зависали {num(t['hung_tasks'], '.0f')}")
    temps = []
    for c, name in (("cpu", "процессор"), ("gpu", "видеокарта"), ("board", "плата"), ("ssd", "SSD")):
        if t.get(f"{c}_max_c") is not None:
            load = t.get(f"{c}_load_avg_c")
            temps.append(f"{name} до {t[f'{c}_max_c']:.0f}°C" + (f" (при расчётах в среднем {load:.0f}°C)" if load else ""))
    if temps:
        print("температура: " + "; ".join(temps))
    if t.get("tasks_energy_wh") or t.get("cpu_energy_wh"):
        tariff = r.get("tariff")
        rub = lambda wh: f" ≈ {wh / 1000 * tariff:.2f} ₽" if tariff else ""
        print(f"энергия (оценка): задачи {energy_text(t.get('tasks_energy_wh') or 0)}{rub(t.get('tasks_energy_wh') or 0)}; "
              f"весь процессор {energy_text(t.get('cpu_energy_wh') or 0)}, видеокарта {energy_text(t.get('gpu_energy_wh') or 0)}")
    if t.get("cpu_power_max_w") is not None:
        cpu = cpu_text(t.get("cpu_power_load_avg_w"), t.get("clock_p_load_ghz"), t.get("clock_e_load_ghz"))
        print(f"процессор: мощность до {t['cpu_power_max_w']:.0f} Вт" + (f"; во время расчётов в среднем {cpu}" if cpu else ""))
    print(f"ограничения: регулятор температуры {hours(t['regulator_min'])}, заморозка от перегрева "
          f"{hours(t['heat_frozen_min'])}, от нехватки места {hours(t['disk_frozen_min'])}; "
          f"игра владельца {hours(t['game_min'])}, пауза всех {hours(t['paused_min'])}")
    if r.get("per_pc"):
        print("\nпо ПК:")
        for x in r["per_pc"]:
            print(f"  {x['pc']:<18} процессор {num(x['tasks_cpu_core_h']):>7} ядро-ч, готово {num(x['tasks_done'], '.0f')}, "
                  f"с ошибкой {num(x['tasks_failed'], '.0f')}"
                  + (f", процессор до {x['cpu_max_c']:.0f}°C" if x.get("cpu_max_c") is not None else ""))
    print(f"\nпо {'часам' if r['bucket'] == 'hour' else 'дням'} (только где менеджер работал):")
    for b in r["buckets"]:
        if b.get("observed_min"):
            print(f"  {b['key']:<13} процессор {num(b['tasks_cpu_core_h']):>7} ядро-ч ({num(b['tasks_cpu_pct'], '.0f'):>3}%), "
                  f"готово {num(b['tasks_done'], '.0f')}, с ошибкой {num(b['tasks_failed'], '.0f')}"
                  + (f", процессор до {b['cpu_max_c']:.0f}°C" if b.get("cpu_max_c") is not None else ""))
    if r.get("agents"):
        print("\nпо агентам:")
        for a in r["agents"]:
            print(f"  {a['agent']:<44} процессор {num(a['cpu_core_h']):>7} ядро-ч, работа задач {num(a['task_h'])} ч, "
                  f"готово {num(a['done'], '.0f')}, с ошибкой {num(a['failed'], '.0f')}")
    for m in r.get("missing") or []:
        err(f"не вошло: {m}")
    return 0


def cmd_top(args) -> int:
    st = Client(args.url).call("GET", pc_path(args, "state"), timeout=60)
    if getattr(args, "pc", None) and not args.json:
        print(f"ПК {args.pc}:")
    if args.json:
        print(json.dumps({k: st.get(k) for k in ("mode", "game", "gpu_busy", "system", "thermal", "disk")},
                         ensure_ascii=False, indent=2))
        return 0
    s = st["system"]
    mode = {"auto": "авто", "game": "игровой (включён владельцем)", "paused": "пауза всех задач"}[st["mode"]]
    if st["game"]["active"] and st["mode"] == "auto":
        mode += f", владелец играет: {st['game']['source']}"
    print(f"режим: {mode}")
    cpu, ram, vram = s["cpu"], s["ram"], s["vram"]
    print(f"CPU : владелец {cpu['user']:.0f}%, задачи {cpu['tasks']:.0f}%; задачам доступно {cpu['pool_cores']:.1f} "
          f"из {cpu['n']} потоков, свободно {max(0, cpu['pool_cores'] - cpu['held']):.1f}")
    print(f"ОЗУ : владелец {ram['user']:.1f} ГБ, задачи {ram['tasks']:.1f} ГБ из {ram['total']:.1f}; "
          f"задачам доступно {ram['pool']:.1f} ГБ, свободно {max(0, ram['pool'] - ram['held']):.1f} ГБ")
    if vram:
        print(f"VRAM: владелец {vram['user']:.1f} ГБ, задачи {vram['tasks']:.1f} ГБ из {vram['total']:.1f}; "
              f"свободно задачам {max(0, vram['pool'] - vram['held']):.1f} ГБ"
              + (" — GPU занят владельцем, GPU-задачи ждут" if st["gpu_busy"] else ""))
    else:
        print("VRAM: видеокарта не найдена, GPU-задачи недоступны")
    c = s["commit"]
    if c["total"] > c["limit"] * c["max_pct"] / 100:
        print(f"ПОДКАЧКА: занято {c['total']:.0f} из {c['limit']:.0f} ГБ — новые задачи не стартуют")
    th = st.get("thermal") or {}
    temps = [f"{r['label']} {r['temp']:.0f}°C" for r in th.get("readings") or [] if r.get("temp") is not None]
    cpu = cpu_text(th.get("cpu_power"), th.get("clock_p"), th.get("clock_e"))
    gpu = ", ".join(x for x in (f"{th['gpu_power']:.0f} Вт" if th.get("gpu_power") is not None else "",
                                f"{th['gpu_clock']:.2f} ГГц" if th.get("gpu_clock") is not None else "") if x)
    if temps or cpu:
        print("датчики: " + ", ".join(temps) + (f"; процессор: {cpu}" if cpu else "") + (f"; видеокарта: {gpu}" if gpu else ""))
    pw = st.get("power") or {}
    if pw.get("mode") or pw.get("plan"):
        print(f"питание: {'от батареи' if pw.get('on_battery') else 'от сети'}"
              + (f", режим Windows «{pw['mode']}»" if pw.get("mode") else "")
              + (f", схема «{pw['plan']}»" if pw.get("plan") else ""))
    if th.get("enabled") and th.get("state") == "hot":
        print(f"ТЕМПЕРАТУРА: перегрев — {th['trigger']}; все задачи заморожены до остывания")
    elif th.get("enabled") and th.get("factor", 1) < 0.995:
        lim = th.get("limiting")
        print(f"температура: регулятор оставляет задачам {th['factor']:.0%} процессора"
              + (f" ({lim['label']} {lim['temp']:.0f}°C при цели {lim['target']:g}°C)" if lim and lim.get("temp") else ""))
    disk = st.get("disk") or {}
    for d in disk.get("drives", []):
        if d["system"] or d["low"]:
            print(f"диск {d['drive']} свободно {d['free']:.0f} ГБ из {d['total']:.0f}"
                  + (f" — МАЛО МЕСТА (минимум {disk['min_free_gb']:g} ГБ), задачи стоят" if d["low"] else ""))
    n = {k: sum(1 for t in st["tasks"] if t["state"] == k) for k in ("running", "suspended", "queued")}
    print(f"задачи: выполняются {n['running']}, на паузе {n['suspended']}, в очереди {n['queued']}")
    return 0


def cmd_whoami(args) -> int:
    name, info = agent_identity()
    print(f"агент: {name}  ({info})")
    try:
        me = Client(args.url).call("GET", "/api/whoami")
        print(f"лимиты: не больше {me['max_running']} задач одновременно и {me['max_queued']} в очереди, "
              f"максимальный приоритет {me['max_priority']}, вес {me['weight']:g}")
    except CliError as e:
        err(str(e))
        return e.code
    return 0


def cmd_guide(args) -> int:
    try:
        text = Client(args.url).call("GET", "/api/guide", raw=True, timeout=5)
    except CliError:  # the manager is down: the guide is right here
        sys.path.insert(0, str(ROOT))
        from rmq.install import render_guide
        text = render_guide()
    sys.stdout.write(text)
    return 0


def cmd_start(args) -> int:
    if alive(args.url):
        err(f"менеджер уже работает: {args.url}")
        return 0
    spawn_server(args.url)
    err(f"менеджер запущен в фоне: {args.url}")
    return 0


# ---------------------------------------------------------------- owner / launcher commands (hidden)
def message_box(text: str, title: str = "Менеджер ресурсов") -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:
        err(text)


def dashboard_url(url: str) -> str:
    tok = admin_token()
    return url + "/" + (f"#token={tok}" if tok else "")


def cmd_launch(args) -> int:
    """Desktop shortcut: start the manager if needed and open the dashboard."""
    import webbrowser
    try:
        if not alive(args.url):
            spawn_server(args.url)
        webbrowser.open(dashboard_url(args.url))
        return 0
    except CliError as e:
        message_box(f"Не удалось запустить менеджер ресурсов.\n\n{e}")
        return e.code


def cmd_dashboard(args) -> int:
    import webbrowser
    webbrowser.open(dashboard_url(args.url))
    print(args.url)
    return 0


def cmd_stop(args) -> int:
    c = Client(args.url, admin=True)
    r = c.call("POST", "/api/shutdown", {"tasks": args.tasks})
    deadline = time.time() + 15  # return only once it is gone, so that an immediate `rmq start` really starts it
    while alive(c.url) and time.time() < deadline:
        time.sleep(0.3)
    err(r.get("note", "менеджер остановлен"))
    return 0


def cmd_serve(args) -> int:
    sys.path.insert(0, str(ROOT))
    from rmq.server import serve
    return serve(home(), open_browser=args.open)


def cmd_install_shortcut(args) -> int:
    from rmq.install import install_shortcut
    print(install_shortcut(home()))
    return 0


def cmd_install_agents(args) -> int:
    from rmq.install import install_agent_rules
    print(install_agent_rules(remove=False))
    return 0


def cmd_uninstall_agents(args) -> int:
    from rmq.install import install_agent_rules
    print(install_agent_rules(remove=True))
    return 0


# ---------------------------------------------------------------- argument parsing
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rmq", description="Очередь тяжёлых вычислений на этом ПК. "
                                "Полное руководство для ИИ-агентов: rmq guide",
                                epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=default_url(), help=argparse.SUPPRESS)
    from . import __version__
    p.add_argument("--version", action="version", version=f"rmq {__version__}", help="версия менеджера")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="команда")

    def task_opts(sp):
        sp.add_argument("--cpu", type=float, help="потоков CPU, которые задача реально использует (по умолчанию 1)")
        sp.add_argument("--ram", help="пик ОЗУ с запасом ~20%%: 8, 8G, 512M (по умолчанию 2 ГБ)")
        sp.add_argument("--vram", help="видеопамять, ГБ; без неё задача не видит CUDA (CUDA_VISIBLE_DEVICES='')")
        sp.add_argument("--time", help="ожидаемая длительность: 30m, 2h, 1d (по умолчанию 1h); "
                                       "дольше заявленного — задачу легче вытеснить")
        sp.add_argument("--time-limit", dest="time_limit", help="жёсткий лимит: по истечении задача будет убита")
        sp.add_argument("--priority", choices=["low", "normal"], help="low — фоновая задача (по умолчанию normal)")
        sp.add_argument("--preempt", choices=["suspend", "requeue"],
                        help="если ресурсы нужны владельцу: suspend — заморозить (по умолчанию), "
                             "requeue — остановить и перезапустить позже (для задач с чекпоинтами)")
        sp.add_argument("--name", help="короткое название задачи для дашборда")
        sp.add_argument("--series", help="серия: из задач одной серии работает одна за раз, по порядку постановки")
        sp.add_argument("--after", type=int, metavar="ID",
                        help="запустить только после успешного окончания задачи ID (иначе эта будет отменена)")
        sp.add_argument("--cwd", help="рабочая папка (по умолчанию текущая)")
        sp.add_argument("--env", action="append", metavar="K=V", help="дополнительная переменная окружения")
        sp.add_argument("--no-env", action="store_true", help="не передавать своё окружение (PATH, conda, venv)")
        sp.add_argument("--shell", action="store_true", help="выполнить команду строкой через cmd.exe")
        sp.add_argument("--where", help="где выполнить: имя ПК из сети (rmq nodes), auto — где свободнее, "
                                        "по умолчанию этот ПК")
        sp.add_argument("--files", action="append", metavar="ШАБЛОН",
                        help="для --where: что из папки отправить (по умолчанию вся папка проекта без .venv, "
                             ".git и игнорируемого git)")
        sp.add_argument("--results", action="append", metavar="ШАБЛОН",
                        help="для --where: что вернуть (по умолчанию все созданные и изменённые задачей файлы)")
        sp.add_argument("--requirements", help="для --where: файл зависимостей pip (по умолчанию requirements.txt, "
                                               "если он есть)")
        sp.add_argument("command", nargs=argparse.REMAINDER, help="команда после --")

    sp = sub.add_parser("run", help="поставить задачу и ждать её с выводом лога (код выхода = код задачи)")
    task_opts(sp)
    sp.add_argument("--timeout", type=float, help="ждать не дольше N секунд (задача продолжит работу), код 124")
    sp.add_argument("--detach", action="store_true", help="по Ctrl+C не отменять задачу")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("submit", help="поставить задачу и сразу напечатать её номер")
    task_opts(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_submit)

    sp = sub.add_parser("wait", help="дождаться задачи или нескольких (код выхода = код задачи, 124 = таймаут)")
    sp.add_argument("id", type=int, nargs="+", help="номер задачи; несколько — ждать, пока закончатся все")
    sp.add_argument("--all", action="store_true", help="ждать все перечисленные задачи (так и по умолчанию)")
    sp.add_argument("--timeout", type=float, help="секунд; задача продолжит работу после таймаута")
    sp.add_argument("--log", action="store_true", help="печатать лог с начала")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(fn=cmd_wait)

    sp = sub.add_parser("logs", help="лог задачи (stdout+stderr)")
    sp.add_argument("id", type=int)
    sp.add_argument("-f", "--follow", action="store_true", help="следить до завершения")
    sp.add_argument("--tail", type=int, help="последние N строк")
    sp.add_argument("--pc", help="задача на другом ПК из «Моих ПК» владельца (номер — тамошний)")
    sp.set_defaults(fn=cmd_logs)

    sp = sub.add_parser("status", help="состояние задачи (без номера — вся очередь)")
    sp.add_argument("id", type=int, nargs="?")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--pc", help="другой ПК из «Моих ПК» владельца (номер задачи — тамошний)")
    sp.add_argument("--events", action="store_true", help="все события задачи: старт, паузы, перезапуски, сон ПК…")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("queue", aliases=["ls"], help="очередь и выполняющиеся задачи всех агентов")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--pc", help="очередь другого ПК из «Моих ПК» владельца (только чтение)")
    sp.set_defaults(fn=cmd_queue)

    sp = sub.add_parser("retime", help="поправить заявку времени задачи на ходу (видно владельцу в журнале)")
    sp.add_argument("id", type=int)
    sp.add_argument("--time", required=True, help="новая ожидаемая длительность от старта: 30m, 2h, 1d")
    sp.set_defaults(fn=cmd_retime)

    sp = sub.add_parser("past", help="прошлые запуски: время, пик ОЗУ, ядра — чтобы точнее заявить следующий")
    sp.add_argument("--name", help="часть названия задачи")
    sp.add_argument("--command", help="часть команды")
    sp.add_argument("--limit", type=int, default=20, help="сколько последних (по умолчанию 20; хранятся все)")
    sp.add_argument("--all-agents", dest="all_agents", action="store_true", help="не только мои задачи")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_past)

    sp = sub.add_parser("cancel", help="отменить свою задачу")
    sp.add_argument("id", type=int)
    sp.set_defaults(fn=cmd_cancel)

    sp = sub.add_parser("resume", help="продолжить свою замороженную задачу (например, помеченную «зависла»)")
    sp.add_argument("id", type=int)
    sp.set_defaults(fn=cmd_resume)

    sp = sub.add_parser("nodes", help="ПК вычислительной сети: кто на связи и сколько у него свободно")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_nodes)

    sp = sub.add_parser("top", help="ресурсы ПК и сколько сейчас доступно задачам")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--pc", help="другой ПК из «Моих ПК» владельца (только чтение)")
    sp.set_defaults(fn=cmd_top)

    sp = sub.add_parser("history", help="нагрузка по времени: весь ПК или одна задача (--task), по данным графиков")
    sp.add_argument("--range", default="1h", help="за какой срок: 15m, 1h, 6h, 24h, 7d, 30d (не больше месяца; "
                                                  "по умолчанию 1h)")
    sp.add_argument("--task", type=int, help="одна задача: сколько ядер и памяти она брала и колебалась ли нагрузка")
    sp.add_argument("--pc", help="другой ПК из «Моих ПК» владельца")
    sp.add_argument("--rows", type=int, default=12, help="на сколько отрезков разбить срок (по умолчанию 12)")
    sp.add_argument("--json", action="store_true", help="точки графиков как есть")
    sp.set_defaults(fn=cmd_history)

    sp = sub.add_parser("stats", help="статистика менеджера: нагрузка, задачи, температуры — по дням и по агентам")
    sp.add_argument("--period", choices=["24h", "7d", "30d", "all"], default="7d", help="за какой срок (по умолчанию 7d)")
    sp.add_argument("--pc", help="другой мой ПК по имени, или all — все мои ПК вместе")
    sp.add_argument("--json", action="store_true", help="всё как есть, в JSON")
    sp.add_argument("--files", action="store_true", help="какие CSV-файлы статистики есть (по часам, по агентам, "
                                                         "поминутно, по задачам)")
    sp.add_argument("--file", metavar="ИМЯ", help="вывести CSV-файл статистики как есть, например tasks.csv")
    sp.set_defaults(fn=cmd_stats)

    sp = sub.add_parser("whoami", help="каким агентом тебя видит менеджер и твои лимиты")
    sp.set_defaults(fn=cmd_whoami)

    sp = sub.add_parser("guide", help="полное руководство для ИИ-агентов")
    sp.set_defaults(fn=cmd_guide)

    sp = sub.add_parser("start", help="запустить менеджер в фоне, если он не запущен")
    sp.set_defaults(fn=cmd_start)

    # owner / launcher commands, not advertised to agents
    for name, fn in (("launch", cmd_launch), ("dashboard", cmd_dashboard), ("install-shortcut", cmd_install_shortcut),
                     ("install-agents", cmd_install_agents), ("uninstall-agents", cmd_uninstall_agents)):
        sub.add_parser(name).set_defaults(fn=fn)
    sp = sub.add_parser("stop")
    sp.add_argument("--tasks", choices=["freeze", "keep", "cancel"], default="freeze")
    sp.set_defaults(fn=cmd_stop)
    sp = sub.add_parser("serve")
    sp.add_argument("--open", action="store_true")
    sp.set_defaults(fn=cmd_serve)
    sp = sub.add_parser("net")
    sp.add_argument("action", choices=["status", "create", "invite", "join", "leave", "mode", "rename",
                                       "home-create", "home-invite", "home-leave", "home-main", "home-ext"])
    sp.add_argument("value", nargs="?")
    sp.set_defaults(fn=cmd_net)
    hidden = {"launch", "dashboard", "install-shortcut", "install-agents", "uninstall-agents", "stop", "serve", "net"}
    sub._choices_actions = [a for a in sub._choices_actions if a.dest not in hidden]
    return p


def run() -> int:
    """main() for the command line: a reader that stops early (rmq guide | head, | more) is not an error."""
    def quiet() -> None:  # the interpreter's last flush would complain about the closed pipe otherwise
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
    try:
        code = main()
    except BrokenPipeError:
        quiet()
        return 0
    try:
        sys.stdout.flush()
    except OSError:  # Windows says EINVAL for a pipe closed by its reader
        quiet()
    return code


def main(argv: list[str] | None = None) -> int:
    leave_stray_console()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    try:
        return args.fn(args)
    except CliError as e:
        err(f"ошибка: {e}")
        return e.code
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
