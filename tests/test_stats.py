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
