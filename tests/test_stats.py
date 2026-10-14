import csv
import time

import pytest

from rmq.stats import StatsBook, combine

H14 = time.mktime((2026, 10, 10, 14, 0, 0, 0, 0, -1))  # local 14:00


def sample(compute=True, cpu_temp=70.0, **kw):
    s = {"compute": compute, "n_cpu": 20, "cpu_tasks": 10.0 if compute else 0.0, "cpu_user": 2.0,
         "ram_tasks": 4.0 if compute else 0.0, "vram_tasks": 0.0, "gpu_tasks": 0.0, "io_read": 0, "io_write": 0,
         "temps": [("cpu", cpu_temp, 80.0), ("gpu", None, 80.0)], "share": 1.0, "regulating": False,
         "heat_frozen": False, "disk_frozen": False, "disk_free": 600.0, "game": False, "paused": False,
         "battery": False, "agents": {"claude@x": {"tasks": 1, "cpu": 10.0, "gpu": 0.0, "ram": 4.0,
                                                  "io_read": 0, "io_write": 0}} if compute else {},
         "metrics": {"cpu_user": 10.0, "cpu_tasks": 50.0, "temp_cpu": cpu_temp, "heat": 0, "cpu_factor": 100.0}}
    s.update(kw)
    return s


def feed(book, start, seconds, step=1.0, **kw):
    t = start
    while t < start + seconds:
        book.add(t, step, sample(**kw))
        t += step


def rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_an_hour_becomes_a_csv_row(tmp_path):
    book = StatsBook(tmp_path, now=H14)
    feed(book, H14, 1800)                                   # 30 min computing at 70 °C
    feed(book, H14 + 1800, 600, cpu_temp=85.0)              # 10 min at 85 °C, above the 80 °C target
    feed(book, H14 + 2400, 1200, compute=False, cpu_temp=50.0)
    book.count("tasks_done", "claude@x")
    book.count("heat_freezes")
    assert not (tmp_path / "hourly.csv").exists()           # the hour is not over yet
    book.add(H14 + 3600, 1.0, sample())                     # 15:00
    r = rows(tmp_path / "hourly.csv")[0]
    assert r["hour"] == "2026-10-10 14:00"
    assert float(r["compute_min"]) == pytest.approx(40, abs=0.1)
    assert float(r["tasks_cpu_core_h"]) == pytest.approx(10 * 2400 / 3600, abs=0.01)
    assert float(r["cpu_max_c"]) == 85 and float(r["cpu_over_target_min"]) == pytest.approx(10, abs=0.1)
    assert float(r["cpu_load_avg_c"]) == pytest.approx((70 * 30 + 85 * 10) / 40, abs=0.1)
    assert r["gpu_avg_c"] == "" and r["tasks_done"] == "1" and r["heat_freezes"] == "1"
    a = rows(tmp_path / "agents_hourly.csv")[0]
    assert a["agent"] == "claude@x" and float(a["cpu_core_h"]) == pytest.approx(10 * 2400 / 3600, abs=0.01)
    assert a["done"] == "1"
    assert (tmp_path / "README.txt").exists()


def test_minutes_are_written_as_they_pass(tmp_path):
    book = StatsBook(tmp_path, now=H14)
    feed(book, H14, 125)
    m = rows(tmp_path / "minutes-2026-10.csv")
    assert [x["time"] for x in m] == ["2026-10-10 14:00", "2026-10-10 14:01"]
    assert float(m[0]["cpu_tasks_pct"]) == 50 and float(m[0]["temp_cpu_max"]) == 70


def test_a_restart_continues_the_hour_and_never_writes_it_twice(tmp_path):
    book = StatsBook(tmp_path, now=H14)
    feed(book, H14, 120)
    book.close()
    book = StatsBook(tmp_path, now=H14 + 300)               # restarted within the same hour
    feed(book, H14 + 300, 120)
    book.close()
    assert book.report("24h", now=H14 + 600)["total"]["compute_min"] == pytest.approx(4, abs=0.05)
    later = H14 + 2 * 3600
    StatsBook(tmp_path, now=later)                          # the next start is hours later: the hour is written
    StatsBook(tmp_path, now=later + 60)                     # ...once
    r = rows(tmp_path / "hourly.csv")
    assert len(r) == 1 and float(r[0]["compute_min"]) == pytest.approx(4, abs=0.05)


