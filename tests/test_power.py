from types import SimpleNamespace

from rmq import manager as mgr
from rmq import winapi


def test_a_ups_is_told_apart_from_a_laptops_battery():
    assert not winapi.is_ups({"lid": True, "batteries": True, "short_term": False})      # a laptop
    assert winapi.is_ups({"lid": True, "batteries": True, "short_term": True})           # Windows says: UPS
    assert winapi.is_ups({"lid": False, "batteries": True, "short_term": False})         # a desktop with a battery
    assert not winapi.is_ups({"lid": False, "batteries": False, "short_term": False})    # a desktop without one
    assert not winapi.is_ups({})                                                         # unknown: nothing changes


def fake(monkeypatch, caps):
    monkeypatch.setattr(winapi, "power_caps", lambda: caps)
    events = []
    m = SimpleNamespace(power_hold=False, _is_ups=False, _caps_at=-1e18, _ups_since=None, _mains_since=None,
                        db=SimpleNamespace(event=lambda kind, msg: events.append((kind, msg))))
    for k in ("UPS_CONFIRM_S", "MAINS_CONFIRM_S", "CAPS_EVERY_S"):
        setattr(m, k, getattr(mgr.Manager, k))
    tick = lambda now, battery, enabled=True: mgr.Manager._ups_tick(m, now, battery, 80, enabled)
    return m, tick, events


def test_power_cut_holds_tasks_after_a_few_seconds_and_lets_go_a_minute_after_power_is_back(monkeypatch):
    m, tick, events = fake(monkeypatch, {"lid": False, "batteries": True})
    tick(0, True)
    tick(3, False)          # a flicker: nothing happens
    tick(4, True)
    assert not m.power_hold
    tick(9.5, True)
    assert m.power_hold and "Пропало питание" in events[-1][1] and "80%" in events[-1][1]
    tick(10, False)
    tick(40, True)          # the power fails again: still held, the minute starts anew
    tick(41, False)
    tick(90, False)
    assert m.power_hold
    tick(101.5, False)
    assert not m.power_hold and "Питание вернулось" in events[-1][1]


def test_a_laptop_on_battery_and_a_switched_off_setting_change_nothing(monkeypatch):
    m, tick, _ = fake(monkeypatch, {"lid": True, "batteries": True, "short_term": False})
    tick(0, True)
    tick(60, True)
    assert not m.power_hold
    m, tick, _ = fake(monkeypatch, {"lid": False, "batteries": True})
    tick(0, True, enabled=False)
    tick(60, True, enabled=False)
    assert not m.power_hold
    tick(61, True)
    tick(70, True)
    assert m.power_hold
    tick(71, True, enabled=False)  # switched off while held: the tasks go on at once
    assert not m.power_hold


def test_space_for_members_is_a_share_of_all_drives_but_at_least_10_gb(monkeypatch):
    sizes = {"C:/": 1907 * 2**30, "D:/": 2000 * 2**30}
    monkeypatch.setattr(winapi, "fixed_drives", lambda: list(sizes))
    monkeypatch.setattr(mgr.shutil, "disk_usage", lambda root: SimpleNamespace(total=sizes[root]))
    pct = {"max_disk_pct": 10.0}
    m = SimpleNamespace(_drives_total=(-1e18, 0.0), NET_DISK_MIN_GB=mgr.Manager.NET_DISK_MIN_GB,
                        settings=SimpleNamespace(section=lambda name: pct))
    limit = lambda: mgr.Manager.net_disk_limit_gb(m)
    assert round(limit(), 1) == 390.7                 # 10% of 3907 GB
    pct["max_disk_pct"] = 25.0
    assert round(limit(), 1) == 976.8                 # the share is the owner's setting
    sizes.clear()
    m._drives_total = (-1e18, 0.0)
    assert limit() == 10.0                            # however small the drives: 10 GB


def test_members_files_never_bring_a_drive_near_full(monkeypatch):
    free = {"C:": 60 * 2**30, "D:": 3000 * 2**30}
    monkeypatch.setattr(mgr.shutil, "disk_usage", lambda root: SimpleNamespace(free=free[root[:2]]))
    m = SimpleNamespace(NET_DISK_SPARE_GB=mgr.Manager.NET_DISK_SPARE_GB, _drive_of=lambda p: p[:2] + "/",
                        settings=SimpleNamespace(section=lambda name: {"min_free_gb": 10.0}))
    room = lambda need: mgr.Manager._net_room(m, need)
    assert room({"C:/work": 40 * 2**30, "D:/rmq/data/net": 500 * 2**30}) is None      # 20 GB stay on C:
    why = room({"C:/work": 30 * 2**30, "C:/rmq/data/net": 20 * 2**30})                # both on C: -> 10 GB left
    assert why and "C:" in why and "60 ГБ" in why
    assert room({"D:/rmq/data/net": 0}) is None
