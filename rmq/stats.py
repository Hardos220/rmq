"""Long-term statistics, saved as CSV files in data/stats (they open in Excel, pandas and the like).

hourly.csv            one row per hour: how long tasks computed and what they used, temperatures (average,
                      under load, maximum, time above the target), regulator and freeze time, disk traffic,
                      task counts
agents_hourly.csv     one row per agent and hour: what its tasks used
minutes-YYYY-MM.csv   one row per minute: the dashboard's numbers, averaged (kept for a year)
tasks.csv             one row per finished task: how long it waited and ran, its request, what it really used and
                      what it ran under (kept for good; the task itself leaves the manager a year after it ends)
current.json          the hour in progress, so a restart or a crash loses at most a minute of it
README.txt            what the columns mean

The dashboard's report is built from the same rows.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("rmq")

GB = 1024 ** 3
COMPONENTS = ("cpu", "board", "gpu", "ssd")
COUNTS = ("heat_freezes", "disk_freezes", "tasks_started", "tasks_done", "tasks_failed", "tasks_cancelled",
          "tasks_requeued", "stops_graceful", "stops_forced", "hung_tasks")
AGENT_COUNTS = {"tasks_started": "started", "tasks_done": "done", "tasks_failed": "failed",
                "tasks_cancelled": "cancelled", "tasks_requeued": "requeued"}

HOURLY = ["hour", "observed_min", "compute_min", "tasks_cpu_core_h", "tasks_cpu_pct", "user_cpu_pct",
          "tasks_ram_gb_avg", "tasks_ram_gb_max", "tasks_vram_gb_max", "tasks_gpu_h", "gpu_busy_min",
          "disk_read_gb", "disk_write_gb"]
for _c in COMPONENTS:
    HOURLY += [f"{_c}_avg_c", f"{_c}_load_avg_c", f"{_c}_max_c", f"{_c}_over_target_min",
               f"{_c}_measured_min", f"{_c}_load_measured_min"]
HOURLY += ["cpu_power_avg_w", "cpu_power_load_avg_w", "cpu_power_max_w", "clock_p_load_ghz", "clock_e_load_ghz",
           "cpu_power_measured_min", "cpu_power_load_measured_min"]
HOURLY += ["regulator_min", "cpu_share_pct", "heat_frozen_min", "disk_frozen_min", "disk_free_min_gb",
           "game_min", "paused_min", "battery_min", *COUNTS]
AGENTS = ["hour", "agent", "task_h", "cpu_core_h", "gpu_h", "ram_gb_h", "disk_read_gb", "disk_write_gb",
          *AGENT_COUNTS.values()]
MINUTES = ["time", "cpu_user_pct", "cpu_tasks_pct", "cpu_limit_pct", "ram_user_gb", "ram_tasks_gb",
           "vram_user_gb", "vram_tasks_gb", "gpu_user_pct", "gpu_tasks_pct", "running", "queued", "suspended",
           "game", "cpu_share_pct", "heat", "temp_cpu", "temp_cpu_max", "temp_board", "temp_gpu", "temp_ssd",
           "cpu_power_w", "clock_p_ghz", "clock_e_ghz", "disk_free_gb"]
# minute columns averaged from the dashboard's metrics (the rest are computed separately)
_MINUTE_AVG = {"cpu_user_pct": "cpu_user", "cpu_tasks_pct": "cpu_tasks", "cpu_limit_pct": "cpu_limit",
               "ram_user_gb": "ram_user", "ram_tasks_gb": "ram_tasks", "vram_user_gb": "vram_user",
               "vram_tasks_gb": "vram_tasks", "gpu_user_pct": "gpu_user", "gpu_tasks_pct": "gpu_tasks",
               "running": "running", "queued": "queued", "suspended": "suspended", "game": "game",
               "cpu_share_pct": "cpu_factor", "temp_cpu": "temp_cpu", "temp_board": "temp_board",
               "temp_gpu": "temp_gpu", "temp_ssd": "temp_ssd", "cpu_power_w": "cpu_power", "clock_p_ghz": "clock_p",
               "clock_e_ghz": "clock_e"}
KEEP_MINUTES_MONTHS = 12
TASKS = ["finished", "id", "agent", "name", "state", "exit_code", "pc", "from_pc", "submitted", "started", "wait_min",
         "run_min", "cpu_claim", "ram_claim_gb", "vram_claim_gb", "time_claim_min", "cpu_avg_cores", "peak_ram_gb",
         "peak_vram_gb", "restarts", "held_min", "paused_min", "slept_min", "ecores_min", "throttled_min", "shared_min",
         "cpu_power_w", "clock_p_ghz", "clock_e_ghz", "series", "command"]

# summing, weighted-average, max and min columns, for combining hours into days and periods
_SUM = {"observed_min", "compute_min", "tasks_cpu_core_h", "tasks_gpu_h", "gpu_busy_min", "disk_read_gb",
        "disk_write_gb", "regulator_min", "heat_frozen_min", "disk_frozen_min", "game_min", "paused_min",
        "battery_min", "cpu_power_measured_min", "cpu_power_load_measured_min", *COUNTS}
_WAVG = {"tasks_cpu_pct": "observed_min", "user_cpu_pct": "observed_min", "tasks_ram_gb_avg": "observed_min",
         "cpu_share_pct": "compute_min", "cpu_power_avg_w": "cpu_power_measured_min",
         "cpu_power_load_avg_w": "cpu_power_load_measured_min", "clock_p_load_ghz": "cpu_power_load_measured_min",
         "clock_e_load_ghz": "cpu_power_load_measured_min"}
_MAX = {"tasks_ram_gb_max", "tasks_vram_gb_max", "cpu_power_max_w"}
_MIN = {"disk_free_min_gb"}
for _c in COMPONENTS:
    _SUM |= {f"{_c}_over_target_min", f"{_c}_measured_min", f"{_c}_load_measured_min"}
    _WAVG[f"{_c}_avg_c"] = f"{_c}_measured_min"
    _WAVG[f"{_c}_load_avg_c"] = f"{_c}_load_measured_min"
    _MAX.add(f"{_c}_max_c")
_AGENT_SUM = [c for c in AGENTS if c not in ("hour", "agent")]

README = """Статистика менеджера ресурсов (обновляется сама; файлы можно открывать, пока менеджер работает).

