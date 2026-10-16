"""Integration with the desktop and with AI tools: a desktop shortcut and a short block of rules
in the global instruction files of installed agents (Claude Code, Codex, Gemini, Windsurf).

Both are plain user-level files, added between markers and removable with `rmq uninstall-agents`.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BEGIN = "<!-- rmq:begin — блок добавлен менеджером ресурсов ПК; удалить: rmq uninstall-agents -->"
END = "<!-- rmq:end -->"

# (tool, config folder that proves the tool is installed, instruction file relative to it)
AGENT_FILES = [
    ("Claude Code", Path.home() / ".claude", "CLAUDE.md"),
    ("Codex CLI", Path.home() / ".codex", "AGENTS.md"),
    ("Gemini CLI", Path.home() / ".gemini", "GEMINI.md"),
    ("Windsurf", Path.home() / ".codeium" / "windsurf", "memories/global_rules.md"),
]


def server_url() -> str:
    port = 8777
    try:
        port = int(json.loads((ROOT / "data" / "settings.json").read_text(encoding="utf-8"))["server"]["port"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return f"http://127.0.0.1:{port}"


def hardware_line() -> str:
    """This PC's hardware in one phrase, for the agents' guide (every installation has its own)."""
    try:
        import psutil
        from .winapi import cpu_sets, dxgi_adapters
        threads = os.cpu_count() or 1
        sets = cpu_sets()
        classes = sorted({c["efficiency"] for c in sets})
        cores = ""
        if len(classes) > 1:
            e = sum(1 for c in sets if c["efficiency"] == classes[0])
            cores = f" ({threads - e} на P-ядрах, {e} на E-ядрах)"
        ram = psutil.virtual_memory().total / 2**30
        gpus = sorted((a for a in dxgi_adapters() if a["vram"] > 512 * 2**20), key=lambda a: -a["vram"])
        gpu = f"видеокарта {gpus[0]['name']} на {gpus[0]['vram'] / 2**30:.0f} ГБ" if gpus else "дискретной видеокарты нет"
        return f"{threads} потоков CPU{cores}, {ram:.0f} ГБ ОЗУ, {gpu}"
    except Exception:
        return "смотри `rmq top`"


def _fill(text: str) -> str:
    """Put this installation's real paths, address and hardware into a template."""
    sh = "/" + str(ROOT).replace("\\", "/").replace(":", "", 1)  # C:\x\y -> /C/x/y (Git Bash)
    sh = sh[:2].lower() + sh[2:] + "/bin/rmq"
    cmd = str(ROOT / "bin" / "rmq.cmd")
    if " " in sh:  # a folder with spaces: agents copy the command as is, so it has to be quoted already
        sh, cmd = f'"{sh}"', f'"{cmd}"'
    if "{HARDWARE}" in text:
        text = text.replace("{HARDWARE}", hardware_line())
    return (text.replace("{RMQ_SH}", sh).replace("{RMQ_CMD}", cmd)
            .replace("{URL}", server_url())).strip() + "\n"


def render_rules() -> str:
    """The short block that goes into agents' global instruction files."""
    return _fill((ROOT / "agent_rules.md").read_text(encoding="utf-8")).strip()


def render_guide() -> str:
    """The full guide for AI agents (`rmq guide`)."""
    return _fill((ROOT / "AGENTS.md").read_text(encoding="utf-8"))


def _strip_block(text: str) -> str:
    start = text.find(BEGIN)
    if start < 0:
        start = text.find("<!-- rmq:begin")
    if start < 0:
        return text
    end = text.find(END, start)
    end = len(text) if end < 0 else end + len(END)
    return (text[:start].rstrip() + "\n\n" + text[end:].lstrip()).strip() + "\n"


def install_agent_rules(remove: bool = False, only_present: bool = False) -> str:
    """Add (or remove) the rules block; `only_present` just refreshes blocks that are already there."""
    rules = render_rules()
    lines = []
    for tool, folder, rel in AGENT_FILES:
        if not folder.is_dir():
            continue
        path = folder / rel
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        if only_present and "<!-- rmq:begin" not in old:
            continue
        new = _strip_block(old) if old else ""
        if not remove:
            new = (new.rstrip() + "\n\n" if new.strip() else "") + f"{BEGIN}\n{rules}\n{END}\n"
        if new.strip() == old.strip():
            lines.append(f"{tool}: без изменений ({path})")
            continue
        if remove and not new.strip() and old.strip().startswith("<!-- rmq:begin"):
            path.unlink()
            lines.append(f"{tool}: файл удалён, в нём были только правила менеджера ({path})")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new, encoding="utf-8")
        lines.append(f"{tool}: {'правила убраны' if remove else 'правила обновлены' if only_present else 'правила добавлены'} ({path})")
    if not remove:
        lines.append("Cursor: глобальные правила у него хранятся в настройках — вставьте текст из дашборда "
                     "(вкладка «Агенты» → «Правила для Cursor») в Cursor Settings → Rules → User Rules.")
    return "\n".join(lines) or "Не найдено ни одного установленного ИИ-инструмента"


