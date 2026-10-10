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
    if "{HARDWARE}" in text:
        text = text.replace("{HARDWARE}", hardware_line())
    return (text.replace("{RMQ_SH}", sh).replace("{RMQ_CMD}", str(ROOT / "bin" / "rmq.cmd"))
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


def install_shortcut() -> str:
    pyw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not pyw.exists():
        raise SystemExit(f"сначала подготовьте окружение: {ROOT / 'setup.cmd'}")

    def q(p) -> str:  # single-quoted PowerShell string
        return "'" + str(p).replace("'", "''") + "'"

    script = f"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$desk = [Environment]::GetFolderPath('Desktop')
$path = Join-Path $desk 'Менеджер ресурсов.lnk'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
$s.TargetPath = {q(pyw)}
$s.Arguments = {q('"' + str(ROOT / 'rmq_cli.py') + '" launch')}
$s.WorkingDirectory = {q(ROOT)}
$s.IconLocation = {q(str(ROOT / 'rmq' / 'static' / 'rmq.ico') + ',0')}
$s.Description = 'Менеджер ресурсов ПК: запускает очередь задач агентов и открывает дашборд'
$s.Save()
Write-Output $path
"""
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"не удалось создать ярлык: {r.stderr.strip()}")
    return f"ярлык создан: {r.stdout.strip()}"