Формат: CSV в UTF-8, разделитель — запятая, дробная часть — через точку. В Excel: «Данные → Из текстового/CSV-файла».
Время — местное. Пустая ячейка — значение не измерялось (например, видеокарта спала).

hourly.csv — по одной строке на час:
  hour                    начало часа
  observed_min            сколько минут этого часа работал менеджер
  compute_min             сколько минут выполнялась хотя бы одна задача
  tasks_cpu_core_h        процессорное время задач, ядро-часы (1 = одно ядро весь час)
  tasks_cpu_pct           средняя загрузка процессора задачами, % от всего процессора
  user_cpu_pct            средняя загрузка процессора вами и Windows, %
  tasks_ram_gb_avg/_max   память задач: в среднем и максимум, ГБ
  tasks_vram_gb_max       видеопамять задач, максимум, ГБ
  tasks_gpu_h             работа видеокарты на задачи, часы полной загрузки
  gpu_busy_min            минуты, когда задачи заметно грузили видеокарту
  disk_read_gb/_write_gb  сколько задачи прочитали и записали (диск и сеть), ГБ
  <компонент>_avg_c       средняя температура, °C (cpu — процессор, board — плата, gpu — видеокарта, ssd — SSD)
  <компонент>_load_avg_c  средняя температура, пока шли расчёты, °C
  <компонент>_max_c       максимальная температура, °C
  <компонент>_over_target_min  минуты выше цели регулятора
  <компонент>_measured_min, _load_measured_min  сколько минут температура измерялась (всего и под нагрузкой)
  cpu_power_avg_w, _load_avg_w, _max_w  мощность процессора (пакет, по LibreHardwareMonitor): в среднем, в среднем
                          во время расчётов и максимум, Вт
  clock_p_load_ghz, clock_e_load_ghz  средняя частота P- и E-ядер во время расчётов, ГГц (у процессора без E-ядер —
                          всех ядер, в clock_p)
  cpu_power_measured_min, _load_measured_min  сколько минут мощность измерялась (всего и во время расчётов)
  regulator_min           минуты, когда регулятор урезал процессор задачам
  cpu_share_pct           средняя доля процессора, которую регулятор оставлял задачам, пока они работали, %
  heat_frozen_min         минуты заморозки из-за перегрева
  disk_frozen_min         минуты, когда задачи стояли из-за нехватки места на диске
  disk_free_min_gb        минимум свободного места на системном диске, ГБ
  game_min, paused_min, battery_min  минуты в игровом режиме, в режиме «Пауза всех», от батареи
  heat_freezes, disk_freezes  сколько раз срабатывала заморозка из-за перегрева и из-за диска
  tasks_started/_done/_failed/_cancelled/_requeued  задачи: запущено, готово, с ошибкой, отменено, перезапущено позже
  stops_graceful, stops_forced  мягкие остановки: задача сохранилась сама / не успела и была остановлена
  hung_tasks              сколько задач помечено как «возможно, зависла»

