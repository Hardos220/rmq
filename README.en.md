# RMQ — a PC resource manager for AI agents

[![Release](https://img.shields.io/github/v/release/Hardos220/rmq)](https://github.com/Hardos220/rmq/releases/latest)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
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

**You need** Windows 10 or 11 (64-bit) and [Python 3.10+](https://www.python.org/downloads/) — tick
"Add python.exe to PATH" when installing it. No administrator rights are needed.

1. Download `rmq-<version>.zip` from [Releases](https://github.com/Hardos220/rmq/releases/latest), unpack it into
   `C:\` and rename the folder `rmq-<version>` to `RMQ`. Any other permanent folder works too (not Downloads or a
   temporary one: the manager runs from there); the examples below use `C:\RMQ`.
2. Run `install.cmd`. It prepares a private Python environment, creates a desktop shortcut, adds the rules for your
   agents, starts the manager and opens the dashboard.
3. Ask your agent to do something heavy in any project — it reads the rules and submits the task through `rmq`. Or try
   it by hand:

   ```bat
   C:\RMQ\bin\rmq.cmd run --cpu 1 --ram 0.5 --time 1m -- python -c "print('hello from the queue')"
   ```

**What the installer changes on your PC** — only this:

- a `.venv` folder with a Python environment inside the manager's folder (one package: psutil);
- a desktop shortcut;
- a block of rules between `<!-- rmq:begin … -->` and `<!-- rmq:end -->` markers in the global instructions of the
  agents that are already installed: `%USERPROFILE%\.claude\CLAUDE.md` (Claude Code), `.codex\AGENTS.md` (Codex CLI),
  `.gemini\GEMINI.md` (Gemini CLI), `.codeium\windsurf\memories\global_rules.md` (Windsurf). Your own text there is
  left as it is. For Cursor, copy the rules from the "Агенты" (Agents) tab into Cursor Settings → Rules.

No services, drivers, autostart or registry changes. Settings, the task database, logs and statistics live in the
`data` folder inside the manager's folder.

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
2. Remove the agents' rules: `C:\RMQ\bin\rmq.cmd uninstall-agents`.
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
[CHANGELOG.md](CHANGELOG.md). Licensed under the [Apache License 2.0](LICENSE); keep the [NOTICE](NOTICE) when you
redistribute. Third-party components of the release archive: [THIRD_PARTY.md](THIRD_PARTY.md).
