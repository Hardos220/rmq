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
    for field in ("name", "cpu", "ram", "vram", "time", "time_limit", "priority", "preempt"):
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


def follow(c: Client, tid: int, *, stream_log: bool, timeout: float | None, quiet: bool = False) -> int:
    """Wait for the task, optionally streaming its log; returns its exit code (124 on timeout)."""
    offset = 0
    last_status = None
    started = time.monotonic()
    while True:
        t = c.call("GET", f"/api/tasks/{tid}")
        if stream_log:
            while True:
                chunk = c.call("GET", f"/api/tasks/{tid}/log?offset={offset}&limit=262144")
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
    return follow(Client(args.url), args.id, stream_log=args.log, timeout=args.timeout, quiet=args.quiet)


def cmd_logs(args) -> int:
    c = Client(args.url)
    tail_bytes = 1 << 20
    if args.follow:
        t = c.call("GET", f"/api/tasks/{args.id}")
        offset = max(0, t.get("log_size", 0) - tail_bytes) if args.tail is not None else 0
        while True:
            chunk = c.call("GET", f"/api/tasks/{args.id}/log?offset={offset}&limit=262144")
            if chunk["data"]:
                sys.stdout.write(chunk["data"])
                sys.stdout.flush()
            offset = chunk["offset"]
            if not chunk["data"]:
                t = c.call("GET", f"/api/tasks/{args.id}")
                if t["state"] in FINAL:
                    err(summary(t))
                    return exit_code_of(t)
                time.sleep(1.0)
    start = -tail_bytes if args.tail is not None else 0
    chunk = c.call("GET", f"/api/tasks/{args.id}/log?offset={start}&limit=1048576")
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
            print(f"{head}: не в сети")
            continue
        state = "принимает задачи" if n["accepting"] else ("только для себя" if n.get("mode") == "one_way" else "закрыт")
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
    err(f"задача #{r['id']} отменена")
    return 0


def cmd_resume(args) -> int:
    r = Client(args.url).call("POST", f"/api/tasks/{args.id}/resume")
    err(f"задача #{r['id']}: {r.get('note', 'возобновится')}")
    return 0


def cmd_status(args) -> int:
    c = Client(args.url)
    if args.id is None:
        return cmd_queue(args)
    t = c.call("GET", f"/api/tasks/{args.id}")
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
    if t.get("reason") == "hung" and t["state"] == "suspended":
        print(f"  что делать: посмотри rmq logs {t['id']}; продолжить — rmq resume {t['id']}, отменить — rmq cancel {t['id']}")
    print(f"  время работы: {fmt_dur(t['run_s'])}" + (" (дольше заявленного)" if t.get("overtime") else "")
          + (f", перезапусков: {t['restarts']}" if t.get("restarts") else ""))
    if t["state"] in FINAL:
        print(f"  итог: {summary(t)}")
    return 0


def cmd_queue(args) -> int:
    st = Client(args.url).call("GET", "/api/state")
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
                 if t["state"] == "running" else describe_wait(t))
        print(f"#{t['id']:<5} {t['state_text']:<12} {t['agent'][:24]:<24} {t['name'][:28]:<28} {extra}")
    return 0


def cmd_top(args) -> int:
    st = Client(args.url).call("GET", "/api/state")
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

    sp = sub.add_parser("wait", help="дождаться задачи (код выхода = код задачи, 124 = таймаут)")
    sp.add_argument("id", type=int)
    sp.add_argument("--timeout", type=float, help="секунд; задача продолжит работу после таймаута")
    sp.add_argument("--log", action="store_true", help="печатать лог с начала")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(fn=cmd_wait)

    sp = sub.add_parser("logs", help="лог задачи (stdout+stderr)")
    sp.add_argument("id", type=int)
    sp.add_argument("-f", "--follow", action="store_true", help="следить до завершения")
    sp.add_argument("--tail", type=int, help="последние N строк")
    sp.set_defaults(fn=cmd_logs)

    sp = sub.add_parser("status", help="состояние задачи (без номера — вся очередь)")
    sp.add_argument("id", type=int, nargs="?")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("queue", aliases=["ls"], help="очередь и выполняющиеся задачи всех агентов")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_queue)

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
    sp.set_defaults(fn=cmd_top)

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


def main(argv: list[str] | None = None) -> int:
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