agents_hourly.csv — по строке на агента и час: task_h (часы работы его задач, сумма по задачам), cpu_core_h, gpu_h,
  ram_gb_h (ГБ-часы памяти), disk_read_gb, disk_write_gb, started, done, failed, cancelled, requeued.

tasks.csv — по строке на каждую завершённую задачу (хранится всегда; сама задача с логом уходит из менеджера через
  12 месяцев после окончания): finished, id, agent, name, state (done/failed/cancelled/timeout/lost), exit_code,
  pc (где выполнялась), from_pc (с какого ПК пришла), submitted, started, wait_min (ждала до старта), run_min,
  заявка (cpu_claim, ram_claim_gb, vram_claim_gb, time_claim_min), cpu_avg_cores (сколько ядер занимала в среднем),
  peak_ram_gb, peak_vram_gb, restarts, условия счёта в минутах: held_min (меньше заявленного процессора),
  paused_min, slept_min (ПК спал), ecores_min (только E-ядра, игра), throttled_min (регулятор), shared_min (рядом
  другие задачи); cpu_power_w, clock_p_ghz, clock_e_ghz (средняя мощность процессора и частоты ядер, пока задача
  работала; пусто, если не измерялись); series, command.

minutes-ГГГГ-ММ.csv — поминутно (хранятся 12 месяцев): средние значения графиков дашборда за минуту — нагрузка
  процессора (%), память и видеопамять (ГБ), видеокарта (%), число задач, доля игрового режима, доля процессора
  от регулятора (%), режим температуры (0 норма, 1 регулятор, 2 перегрев — максимум за минуту), температуры (°C;
  temp_cpu_max — максимум за минуту), мощность процессора (cpu_power_w, Вт), частоты P- и E-ядер (clock_p_ghz,
  clock_e_ghz, ГГц) и свободное место на системном диске (ГБ).

Старые строки файлов, записанные до появления новой колонки, в ней пустые.
"""


def _fmt(v, digits: int = 3) -> str:
    if v is None:
        return ""
    if isinstance(v, int):
        return str(v)
    return f"{v:.{digits}f}".rstrip("0").rstrip(".") if v != 0 else "0"


def _parse(v: str):
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return v


_HEADERS: dict[str, list[str]] = {}  # the header of each file appended to, as it is on disk


def _upgrade_header(path: Path, columns: list[str]) -> list[str]:
    """A file begun by an older version gets the columns added since (empty in its old rows), then rows are
    appended to it in its new order. Columns are only ever added, but one the file has and the code no longer
    writes is kept, not dropped."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        head = next(csv.reader(f), [])
        if head == columns:
            return head
        rows = list(csv.DictReader(f, fieldnames=head))
    keep = list(columns) + [c for c in head if c not in columns]
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(keep)
        for r in rows:
            w.writerow([r.get(c) or "" for c in keep])
    os.replace(tmp, path)
    log.info("statistics file %s: new columns %s", path.name, ", ".join(c for c in columns if c not in head))
    return keep


def _hour_key(t: float) -> str:
    return time.strftime("%Y-%m-%d %H", time.localtime(t))


def _key_ts(key: str) -> float:
    fmt = "%Y-%m-%d %H" if len(key) > 10 else "%Y-%m-%d"
    return time.mktime(time.strptime(key, fmt))


def _new_hour() -> dict:
    a = {"observed": 0.0, "compute": 0.0, "cpu_core_s": 0.0, "user_core_s": 0.0, "cpu_n_s": 0.0, "ram_gbs": 0.0,
         "ram_max": 0.0, "vram_max": 0.0, "gpu_util_s": 0.0, "gpu_busy": 0.0, "io_read": 0.0, "io_write": 0.0,
         "regulated": 0.0, "share_s": 0.0, "heat_frozen": 0.0, "disk_frozen": 0.0, "disk_free_min": None,
         "game": 0.0, "paused": 0.0, "battery": 0.0, "pw_sum": 0.0, "pw_s": 0.0, "pw_lsum": 0.0, "pw_ls": 0.0,
         "pw_max": None, "cp_lsum": 0.0, "cp_ls": 0.0, "ce_lsum": 0.0, "ce_ls": 0.0}
    for c in COMPONENTS:
        a.update({f"{c}_sum": 0.0, f"{c}_s": 0.0, f"{c}_lsum": 0.0, f"{c}_ls": 0.0, f"{c}_max": None,
                  f"{c}_over": 0.0})
    a.update(dict.fromkeys(COUNTS, 0))
    return a