def desktop_folder() -> Path:
    """The user's real Desktop folder (it may live in OneDrive)."""
    try:
        import ctypes
        import uuid
        shell32, ole32 = ctypes.WinDLL("shell32"), ctypes.WinDLL("ole32")
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        shell32.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                                 ctypes.POINTER(ctypes.c_wchar_p)]
        guid = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID("B4BFCC3A-DB2C-424C-B029-7FE99A87C641").bytes_le)
        out = ctypes.c_wchar_p()
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0 and out.value:
            path = out.value
            ole32.CoTaskMemFree(out)
            return Path(path)
    except (OSError, AttributeError, ValueError):
        pass
    return Path.home() / "Desktop"


SHORTCUT = "Менеджер ресурсов.lnk"
LAUNCHER = "Менеджер ресурсов.cmd"   # made instead of the shortcut where PowerShell may not create one


def _shortcut_mark(home: Path | None) -> Path:
    return Path(home or ROOT / "data") / "shortcut.txt"


def ensure_shortcut(home: Path | None = None) -> str | None:
    """Once per installation: make the desktop shortcut if this installation never got one (version 1.1.1 could
    fail at it silently). A shortcut the owner deletes later is not brought back."""
    mark = _shortcut_mark(home)
    if mark.exists():
        return None
    desk = desktop_folder()
    for name in (SHORTCUT, LAUNCHER):
        if (desk / name).exists():
            mark.parent.mkdir(parents=True, exist_ok=True)
            mark.write_text(str(desk / name), encoding="utf-8")
            return None
    return install_shortcut(home)


def install_shortcut(home: Path | None = None) -> str:
    pyw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not pyw.exists():
        raise SystemExit(f"сначала подготовьте окружение: {ROOT / 'setup.cmd'}")

    def q(p) -> str:  # single-quoted PowerShell string
        return "'" + str(p).replace("'", "''") + "'"

    desk = desktop_folder()
    lnk, launcher = desk / SHORTCUT, desk / LAUNCHER
    mark = _shortcut_mark(home)
    mark.parent.mkdir(parents=True, exist_ok=True)
    script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$path = {q(lnk)}
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
$s.TargetPath = {q(pyw)}
$s.Arguments = {q('"' + str(ROOT / 'rmq_cli.py') + '" launch')}
$s.WorkingDirectory = {q(ROOT)}
$s.IconLocation = {q(str(ROOT / 'rmq' / 'static' / 'rmq.ico') + ',0')}
$s.Description = 'Менеджер ресурсов ПК: запускает очередь задач агентов и открывает дашборд'
$s.Save()
"""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, encoding="utf-8", errors="replace", timeout=60,
                           creationflags=0x08000000)  # CREATE_NO_WINDOW: the manager itself calls it after an update
        why = (r.stderr or "").strip().splitlines()[:1]
    except (OSError, subprocess.TimeoutExpired) as e:
        why = [str(e)]
    if lnk.exists():
        if launcher.exists() and "rmq_cli.py" in launcher.read_text(encoding="utf-8", errors="replace"):
            launcher.unlink()  # a launcher left from an earlier install that could not make the shortcut
        mark.write_text(str(lnk), encoding="utf-8")
        return f"ярлык создан: {lnk}"
    # PowerShell could not make a shortcut (a security policy, an antivirus): a small launcher file does the same
    try:
        launcher.write_bytes((f'@echo off\r\nchcp 65001 >nul\r\nstart "" "{pyw}" "{ROOT / "rmq_cli.py"}" launch\r\n')
                             .encode("utf-8"))
    except OSError as e:
        raise SystemExit(f"не удалось создать ни ярлык, ни файл запуска на рабочем столе ({e}); "
                         f"открывайте менеджер файлом start.cmd в папке {ROOT}") from None
    mark.write_text(str(launcher), encoding="utf-8")
    return (f"ярлык создать не удалось ({why[0] if why else 'причина неизвестна'}), "
            f"вместо него на рабочем столе файл запуска: {launcher}")
