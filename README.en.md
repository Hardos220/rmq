# RMQ — a PC resource manager for AI agents

[![Release](https://img.shields.io/github/v/release/Hardos220/rmq)](https://github.com/Hardos220/rmq/releases/latest)
[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/license-PolyForm%20Noncommercial-blue)](LICENSE)
![Windows 10/11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D6)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB)

**Русский:** [README.md](README.md) (full documentation).

> **Language note.** The dashboard, the command-line messages and the guide for agents are in **Russian** only.
> AI agents read Russian without trouble; for a human the dashboard is mostly numbers and charts.

RMQ is a queue for heavy computations started by AI coding agents (Claude Code, Codex, Gemini CLI, Cursor and
others) on a Windows PC that **you keep using at the same time**. Agents run tasks only on the resources you are not
using: your games, browser and everything else you start always come first.

![The dashboard](docs/images/dashboard.png)

## Why

Agents increasingly train models, run simulations, sweep parameters and build large projects on their own. Without a
queue they take the whole PC, get in each other's way and in yours, and a laptop overheats. With RMQ an agent submits a
task with the `rmq` command; the manager starts it when there is room, squeezes or freezes it when you need the
resources, and keeps the hardware cool.

## Features

- **You always come first.** Tasks run at the lowest CPU, I/O and memory priority; when you need memory they are
  frozen (their memory is paged out) and resume where they stopped. While you play, GPU tasks pause and CPU tasks move
  to the efficiency cores.
- **Fair queue** with priorities, fair sharing between agents, series of tasks and dependencies (`--after`).
- **Agents set themselves up.** A short block of rules goes into the agents' global instructions; an agent registers
  on its first command — no keys.
- **Hardware care.** A smooth temperature regulator for the CPU, the motherboard (ACPI), the GPU and the SSD; freezing
  on overheating or when the system disk runs out of space.
- **Dashboard** at `http://127.0.0.1:8777`: CPU, RAM, VRAM and GPU load split into "you" and "agents", per-task
  resources, temperatures, CPU/GPU power and clocks, a forecast of the queue, statistics and a timeline of tasks.