def _new_agent() -> dict:
    return {"task_s": 0.0, "cpu_core_s": 0.0, "gpu_util_s": 0.0, "ram_gbs": 0.0, "io_read": 0.0, "io_write": 0.0,
            **dict.fromkeys(AGENT_COUNTS.values(), 0)}


def hour_row(key: str, a: dict) -> dict:
    """The CSV row of one hour from its running sums."""
    obs, comp = a["observed"], a["compute"]

    def ratio(x, y, scale=1.0):
        return x / y * scale if y > 0 else None

    r = {"hour": f"{key}:00", "observed_min": obs / 60, "compute_min": comp / 60,
         "tasks_cpu_core_h": a["cpu_core_s"] / 3600, "tasks_cpu_pct": ratio(a["cpu_core_s"], a["cpu_n_s"], 100),
         "user_cpu_pct": ratio(a["user_core_s"], a["cpu_n_s"], 100), "tasks_ram_gb_avg": ratio(a["ram_gbs"], obs),
         "tasks_ram_gb_max": a["ram_max"], "tasks_vram_gb_max": a["vram_max"],
         "tasks_gpu_h": a["gpu_util_s"] / 100 / 3600, "gpu_busy_min": a["gpu_busy"] / 60,
         "disk_read_gb": a["io_read"] / GB, "disk_write_gb": a["io_write"] / GB}
    for c in COMPONENTS:
        r[f"{c}_avg_c"] = ratio(a[f"{c}_sum"], a[f"{c}_s"])
        r[f"{c}_load_avg_c"] = ratio(a[f"{c}_lsum"], a[f"{c}_ls"])
        r[f"{c}_max_c"] = a[f"{c}_max"]
        r[f"{c}_over_target_min"] = a[f"{c}_over"] / 60
        r[f"{c}_measured_min"] = a[f"{c}_s"] / 60
        r[f"{c}_load_measured_min"] = a[f"{c}_ls"] / 60
    r.update({"cpu_power_avg_w": ratio(a["pw_sum"], a["pw_s"]), "cpu_power_load_avg_w": ratio(a["pw_lsum"], a["pw_ls"]),
              "cpu_power_max_w": a["pw_max"], "clock_p_load_ghz": ratio(a["cp_lsum"], a["cp_ls"]),
              "clock_e_load_ghz": ratio(a["ce_lsum"], a["ce_ls"]), "cpu_power_measured_min": a["pw_s"] / 60,
              "cpu_power_load_measured_min": a["pw_ls"] / 60})
    r.update({"regulator_min": a["regulated"] / 60, "cpu_share_pct": ratio(a["share_s"], comp, 100),
              "heat_frozen_min": a["heat_frozen"] / 60, "disk_frozen_min": a["disk_frozen"] / 60,
              "disk_free_min_gb": a["disk_free_min"], "game_min": a["game"] / 60, "paused_min": a["paused"] / 60,
              "battery_min": a["battery"] / 60})
    r.update({k: a[k] for k in COUNTS})
    return r


def agent_row(key: str, name: str, a: dict) -> dict:
    return {"hour": f"{key}:00", "agent": name, "task_h": a["task_s"] / 3600, "cpu_core_h": a["cpu_core_s"] / 3600,
            "gpu_h": a["gpu_util_s"] / 100 / 3600, "ram_gb_h": a["ram_gbs"] / 3600,
            "disk_read_gb": a["io_read"] / GB, "disk_write_gb": a["io_write"] / GB,
            **{v: a[v] for v in AGENT_COUNTS.values()}}