def test_report_by_day_and_by_agent(tmp_path):
    book = StatsBook(tmp_path, now=H14 - 86400)
    feed(book, H14 - 86400, 3600, step=10)                 # yesterday 14:00-15:00
    feed(book, H14, 1800, step=10)                         # today 14:00-14:30
    rep = book.report("7d", now=H14 + 1800)
    assert rep["bucket"] == "day" and len(rep["buckets"]) == 7
    days = {b["key"]: b for b in rep["buckets"]}
    assert days["2026-10-09"]["compute_min"] == pytest.approx(60, abs=0.2)
    assert days["2026-10-10"]["compute_min"] == pytest.approx(30, abs=0.2)   # the hour in progress counts too
    assert days["2026-10-08"]["hours"] == 0                                 # the manager was not running
    assert rep["total"]["compute_min"] == pytest.approx(90, abs=0.3)
    assert rep["agents"][0]["agent"] == "claude@x"
    assert rep["agents"][0]["cpu_core_h"] == pytest.approx(10 * 1.5, abs=0.05)
    hourly = book.report("24h", now=H14 + 1800)
    assert hourly["bucket"] == "hour" and len(hourly["buckets"]) == 24
    assert hourly["buckets"][-1]["key"] == "2026-10-10 14"


def test_combine_weights_averages_by_measured_time():
    out = combine([{"cpu_avg_c": 60.0, "cpu_measured_min": 60.0, "cpu_max_c": 70.0, "compute_min": 10.0},
                   {"cpu_avg_c": 90.0, "cpu_measured_min": 20.0, "cpu_max_c": 95.0, "compute_min": 5.0},
                   {"cpu_avg_c": None, "cpu_measured_min": 0.0, "cpu_max_c": None, "compute_min": 0.0}])
    assert out["cpu_avg_c"] == pytest.approx((60 * 60 + 90 * 20) / 80)
    assert out["cpu_max_c"] == 95 and out["compute_min"] == 15


def test_only_statistics_files_can_be_downloaded(tmp_path):
    book = StatsBook(tmp_path, now=H14)
    feed(book, H14, 70)
    assert book.file_path("minutes-2026-10.csv") is not None
    assert book.file_path("README.txt") is not None
    for bad in ("../settings.json", "current.json", "hourly.csv"):     # hourly.csv does not exist yet
        assert book.file_path(bad) is None


def test_charts_keep_what_each_task_used_and_average_it():
    from rmq.metrics import Metrics
    m = Metrics(coarse_step=3)
    for i in range(4):
        m.add(1000.0 + i, {"cpu_tasks": 50.0, "tasks": {7: (40.0, 2.0, 0.0, 0.0), **({9: (10.0, 1.0, 0.5, 30.0)} if i < 2 else {})}})
    fine = m.series(60)
    assert fine["tasks"][0] == {"7": [40.0, 2.0, 0.0, 0.0], "9": [10.0, 1.0, 0.5, 30.0]} and "9" not in fine["tasks"][3]
    coarse = m.series(7200)
    assert coarse["tasks"][0]["7"][0] == 40.0 and coarse["tasks"][0]["9"][0] == 5.0  # present in 2 of the 4 points it averages



