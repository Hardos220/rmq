import pytest

from rmq.planner import Pools
from rmq.settings import DEFAULTS
from rmq.thermal import HeatRegulator, Reading, ThermalGuard, readings_from
from test_planner import NOW, acts, run, task

CFG = dict(DEFAULTS["thermal"])


def cpu(t, throttling=False):  # the board zone; thresholds scaled to the old CPU test values
    return Reading("board", "Плата (ACPI)", t, 85, 95, 70, throttling)


def gpu(t):
    return Reading("gpu", "Видеокарта", t, CFG["gpu_target_c"], CFG["gpu_hot_c"], CFG["gpu_normal_c"])


def feed(guard, start, end, readings, step=1.0):
    t = start
    while t <= end:
        guard.update(t, readings, CFG)
        t += step
    return guard.state


def test_short_spike_does_not_freeze():
    g = ThermalGuard()
    assert feed(g, 0, 5, [cpu(97)]) == "normal"          # 5 s above the limit: not yet
    assert feed(g, 6, 30, [cpu(75)]) == "normal"


def test_sustained_overheating_freezes_until_fully_cooled():
    g = ThermalGuard()
    assert feed(g, 0, 11, [cpu(96), gpu(70)]) == "hot"
    assert "Плата" in g.trigger
    assert feed(g, 12, 200, [cpu(80), gpu(55)]) == "hot"   # cooler, but not back to normal (70)
    assert feed(g, 201, 300, [cpu(69), gpu(55)]) == "hot"  # normal, but not long enough yet
    assert g.cool_left is not None and g.cool_left > 0
    assert feed(g, 301, 330, [cpu(69), gpu(55)]) == "normal"


def test_any_component_can_trigger_and_all_must_cool():
    g = ThermalGuard()
    hot, normal = CFG["gpu_hot_c"], CFG["gpu_normal_c"]
    assert feed(g, 0, 11, [cpu(60), gpu(hot + 1)]) == "hot"
    assert feed(g, 12, 200, [cpu(60), gpu(normal + 3)]) == "hot"   # the GPU is still above its normal
    assert feed(g, 201, 330, [cpu(60), gpu(normal - 2)]) == "normal"


def test_hardware_throttling_counts_as_overheating():
    g = ThermalGuard()
    assert feed(g, 0, 11, [cpu(88, throttling=True)]) == "hot"


def test_warm_is_not_a_freeze():
    g = ThermalGuard()
    assert feed(g, 0, 60, [cpu(90)]) == "normal"           # above the target, below "hot": the regulator's job


def test_sleeping_gpu_is_not_a_problem():
    g = ThermalGuard()
    assert feed(g, 0, 11, [cpu(96), gpu(None)]) == "hot"
    assert feed(g, 12, 140, [cpu(65), gpu(None)]) == "normal"


def test_disabled_guard_never_freezes():
    g = ThermalGuard()
    cfg = dict(CFG, enabled=False)
    for t in range(30):
        g.update(t, [cpu(99)], cfg)
    assert g.state == "normal"


def test_ssd_limits_follow_the_drive():
    r = readings_from(CFG, (60.0, False), (None, False, False), (40.0, 70))
    ssd = next(x for x in r if x.key == "ssd")
    assert ssd.hot == 67 and ssd.target == min(CFG["ssd_target_c"], 62)  # the drive warns at 70: freeze before it
    r = readings_from(CFG, (60.0, False), (None, False, False), (40.0, 56))
    ssd = next(x for x in r if x.key == "ssd")
    assert ssd.hot == 53 and ssd.target == 48                # a drive with a low limit pulls the target down too
    assert not any(x.key == "gpu" for x in r)              # no NVIDIA GPU, no GPU reading


# ---------------------------------------------------------------- planner side
def test_overheating_freezes_every_task_and_starts_nothing():
    running = task(1, state="running", cpu=4)
    owner = task(2, agent=3, state="running", cpu=2, priority=2)
    waiting = task(3, cpu=1)
    p = run([running, owner, waiting], thermal_freeze=True)
    assert sorted(acts(p)) == [("suspend", 1, "heat"), ("suspend", 2, "heat")]
    assert p.waiting[3].reason == "heat"


def test_heat_frozen_tasks_resume_when_cool():
    t = task(1, state="suspended", suspend_reason="heat", started_before=True, resume_ram=2)
    assert acts(run([t], thermal_freeze=True)) == []
    assert acts(run([t])) == [("resume", 1, "")]


def test_frozen_memory_is_pushed_out_only_when_needed():
    frozen = task(1, state="suspended", suspend_reason="heat", used_ram=6, started_before=True)
    assert acts(run([frozen], thermal_freeze=True)) == []
    assert acts(run([frozen], thermal_freeze=True, ram_deficit=2)) == [("trim", 1, "ram")]