def combine(rows: list[dict]) -> dict:
    """Hours (or days) folded into one: sums, weighted averages, maxima and minima."""
    out: dict = {}
    for k in _SUM:
        out[k] = sum(r.get(k) or 0 for r in rows)
    for k, w in _WAVG.items():
        num = sum((r.get(k) or 0) * (r.get(w) or 0) for r in rows if r.get(k) is not None)
        den = sum(r.get(w) or 0 for r in rows if r.get(k) is not None)
        out[k] = num / den if den > 0 else None
    for k in _MAX:
        vals = [r[k] for r in rows if r.get(k) is not None]
        out[k] = max(vals) if vals else None
    for k in _MIN:
        vals = [r[k] for r in rows if r.get(k) is not None]
        out[k] = min(vals) if vals else None
    return out


def combine_reports(parts: list[tuple[str, dict]], missing: list[str]) -> dict:
    """The reports of several of one's PCs as one: sums add up, averages are weighted by time, maxima are the
    highest on any PC. The first report (this PC's) gives the period, the buckets and the temperature limits."""
    base = parts[0][1]
    by_key: dict[str, list[dict]] = {}
    for _, rep in parts:
        for b in rep.get("buckets") or []:
            by_key.setdefault(b["key"], []).append(b)
    buckets = [{"key": b["key"], "t": b["t"], "hours": sum(x.get("hours") or 0 for x in by_key[b["key"]]),
                **combine(by_key[b["key"]])} for b in base["buckets"]]
    agents: dict[str, dict] = {}
    for _, rep in parts:
        for a in rep.get("agents") or []:
            acc = agents.setdefault(a["agent"], dict.fromkeys(_AGENT_SUM, 0.0))
            for k in _AGENT_SUM:
                acc[k] += a.get(k) or 0
    out = dict(base)
    out.update(buckets=buckets, total=combine([rep["total"] for _, rep in parts]),
               agents=sorted(({"agent": n, **a} for n, a in agents.items()), key=lambda x: -x["cpu_core_h"]),
               since=min((rep["since"] for _, rep in parts if rep.get("since")), default=None),
               per_pc=[{"pc": pc, **rep["total"]} for pc, rep in parts], missing=missing,
               files=[], folder=None, pc="all")
    return out