- **Compute network.** Several PCs with RMQ join through [Tailscale](https://tailscale.com); agents send tasks where
  there is room. Friends' tasks run with low priority and low integrity (they cannot change your files).
- **Updates itself** from the GitHub releases (SHA-256 checked); tasks survive restarts and updates.

## Quick start

You need Windows 10 or 11 and Python 3.10 or newer ([no Python?](#if-python-is-not-installed)).

1. **Download.** Open the [latest release](https://github.com/Hardos220/rmq/releases/latest) and under **Assets**
   click **`rmq-<version>.zip`** (not "Source code").
2. **Unpack.** Right-click the downloaded archive → "Extract All…" → type `C:\` as the destination → "Extract".
   You get the folder `C:\rmq-<version>`.
3. **Run.** In that folder double-click `install.cmd`. If Windows asks whether to run the file, click "Run"; if it
   shows "Windows protected your PC", click "More info" → "Run anyway". In a minute the dashboard opens.

Done. Agents (Claude Code, Codex CLI, Gemini CLI, Windsurf) learn about the manager in their next session and put
heavy computations into the queue themselves. Later, open the manager with the "Менеджер ресурсов" desktop shortcut.

## Installation details

### If Python is not installed

1. Open [python.org/downloads](https://www.python.org/downloads/) and click **Download Python 3.x**.
2. Run the installer and **tick "Add python.exe to PATH" on its first screen** — otherwise the manager cannot find
   Python. Then click "Install Now".
3. Run `install.cmd` again.

To check: Win+R → `cmd` → `python --version`. If `install.cmd` cannot find Python, it says so.

### The manager's folder

The manager runs from the folder you unpacked it to, so do not leave it in Downloads or a temporary folder. You may
rename the folder (e.g. to `C:\RMQ`) before running `install.cmd`; if you move or rename it later, run `install.cmd`
again. Updates install into the same folder by themselves.

### What the installer changes on your PC

Only this:

- a `.venv` folder with a Python environment inside the manager's folder (one package: psutil);
- a desktop shortcut;
- a block of rules between `<!-- rmq:begin … -->` and `<!-- rmq:end -->` markers in the global instructions of the
  agents that are already installed: `%USERPROFILE%\.claude\CLAUDE.md` (Claude Code), `.codex\AGENTS.md` (Codex CLI),
  `.gemini\GEMINI.md` (Gemini CLI), `.codeium\windsurf\memories\global_rules.md` (Windsurf). Your own text there is
  left as it is. For Cursor, copy the rules from the "Агенты" (Agents) tab into Cursor Settings → Rules.

No services, drivers, autostart or registry changes, and no administrator rights. Settings, the task database, logs
and statistics live in the `data` folder inside the manager's folder.

### Try it by hand

Put a test task into the queue (the path is your manager's folder):

```bat
C:\rmq-<version>\bin\rmq.cmd run --cpu 1 --ram 0.5 --time 1m -- python -c "print('hello from the queue')"
```

Optional: the CPU temperature sensor ([tools/README.md](tools/README.md), one UAC prompt), autostart (copy the shortcut
into `shell:startup`), a network of several PCs (the "Сеть" tab has step-by-step instructions).

## Commands for agents

```bat
rmq top                                                      :: what is free right now
rmq run --cpu 4 --ram 8 --time 10m -- python analyze.py      :: short tasks: waits and prints the log
rmq submit --cpu 8 --ram 16 --vram 6 --time 3h --preempt requeue --name train -- python train.py
rmq wait 12 --timeout 540                                    :: 0 done, 124 still running
rmq status 12                                                :: state, why it waits, forecast, progress
rmq logs 12 --tail 50
rmq guide                                                    :: the full guide for agents (AGENTS.md)
```

## Uninstall

1. In the dashboard: "Выключить" (Shut down) → cancel the tasks.
2. Remove the agents' rules: in the manager's folder run `bin\rmq.cmd uninstall-agents`.
3. If you installed the CPU temperature sensor: `tools\uninstall_cpu_sensors.ps1` as administrator.
4. If you pressed "Разрешить в брандмауэре" (allow in the firewall): delete the inbound rule
   "RMQ resource manager (network)" in Windows Defender Firewall → Advanced settings.
5. Delete the shortcut, the manager's folder and `%USERPROFILE%\AppData\LocalLow\RMQ` (it exists only if tasks came
   from the network).

## Privacy and security

- The dashboard and the API listen on `127.0.0.1` only; agents never get the dashboard's token.
- The only outgoing connections are update checks to GitHub (can be turned off in the settings). No telemetry.
- The compute network, if you enable it, listens only on this PC's Tailscale address; every request between PCs is
  signed with the network's key (HMAC-SHA256 with a timestamp and a nonce).
- Report a vulnerability: [SECURITY.md](SECURITY.md).

## FAQ

- **Linux or macOS?** No, Windows 10/11 only (Job Objects, performance counters, I/O priorities).
- **Which GPU?** Any or none. Load and VRAM come from Windows for any GPU; temperature, power and clocks are read for
  NVIDIA only.
- **Which agents?** Any agent that can run terminal commands; agents without a shell can use the HTTP API (section 16
  of [AGENTS.md](AGENTS.md)).
- **Can an agent bypass the queue?** Technically yes — the rules ask agents to use it; the manager cannot forbid an
  agent to start programs directly.

## Contributing and license

Issues and pull requests are welcome: [CONTRIBUTING.md](CONTRIBUTING.md). Changes in each version:
[CHANGELOG.md](CHANGELOG.md).

Licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE): free for any noncommercial purpose — personal
projects, research, study, hobbies, noncommercial and educational organizations. **Commercial use requires a separate
license** — write to the author: [maksimelisov2003@gmail.com](mailto:maksimelisov2003@gmail.com).
When you pass the software on, include the license terms and the "Required Notice" line from [NOTICE](NOTICE).
Versions up to and including 1.8.2 were released under the Apache License 2.0 and stay under it for those copies.
Third-party components of the release archive: [THIRD_PARTY.md](THIRD_PARTY.md).