def test_trimming_frozen_tasks_comes_before_freezing_running_ones():
    frozen = task(1, state="suspended", suspend_reason="user", used_ram=6, started_before=True)
    busy = task(2, state="running", ram=4, used_ram=4)
    p = run([frozen, busy], pools=Pools(18, 22, 11), ram_deficit=3)
    assert acts(p) == [("trim", 1, "ram")]


def test_frozen_resident_memory_counts_as_occupied():
    frozen = task(1, state="suspended", suspend_reason="user", used_ram=10, started_before=True)
    newcomer = task(2, ram=8, queued_since=NOW)
    p = run([frozen, newcomer], pools=Pools(18, 15, 11))
    assert p.waiting[2].reason == "no_fit"                 # 15 - 10 resident < 8


def test_parse_lhm_prefers_the_cpu_package():
    from rmq.thermal import parse_lhm
    tree = {"Text": "Sensor", "Children": [{"Text": "PC", "Children": [{"Text": "Intel Core i7-13650HX", "Children": [
        {"Text": "Temperatures", "Children": [
            {"Text": "P-Core #1", "Value": "71,0 °C", "SensorId": "/intelcpu/0/temperature/2", "Type": "Temperature"},
            {"Text": "Core Max", "Value": "88,0 °C", "SensorId": "/intelcpu/0/temperature/0", "Type": "Temperature"},
            {"Text": "CPU Package", "Value": "86.5 °C", "SensorId": "/intelcpu/0/temperature/16", "Type": "Temperature"},
            {"Text": "P-Core #1 Distance to TjMax", "Value": "29,0 °C", "SensorId": "/intelcpu/0/temperature/17"},
        ]}]}]}]}
    assert parse_lhm(tree) == 86.5
    tree["Children"][0]["Children"][0]["Children"][0]["Children"].pop(2)  # no package sensor
    assert parse_lhm(tree) == 88.0
    assert parse_lhm({"Text": "Sensor", "Children": []}) is None


def test_cpu_reading_joins_when_available():
    r = readings_from(CFG, (58.0, False), (None, False, True), (30.0, 83), 91.0)
    assert [x.key for x in r] == ["cpu", "board", "gpu", "ssd"]
    assert r[0].hot == CFG["cpu_hot_c"] and r[0].temp == 91.0


# ---------------------------------------------------------------- the regulator
def die(t):
    return Reading("cpu", "Процессор", t, CFG["cpu_target_c"], CFG["cpu_hot_c"], CFG["cpu_normal_c"])


def drive(reg, start, end, readings_at, step=0.5, cfg=CFG):
    t = start
    while t <= end:
        reg.update(t, readings_at(t), cfg)
        t += step
    return reg.factor


def test_regulator_holds_the_die_at_its_target():
    """A crude laptop: the die follows the tasks' power within seconds, the heatsink within minutes."""
    reg = HeatRegulator()
    hs = dt = 45.0
    seen = []
    t = 0.0
    while t < 1800:
        p = 8 + reg.factor * 70                      # watts: the owner + the tasks at their share
        hs += 0.5 * (25 + 0.55 * p - hs) / 90
        dt += 0.5 * (hs + 0.35 * p - dt) / 2
        reg.update(t, [die(round(dt, 1))], CFG)
        if t > 900:
            seen.append((dt, reg.factor))
        t += 0.5
    temps = [x for x, _ in seen]
    assert max(temps) < CFG["cpu_target_c"] + 1.5 and min(temps) > CFG["cpu_target_c"] - 4
    assert 0.5 < min(f for _, f in seen) <= max(f for _, f in seen) < 1.0   # some CPU taken, not all of it


def test_cool_components_keep_the_whole_cpu():
    reg = HeatRegulator()
    assert drive(reg, 0, 600, lambda t: [die(60), gpu(55)]) == 1.0
    assert reg.limiting is None


def test_cpu_heat_can_cut_down_to_the_floor_but_gpu_heat_only_in_half():
    reg = HeatRegulator()
    assert drive(reg, 0, 900, lambda t: [die(99)]) == pytest.approx(CFG["min_cpu_factor"])
    assert reg.limiting.key == "cpu"
    reg = HeatRegulator()
    assert drive(reg, 0, 900, lambda t: [die(60), gpu(86)]) == pytest.approx(0.5)


def test_a_component_already_cooling_down_is_not_squeezed_further():
    reg = HeatRegulator()
    drive(reg, 0, 20, lambda t: [die(90)])
    before = drive(reg, 20.5, 25, lambda t: [die(90 - (t - 20) * 0.5)])     # it has started to cool
    after = drive(reg, 25.5, 32, lambda t: [die(90 - (t - 20) * 0.5)])      # still above 80, falling 0.5 °C/s
    assert after == pytest.approx(before, abs=0.01)


