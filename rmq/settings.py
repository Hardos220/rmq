"""Settings: built-in defaults merged with data/settings.json (also editable from the dashboard)."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

DEFAULTS: dict = {
    "server": {
        "host": "127.0.0.1",
        "port": 8777,
    },
    # What is always kept free for the user. "pct_of_free" is taken from what is
    # left after the OS and the user's apps, e.g. 5% of (32 - 8 - 10) GB.
    "reserve": {
        "cpu_pct_of_free": 5.0,
        "cpu_min_cores": 1.0,
        "ram_pct_of_free": 5.0,
        "ram_min_gb": 1.0,
        "vram_pct_of_free": 5.0,
        "vram_min_gb": 0.5,
        "commit_max_pct": 90.0,       # don't start tasks past this share of the commit limit
        "commit_critical_pct": 96.0,  # restart tasks from scratch past this (protects the OS)
    },
    # RAM/VRAM: the user's load is its maximum over a sliding window, so the pool shrinks at once
    # and grows back only after the window passes. CPU: the average over the window (tasks run at
    # idle priority, so the user's programs get the CPU instantly anyway).
    "smoothing": {
        "cpu_window_s": 10,
        "ram_window_s": 60,
        "vram_window_s": 60,
    },
    "gpu": {
        "adapter": "auto",            # "auto" = the discrete GPU with most VRAM, or a name substring
        "user_busy_pct": 25.0,        # user's GPU load above this pauses GPU tasks
        "busy_confirm_s": 3,
        "idle_confirm_s": 60,
        "hide_gpu_for_cpu_tasks": True,  # CUDA_VISIBLE_DEVICES="" when a task asked for no VRAM
        "free_vram_for_user": True,      # frozen GPU tasks still holding VRAM the user needs are restarted later
    },
    "game_mode": {
        "auto_detect": True,
        "detect_fullscreen": True,
        "pause_gpu_tasks": True,
        "cpu_cores": "efficiency",    # "efficiency" = E-cores (or half the threads on CPUs without them); "all"
        "eco_qos": True,
        "learn_game_memory": True,    # remember how much RAM/VRAM each game took and free it at its next start
        "extra_ram_reserve_gb": 2.0,  # freed at the start of a game the manager has not seen yet
        "release_after_s": 30,
        "path_patterns": [
            "\\steamapps\\common\\",
            "\\epic games\\",
            "\\gog galaxy\\games\\",
            "\\xboxgames\\",
            "\\riot games\\",
            "\\ubisoft game launcher\\games\\",
            "\\ea games\\",
        ],
        "ignore_path_patterns": [
            "\\epic games\\launcher\\",
            "\\epic games\\epic online services\\",
            "\\riot games\\riot client\\",
            "\\steamapps\\common\\steamworks shared\\",
            "\\steamapps\\common\\wallpaper_engine\\",
        ],
        "process_names": [],
        "ignore_names": [
            "unitycrashhandler64.exe", "unitycrashhandler32.exe", "crashreportclient.exe",
            "easyanticheat.exe", "easyanticheat_eos.exe", "steamerrorreporter.exe",
            "steamerrorreporter64.exe", "soundpad.exe",
        ],
    },
    # Temperatures: the regulator smoothly cuts the tasks' CPU to keep every component at or below its
    # target; "hot" freezes every task until all components are back at their normal temperature and
    # stay there for `cool_confirm_s`.
    "thermal": {
        "enabled": True,
        "regulate": True,
        # general limits that suit most desktops and laptops; tighten them for your own PC in the dashboard
        # the CPU die (through LibreHardwareMonitor + the PawnIO driver, see tools/README.md)
        "cpu_target_c": 85.0, "cpu_hot_c": 97.0, "cpu_normal_c": 75.0,
        # the ACPI thermal zone: on laptops usually a board sensor that follows the whole load slowly
        "board_target_c": 75.0, "board_hot_c": 85.0, "board_normal_c": 70.0,
        "gpu_target_c": 83.0, "gpu_hot_c": 90.0, "gpu_normal_c": 65.0,
        "ssd_target_c": 65.0, "ssd_hot_c": 75.0, "ssd_normal_c": 50.0,
        "min_cpu_factor": 0.2,        # the regulator never leaves tasks less than this share of their CPU
        "hot_confirm_s": 10,
        "cool_confirm_s": 120,
        "freeze_on_throttling": True,
    },
    # Less free space than this on the system drive freezes every task (Windows itself needs the room);
    # on another drive, only the tasks working there. They go on once there is 2 GB more again.
    "disk": {
        "enabled": True,
        "min_free_gb": 10.0,
    },
    # The compute network (see rmq/net.py): PCs that lend each other spare capacity over Tailscale.
    "network": {
        "port": 8778,
        "bind": "tailscale",          # where members reach this PC: "tailscale" or an address of this PC
        "remote_weight": 0.5,         # members' agents share the CPU with half the weight of your own
        "max_hosted_tasks": 8,        # members' tasks here at once (queued, running or frozen)
        "max_bundle_mb": 1024,        # the largest project a member may send
        "max_results_mb": 2048,       # the most a task may send back
        "max_disk_gb": 20.0,          # all members' files on this PC together (code, results, environments)
        "protect_host": True,         # members' tasks may read this PC's files but never change them
        "keep_results_h": 72,         # results nobody collected are deleted after this time
    },
    # Updates come from the author's GitHub releases; a developer checkout (with .git) never updates itself.
    "update": {
        "auto": True,
        "check_every_h": 6,
    },
    "power": {
        "keep_awake_while_running": True,   # no idle sleep while tasks run (the display may still turn off)
        "keep_awake_on_battery": False,     # on battery the PC may sleep so the computation does not drain it
        "tariff_rub_kwh": 8.90,             # the price of electricity, for the statistics' energy in rubles
    },
    "scheduler": {
        "tick_s": 0.5,
        "cpu_squeeze_pct": 25.0,      # running tasks' CPU may shrink this much in total to let new tasks in
        "settle_s": 10,
        "short_task_max_min": 30,
        "preempt_after_short_min": 2,
        "preempt_after_min": 10,
        "min_slice_min": 20,
        "reservation_after_min": 20,
        "starvation_after_min": 60,
        "fair_preemption": True,
        "share_tolerance_pct": 5.0,
        "overtime_grace_pct": 10.0,
        "usage_half_life_h": 12.0,
        "overtime_usage_mult": 2.0,
        "age_full_h": 4.0,
        "w_priority": 1000.0,
        "w_fairshare": 500.0,
        "w_age": 300.0,
        "w_short": 150.0,
        "w_started": 100.0,
        "ramp_s": 90,                 # a started task's CPU grows to its claim over this time (0 = at once)
        "ramp_start_pct": 30.0,       # ... starting from this part of its claim
    },
    "tasks": {
        "default_cpu": 1.0,
        "default_ram_gb": 2.0,
        "default_vram_gb": 0.0,
        "default_time": "1h",
        "default_priority": "normal",
        "default_preempt": "suspend",
        "set_thread_env": True,
        "low_io_priority": True,      # task processes get the lowest disk priority
        "log_max_mb": 256,
        "keep_finished_days": 365,    # finished tasks with their logs, and the journal of events
        "orphan_grace_s": 10,
        "stop_grace_s": 60,           # a --preempt requeue task may save a checkpoint this long (0 = stop at once)
        "hung_after_min": 30,         # no CPU, disk, log or progress activity this long freezes a task as hung (0 = never)
    },
    "agents": {
        "default_weight": 1.0,
        "default_max_running": 4,
        "default_max_queued": 100,
        "default_max_priority": "normal",
        # the owner's code word: an agent that names it (rmq submit --priority high --code ...) gets high priority.
        # Empty: no agent can. Each owner sets his own; it stays in data/settings.json on this PC.
        "priority_code": "",
    },
}


# Presets: ready sets of values for the main knobs. Applying one sets every knob any preset touches (to the preset's
# value, or the default where it says nothing); everything else stays as the owner set it.
PRESETS: dict[str, tuple[str, str, dict]] = {
    "standard": ("Стандартный", "Задачам — всё свободное, обычные цели температуры. Подходит большинству ПК.", {}),
    "gentle": ("Бережный к железу",
               "Цели температуры ниже (процессор 70°, плата 65°, видеокарта 75°, SSD 55°): ПК меньше греется и "
               "меньше изнашивается, а при нагреве расчёты идут медленнее.",
               {"thermal": {"cpu_target_c": 70.0, "cpu_hot_c": 95.0, "cpu_normal_c": 70.0,
                            "board_target_c": 65.0, "board_hot_c": 80.0, "board_normal_c": 65.0,
                            "gpu_target_c": 75.0, "gpu_hot_c": 87.0, "gpu_normal_c": 60.0,
                            "ssd_target_c": 55.0}}),
    "quiet": ("Тихий",
              "Пока вы работаете за ПК: задачам примерно 60% свободного процессора, больше запаса памяти и цели "
              "температуры чуть ниже — вентиляторы тише, ПК отзывчивее.",
              {"reserve": {"cpu_pct_of_free": 40.0, "cpu_min_cores": 2.0, "ram_pct_of_free": 15.0, "ram_min_gb": 3.0},
               "thermal": {"cpu_target_c": 75.0, "board_target_c": 70.0, "gpu_target_c": 78.0, "ssd_target_c": 60.0},
               "gpu": {"user_busy_pct": 15.0}}),
    "max": ("Максимум расчётов",
            "Задачам — почти всё свободное. Ваши программы всё равно первые (задачи на самом низком приоритете), "
            "температура — по обычным правилам.",
            {"reserve": {"cpu_pct_of_free": 2.0, "ram_pct_of_free": 3.0},
             "gpu": {"user_busy_pct": 40.0}}),
}
PRESET_KEYS = sorted({(sec, key) for _t, _d, over in PRESETS.values() for sec, vals in over.items() for key in vals})


def preset_values(name: str) -> dict:
    over = PRESETS[name][2]
    out: dict = {}
    for sec, key in PRESET_KEYS:
        out.setdefault(sec, {})[key] = over.get(sec, {}).get(key, DEFAULTS[sec][key])
    return out


def active_preset(data: dict) -> str | None:
    for name in PRESETS:
        vals = preset_values(name)
        if all(data[sec][key] == v for sec, keys in vals.items() for key, v in keys.items()):
            return name
    return None


# settings.json keeps only what the owner changed, so new defaults reach existing installs.
# Older files stored every value; these defaults changed since and are dropped if left untouched.
VERSION = 3
MIGRATIONS = {1: [("game_mode", "extra_ram_reserve_gb", 0.0)]}
# version 3: the "warm" step (half the CPU) became a smooth regulator with a target temperature;
# an owner's own "warm" limit becomes the target, an untouched old default gives way to the new one
RENAMED = {2: [("thermal", "cpu_warm_c", "cpu_target_c", 85.0), ("thermal", "board_warm_c", "board_target_c", 72.0),
               ("thermal", "gpu_warm_c", "gpu_target_c", 80.0), ("thermal", "ssd_warm_c", "ssd_target_c", 65.0)]}
REMOVED = {2: [("thermal", "warm_cpu_factor")]}


class SettingsError(ValueError):
    pass


def _coerce(value, default, path: str):
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        raise SettingsError(f"{path}: expected true/false")
    if isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"{path}: expected a number")
        if value < 0:
            raise SettingsError(f"{path}: must be >= 0")
        if isinstance(default, float):
            return float(value)
        if not float(value).is_integer():
            raise SettingsError(f"{path}: expected an integer")
        return int(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise SettingsError(f"{path}: expected a string")
        return value
    if isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise SettingsError(f"{path}: expected a list of strings")
        return list(value)
    raise SettingsError(f"{path}: unsupported")


def _merge(base: dict, patch: dict, defaults: dict, prefix: str = "") -> None:
    for key, value in patch.items():
        path = f"{prefix}{key}"
        if key not in defaults:
            raise SettingsError(f"unknown setting: {path}")
        if isinstance(defaults[key], dict):
            if not isinstance(value, dict):
                raise SettingsError(f"{path}: expected an object")
            _merge(base[key], value, defaults[key], path + ".")
        else:
            base[key] = _coerce(value, defaults[key], path)


def _diff(data: dict, defaults: dict) -> dict:
    out = {}
    for key, value in data.items():
        if isinstance(value, dict):
            sub = _diff(value, defaults[key])
            if sub:
                out[key] = sub
        elif value != defaults[key]:
            out[key] = value
    return out


class Settings:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.data = copy.deepcopy(DEFAULTS)
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                version = loaded.pop("_version", 1)
                for v in range(version, VERSION):
                    for sec, key, old_default in MIGRATIONS.get(v, []):
                        if loaded.get(sec, {}).get(key) == old_default:
                            del loaded[sec][key]
                    for sec, old, new, old_default in RENAMED.get(v, []):
                        if old in loaded.get(sec, {}):
                            value = loaded[sec].pop(old)
                            if value != old_default:
                                loaded[sec].setdefault(new, value)
                    for sec, key in REMOVED.get(v, []):
                        loaded.get(sec, {}).pop(key, None)
                _merge(self.data, loaded, DEFAULTS)
            except (OSError, ValueError) as e:
                raise SettingsError(f"{path}: {e}") from e
            if version != VERSION:
                self._save()
        else:
            self._save()

    def section(self, name: str) -> dict:
        return self.data[name]

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.data)

    def update(self, patch: dict) -> dict:
        with self._lock:
            candidate = copy.deepcopy(self.data)
            _merge(candidate, patch, DEFAULTS)
            self.data = candidate
            self._save()
            return copy.deepcopy(self.data)

    def apply_preset(self, name: str) -> dict:
        if name not in PRESETS:
            raise SettingsError(f"нет такого режима: {name}")
        return self.update(preset_values(name))

    def reset(self) -> dict:
        with self._lock:
            self.data = copy.deepcopy(DEFAULTS)
            self._save()
            return copy.deepcopy(self.data)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        content = {"_version": VERSION, **_diff(self.data, DEFAULTS)}
        tmp.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