def test_chart_history_survives_a_restart_and_comes_back_from_minutes(tmp_path):
    import time as _t
    from rmq.metrics import Metrics
    now = _t.time()
    m = Metrics()
    for i in range(30):
        m.add(now - 30 + i, {"cpu_user": 10.0 + i, "tasks": {5: (20.0, 1.0, 0.0, 0.0)}})
    m.save(tmp_path / "metrics.json.gz")
    again = Metrics()
    last = again.load(tmp_path / "metrics.json.gz", now)
    s = again.series(60)
    assert last == pytest.approx(now - 1, abs=0.1) and len(s["t"]) == 30
    assert s["cpu_user"][-1] == 39.0 and s["tasks"][0] == {"5": [20.0, 1.0, 0.0, 0.0]}
    assert Metrics().load(tmp_path / "missing.gz", now) == 0.0
    rebuilt = Metrics()                                           # no saved file: per-minute rows instead
    rebuilt.seed([{"t": now - 600, "cpu_user": 30.0}, {"t": now - 540, "cpu_user": 40.0}], now)
    day = rebuilt.series(86400)
    assert day["step"] == 15 and len(day["t"]) == 8 and day["cpu_user"][0] == 30.0 and day["cpu_user"][-1] == 40.0
    late = Metrics()                                              # the minute still going: never past now
    late.seed([{"t": now - 20, "cpu_user": 5.0}], now)
    assert max(late.series(3600)["t"]) <= now - 1
    late.add(now + 1, {"cpu_user": 6.0})                          # and new points come in right away
    assert late.series(3600)["cpu_user"][-1] == 6.0


def test_reports_of_several_pcs_add_up():
    from rmq.stats import combine_reports
    def rep(core_h, done, cpu_max, pct, agent_h):
        b = {"observed_min": 60, "tasks_cpu_core_h": core_h, "tasks_done": done, "cpu_max_c": cpu_max,
             "tasks_cpu_pct": pct}
        return {"period": "24h", "bucket": "day", "buckets": [{"key": "2026-10-14", "t": 1, "hours": 1, **b}],
                "total": dict(b), "agents": [{"agent": "a", "cpu_core_h": agent_h, "task_h": 1}], "since": "2026-10-01",
                "targets": {}, "hot": {}, "files": [{"name": "hourly.csv"}]}
    out = combine_reports([("Максим", rep(10, 3, 70, 50, 10)), ("Пароварка", rep(4, 2, 90, 10, 4))], ["Третий: не в сети"])
    t = out["total"]
    assert t["tasks_cpu_core_h"] == 14 and t["tasks_done"] == 5 and t["cpu_max_c"] == 90   # sums and the hottest
    assert t["tasks_cpu_pct"] == 30                    # an average weighted by the time each PC was watched
    assert out["buckets"][0]["tasks_cpu_core_h"] == 14 and out["buckets"][0]["hours"] == 2
    a = out["agents"][0]
    assert len(out["agents"]) == 1 and a["agent"] == "a" and a["cpu_core_h"] == 14 and a["task_h"] == 2
    assert [p["pc"] for p in out["per_pc"]] == ["Максим", "Пароварка"] and out["missing"] == ["Третий: не в сети"]
    assert out["files"] == []                          # the files stay on each PC


def test_charts_keep_a_month_of_5_minute_averages(tmp_path):
    import time as _t
    from rmq.metrics import Metrics
    m = Metrics(coarse_step=3, long_step=12)                   # small steps: four 3-s points make a 12-s one
    for i in range(48):
        m.add(1000.0 + i, {"cpu_tasks": 10.0 if i < 24 else 30.0, "tasks": {7: (40.0, 2.0, 0.0, 0.0)}})
    month = m.series(7 * 86400)
    assert month["step"] == 12 and len(month["t"]) == 3
    assert month["cpu_tasks"][0] == 10.0 and month["cpu_tasks"][-1] == 30.0 and month["tasks"][0]["7"][0] == 40.0
    assert m.series(86400)["step"] == 3 and m.series(3600)["step"] == 1   # the right tier for each period
    now = _t.time()                                             # the month comes back after a restart
    m2 = Metrics()
    m2._long.extend([(now - 3000.0 + k * 300, *([1.0] * 21), {}) for k in range(3)])
    m2.save(tmp_path / "m.json.gz")
    again = Metrics()
    again.load(tmp_path / "m.json.gz", now)
    assert len(again.series(30 * 86400)["t"]) == 3
    assert again.long_missing(now)                              # but its beginning is missing: rebuild it
    minutes = [{"t": now - 20 * 86400 + k * 60, "cpu_user": 20.0} for k in range(10)]
    n = again.seed_long(minutes, now)
    s = again.series(30 * 86400)
    assert n == len({int(x["t"] // 300) for x in minutes})        # 10 minutes: their 5-minute slots
    assert len(s["t"]) == 3 + n and s["t"] == sorted(s["t"]) and s["cpu_user"][0] == 20.0