def test_cpu_comes_back_gradually_after_a_freeze_and_when_sensors_sleep():
    reg = HeatRegulator()
    drive(reg, 0, 10, lambda t: [die(60), gpu(55)])
    reg.after_freeze()
    assert reg.factor == 0.5
    assert 0.5 < drive(reg, 10.5, 20, lambda t: [die(60), gpu(None)]) < 1.0
    assert drive(reg, 20.5, 400, lambda t: [die(60), gpu(None)]) == 1.0


def test_regulation_can_be_switched_off():
    reg = HeatRegulator()
    assert drive(reg, 0, 300, lambda t: [die(99)], cfg=dict(CFG, regulate=False)) == 1.0


def test_a_dummy_acpi_zone_neither_shows_nor_freezes_anything():
    from rmq.thermal import zone_reading
    k = 273.15
    # a desktop's dummy zone: 17 °C and a passive-cooling flag that never clears
    assert zone_reading({"TZ00": k + 17}, {"TZ00": 0.0}, {"TZ00": 1.0}) == (None, False)
    # a real laptop zone, warm and throttled by the firmware
    assert zone_reading({"TZ0": k + 78.4}, {"TZ0": 60.0}, {"TZ0": 0.0}) == (78.4, True)
    # a real zone that is cool: a stuck flag there is not believed
    assert zone_reading({"TZ0": k + 41}, {"TZ0": 0.0}, {}) == (41.0, False)
    # the dummy is skipped, the real zone next to it counts
    assert zone_reading({"A": k + 17, "B": k + 57}, {"A": 0.0, "B": 100.0}, {}) == (57.0, False)
    assert zone_reading({"Z": 0.0}, {}, {}) == (None, False)          # an unpopulated zone


def _lhm_server(auth=None):
    """A stand-in for LibreHardwareMonitor's web server on a free port: (server, port)."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    tree = {"Children": [{"SensorId": "/intelcpu/0/temperature/16", "Text": "CPU Package", "Value": "64,0 °C"}]}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if auth and self.headers.get("Authorization") != auth:
                self.send_response(401)
                self.end_headers()
                return
            data = json.dumps(tree).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_lhm_is_found_wherever_the_owner_runs_it(tmp_path, monkeypatch):
    from rmq.thermal import LhmCpu
    monkeypatch.setattr(LhmCpu, "CONFIG", tmp_path / "nowhere" / "LibreHardwareMonitor.config")
    srv, port = _lhm_server()
    try:
        # started from Downloads, its settings next to it (web server on, no password)
        exe = tmp_path / "Downloads" / "LHM" / "LibreHardwareMonitor.exe"
        exe.parent.mkdir(parents=True)
        exe.with_name("LibreHardwareMonitor.config").write_text(
            f'<configuration><appSettings><add key="listenerPort" value="{port}" /></appSettings></configuration>',
            encoding="utf-8")
        monkeypatch.setattr(LhmCpu, "_process", staticmethod(lambda: (True, exe)))
        lhm = LhmCpu()
        assert lhm.read() == 64.0 and lhm.status == "работает"
        # running elevated (its folder hidden), no settings at hand: the default port, no password
        monkeypatch.setattr(LhmCpu, "_process", staticmethod(lambda: (True, None)))
        monkeypatch.setattr(LhmCpu, "PORT", port)
        lhm = LhmCpu()
        assert lhm.read() == 64.0
    finally:
        srv.shutdown()


def test_lhm_says_what_is_missing(tmp_path, monkeypatch):
    from rmq.thermal import LhmCpu
    monkeypatch.setattr(LhmCpu, "CONFIG", tmp_path / "LibreHardwareMonitor.config")
    monkeypatch.setattr(LhmCpu, "_process", staticmethod(lambda: (False, None)))
    lhm = LhmCpu()
    assert lhm.read() is None and lhm.status == "нет"                      # not running, not installed
    monkeypatch.setattr(LhmCpu, "_process", staticmethod(lambda: (True, None)))
    monkeypatch.setattr(LhmCpu, "PORT", _free_port())
    lhm = LhmCpu()
    assert lhm.read() is None and lhm.status == "веб-сервер выключен"      # runs, but the server is off
    srv, port = _lhm_server(auth="Basic secret")
    try:
        monkeypatch.setattr(LhmCpu, "PORT", port)
        lhm = LhmCpu()
        assert lhm.read() is None and lhm.status == "закрыт паролем"       # a password, and no settings to read it
    finally:
        srv.shutdown()