class StatsBook:
    def __init__(self, folder: Path, now: float | None = None):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.hourly_path = folder / "hourly.csv"
        self.agents_path = folder / "agents_hourly.csv"
        self.tasks_path = folder / "tasks.csv"
        self._lock = threading.Lock()
        self.hours: list[dict] = self._load(self.hourly_path)
        self.agent_hours: list[dict] = self._load(self.agents_path)
        self.key: str | None = None
        self.acc = _new_hour()
        self.agents: dict[str, dict] = {}
        self._minute: str | None = None
        self._macc: dict = {}
        self._saved = 0.0
        self._warned = 0.0
        self._write_readme()
        self._resume(now or time.time())
        self._prune_minutes()

    # ------------------------------------------------------------ files
    @staticmethod
    def _load(path: Path) -> list[dict]:
        try:
            with open(path, encoding="utf-8-sig", newline="") as f:
                return [{k: (v if k in ("hour", "agent") else _parse(v)) for k, v in row.items()}
                        for row in csv.DictReader(f)]
        except FileNotFoundError:
            return []
        except (OSError, csv.Error) as e:
            log.warning("cannot read %s: %s", path, e)
            return []

    @staticmethod
    def _append(path: Path, columns: list[str], rows: list[dict], digits: dict | None = None) -> None:
        new = not path.exists() or path.stat().st_size == 0
        if new:
            _HEADERS[str(path)] = list(columns)
        elif str(path) not in _HEADERS:
            _HEADERS[str(path)] = _upgrade_header(path, columns)
        header = _HEADERS[str(path)]
        with open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(columns)
            for r in rows:
                w.writerow([r.get(c) if isinstance(r.get(c), str) else _fmt(r.get(c), (digits or {}).get(c, 3))
                            for c in header])

    def _write_readme(self) -> None:
        path = self.folder / "README.txt"
        try:
            if not path.exists() or path.read_text(encoding="utf-8-sig") != README:
                path.write_text(README, encoding="utf-8-sig")
        except OSError:
            pass

    def _resume(self, now: float) -> None:
        """Continue the hour in progress after a restart, or write it out if that hour is over."""
        try:
            saved = json.loads((self.folder / "current.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        key = saved.get("hour")
        acc = {**_new_hour(), **saved.get("machine", {})}
        agents = {n: {**_new_agent(), **a} for n, a in saved.get("agents", {}).items()}
        if key == _hour_key(now):
            self.key, self.acc, self.agents = key, acc, agents
            return
        try:
            if key and not any(r.get("hour") == f"{key}:00" for r in self.hours[-3:]):
                self._write_hour(key, acc, agents)
            (self.folder / "current.json").unlink(missing_ok=True)
        except OSError as e:
            self._io_failed(e)

    def _write_hour(self, key: str, acc: dict, agents: dict) -> None:
        if acc["observed"] <= 0:
            return
        row = hour_row(key, acc)
        arows = [agent_row(key, n, a) for n, a in sorted(agents.items())]
        digits = {c: 1 for c in HOURLY if c.endswith(("_min", "_c", "_pct", "_w"))}
        digits.update(clock_p_load_ghz=2, clock_e_load_ghz=2)
        self._append(self.hourly_path, HOURLY, [row], digits)
        if arows:
            self._append(self.agents_path, AGENTS, arows)
        self.hours.append({k: (round(v, 6) if isinstance(v, float) else v) for k, v in row.items()})
        self.agent_hours.extend(arows)

    def _save_current(self) -> None:
        tmp = self.folder / "current.json.tmp"
        tmp.write_text(json.dumps({"hour": self.key, "machine": self.acc, "agents": self.agents}), encoding="utf-8")
        os.replace(tmp, self.folder / "current.json")

    def _prune_minutes(self) -> None:
        now = time.localtime()
        for p in self.folder.glob("minutes-*.csv"):
            m = re.fullmatch(r"minutes-(\d{4})-(\d{2})\.csv", p.name)
            if m and (now.tm_year * 12 + now.tm_mon) - (int(m[1]) * 12 + int(m[2])) > KEEP_MINUTES_MONTHS:
                try:
                    p.unlink()
                except OSError:
                    pass

    def _io_failed(self, e: Exception) -> None:
        if time.time() - self._warned > 3600:  # a locked or full disk must not flood the log
            self._warned = time.time()
            log.warning("statistics not saved: %s", e)

    # ------------------------------------------------------------ input
    def task_finished(self, rows: list[dict]) -> None:
        """Lines of tasks.csv (the dates as text, the rest as numbers)."""
        with self._lock:
            try:
                self._append(self.tasks_path, TASKS, rows, {"wait_min": 2, "run_min": 2, "time_claim_min": 1,
                                                             "held_min": 1, "paused_min": 1, "slept_min": 1,
                                                             "ecores_min": 1, "throttled_min": 1, "shared_min": 1,
                                                             "cpu_power_w": 1, "clock_p_ghz": 2, "clock_e_ghz": 2})
            except OSError as e:
                self._io_failed(e)

    def count(self, kind: str, agent: str | None = None, n: int = 1) -> None:
        with self._lock:
            self.acc[kind] = self.acc.get(kind, 0) + n
            if agent and kind in AGENT_COUNTS:
                a = self.agents.setdefault(agent, _new_agent())
                a[AGENT_COUNTS[kind]] += n

    # the dashboard's columns, as the per-minute file names them (for rebuilding the charts after a crash)
    MINUTE_TO_CHART = {"cpu_user": "cpu_user_pct", "cpu_tasks": "cpu_tasks_pct", "cpu_limit": "cpu_limit_pct",
                       "ram_user": "ram_user_gb", "ram_tasks": "ram_tasks_gb", "vram_user": "vram_user_gb",
                       "vram_tasks": "vram_tasks_gb", "gpu_user": "gpu_user_pct", "gpu_tasks": "gpu_tasks_pct",
                       "running": "running", "queued": "queued", "suspended": "suspended", "game": "game",
                       "cpu_factor": "cpu_share_pct", "heat": "heat", "temp_cpu": "temp_cpu",
                       "temp_board": "temp_board", "temp_gpu": "temp_gpu", "temp_ssd": "temp_ssd",
                       "cpu_power": "cpu_power_w", "clock_p": "clock_p_ghz", "clock_e": "clock_e_ghz"}

    def minutes_since(self, since: float) -> list[dict]:
        """Per-minute rows after `since`, as {"t": the minute's start, chart column: value}."""
        out = []
        months = sorted({time.strftime("%Y-%m", time.localtime(t)) for t in (since, time.time())})
        for month in months:
            path = self.folder / f"minutes-{month}.csv"
            try:
                with open(path, encoding="utf-8-sig", newline="") as f:
                    rows = list(csv.DictReader(f))
            except OSError:
                continue
            for r in rows:
                try:
                    t = time.mktime(time.strptime(r["time"], "%Y-%m-%d %H:%M"))
                except (KeyError, ValueError):
                    continue
                if t <= since:
                    continue
                d = {"t": t}
                for col, name in self.MINUTE_TO_CHART.items():
                    v = r.get(name)
                    try:
                        d[col] = float(v) if v not in (None, "") else None
                    except ValueError:
                        d[col] = None
                out.append(d)
        return out

    def add(self, now: float, dt: float, s: dict) -> None:
        """One scheduler tick: `dt` seconds of observation described by `s` (see Manager._stats_tick)."""
        with self._lock:
            key = _hour_key(now)
            if key != self.key:
                if self.key is not None:
                    try:
                        self._write_hour(self.key, self.acc, self.agents)
                    except OSError as e:
                        self._io_failed(e)
                    self._prune_minutes()
                    self.acc, self.agents = _new_hour(), {}
                self.key = key  # events counted before the first tick stay in this hour
            self._minute_tick(now, s)
            if dt <= 0:
                return
            a = self.acc
            a["observed"] += dt
            compute = s["compute"]
            if compute:
                a["compute"] += dt
                a["share_s"] += s["share"] * dt
            a["cpu_core_s"] += s["cpu_tasks"] * dt
            a["user_core_s"] += s["cpu_user"] * dt
            a["cpu_n_s"] += s["n_cpu"] * dt
            a["ram_gbs"] += s["ram_tasks"] * dt
            a["ram_max"] = max(a["ram_max"], s["ram_tasks"])
            a["vram_max"] = max(a["vram_max"], s["vram_tasks"])
            a["gpu_util_s"] += s["gpu_tasks"] * dt
            if s["gpu_tasks"] > 5:
                a["gpu_busy"] += dt
            a["io_read"] += s["io_read"]
            a["io_write"] += s["io_write"]
            for c, temp, target in s["temps"]:
                if temp is None or c not in COMPONENTS:
                    continue
                a[f"{c}_sum"] += temp * dt
                a[f"{c}_s"] += dt
                if compute:
                    a[f"{c}_lsum"] += temp * dt
                    a[f"{c}_ls"] += dt
                a[f"{c}_max"] = temp if a[f"{c}_max"] is None else max(a[f"{c}_max"], temp)
                if temp > target:
                    a[f"{c}_over"] += dt
            mv = s.get("metrics") or {}
            pw = mv.get("cpu_power")
            if pw is not None:
                a["pw_sum"] += pw * dt
                a["pw_s"] += dt
                a["pw_max"] = pw if a["pw_max"] is None else max(a["pw_max"], pw)
                if compute:
                    a["pw_lsum"] += pw * dt
                    a["pw_ls"] += dt
            for src, k in (("clock_p", "cp"), ("clock_e", "ce")):
                if compute and mv.get(src) is not None:
                    a[f"{k}_lsum"] += mv[src] * dt
                    a[f"{k}_ls"] += dt
            for flag, col in (("regulating", "regulated"), ("heat_frozen", "heat_frozen"),
                              ("disk_frozen", "disk_frozen"), ("game", "game"), ("paused", "paused"),
                              ("battery", "battery")):
                if s[flag]:
                    a[col] += dt
            if s["disk_free"] is not None:
                a["disk_free_min"] = s["disk_free"] if a["disk_free_min"] is None else min(a["disk_free_min"],
                                                                                            s["disk_free"])
            for name, u in s["agents"].items():
                g = self.agents.setdefault(name, _new_agent())
                g["task_s"] += u["tasks"] * dt
                g["cpu_core_s"] += u["cpu"] * dt
                g["gpu_util_s"] += u["gpu"] * dt
                g["ram_gbs"] += u["ram"] * dt
                g["io_read"] += u["io_read"]
                g["io_write"] += u["io_write"]
            if now - self._saved >= 60:
                self._saved = now
                try:
                    self._save_current()
                except OSError as e:
                    self._io_failed(e)

    def _minute_tick(self, now: float, s: dict) -> None:
        minute = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
        if minute != self._minute:
            if self._minute is not None and self._macc.get("n"):
                try:
                    self._write_minute()
                except OSError as e:
                    self._io_failed(e)
            self._minute, self._macc = minute, {"n": 0}
        m, vals = self._macc, s["metrics"]
        m["n"] += 1
        for col, src in _MINUTE_AVG.items():
            v = vals.get(src)
            if v is not None:
                m[col] = m.get(col, 0.0) + v
                m[col + "#"] = m.get(col + "#", 0) + 1
        t = vals.get("temp_cpu")
        if t is not None:
            m["temp_cpu_max"] = max(m.get("temp_cpu_max", t), t)
        m["heat"] = max(m.get("heat", 0), int(vals.get("heat") or 0))
        if s["disk_free"] is not None:
            m["disk_free_gb"] = min(m.get("disk_free_gb", s["disk_free"]), s["disk_free"])

    def _write_minute(self) -> None:
        m = self._macc
        row = {"time": self._minute, "temp_cpu_max": m.get("temp_cpu_max"), "heat": m.get("heat", 0),
               "disk_free_gb": m.get("disk_free_gb")}
        for col in _MINUTE_AVG:
            row[col] = m[col] / m[col + "#"] if m.get(col + "#") else None
        path = self.folder / f"minutes-{self._minute[:7]}.csv"
        digits = {c: 1 for c in MINUTES if c.endswith(("_pct", "temp_cpu", "temp_board", "temp_gpu", "temp_ssd",
                                                        "_max", "_w"))}
        self._append(path, MINUTES, [row], {**digits, "game": 2, "running": 2, "queued": 2, "suspended": 2,
                                            "clock_p_ghz": 2, "clock_e_ghz": 2})

    def close(self) -> None:
        with self._lock:
            try:
                if self._minute is not None and self._macc.get("n"):
                    self._write_minute()
                    self._macc = {"n": 0}
                if self.key is not None:
                    self._save_current()
            except OSError as e:
                self._io_failed(e)

    # ------------------------------------------------------------ report
    def report(self, period: str, now: float | None = None) -> dict:
        now = now or time.time()
        with self._lock:
            hours = list(self.hours)
            agent_rows = list(self.agent_hours)
            if self.key is not None and self.acc["observed"] > 0:
                hours.append(hour_row(self.key, self.acc))
                agent_rows += [agent_row(self.key, n, a) for n, a in self.agents.items()]
        if period == "24h":
            start = _key_ts(_hour_key(now)) - 23 * 3600
            keys = [_hour_key(start + 3600 * i + 1800) for i in range(24)]
            width = 13
        else:
            today = time.strftime("%Y-%m-%d", time.localtime(now))
            if period == "all":
                first = min((r["hour"][:10] for r in hours), default=today)
                days = max(1, round((_key_ts(today) - _key_ts(first)) / 86400) + 1)
            else:
                days = {"7d": 7, "30d": 30}.get(period, 7)
            keys = [time.strftime("%Y-%m-%d", time.localtime(_key_ts(today) - 86400 * i + 43200))
                    for i in range(days - 1, -1, -1)]
            width = 10
        lo = keys[0]
        picked = [r for r in hours if r.get("hour") and r["hour"][:width] >= lo]
        groups: dict[str, list[dict]] = {k: [] for k in keys}
        for r in picked:
            groups.setdefault(r["hour"][:width], []).append(r)
        buckets = [{"key": k, "t": _key_ts(k), "hours": len(groups[k]), **combine(groups[k])} for k in keys]
        per_agent: dict[str, dict] = {}
        for r in agent_rows:
            if r.get("hour") and r["hour"][:width] >= lo:
                a = per_agent.setdefault(r["agent"], dict.fromkeys(_AGENT_SUM, 0.0))
                for k in _AGENT_SUM:
                    a[k] += r.get(k) or 0
        agents = sorted(({"agent": n, **a} for n, a in per_agent.items()), key=lambda x: -x["cpu_core_h"])
        first_key = min((r["hour"] for r in hours if r.get("hour")), default=None)
        return {"period": period, "bucket": "hour" if width == 13 else "day", "buckets": buckets,
                "total": combine(picked), "agents": agents, "since": first_key, "folder": str(self.folder),
                "files": self.files()}

    def files(self) -> list[dict]:
        out = []
        for p in sorted(self.folder.glob("*.csv")) + [self.folder / "README.txt"]:
            try:
                out.append({"name": p.name, "size": p.stat().st_size})
            except OSError:
                continue
        return out

    def file_path(self, name: str) -> Path | None:
        """A statistics file by its plain name (never a path outside the folder)."""
        if not re.fullmatch(r"(hourly|agents_hourly|tasks|minutes-\d{4}-\d{2})\.csv|README\.txt", name):
            return None
        p = self.folder / name
        return p if p.is_file() else None
