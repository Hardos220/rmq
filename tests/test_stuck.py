import threading
import time
from types import SimpleNamespace

import pytest

from rmq import manager as mgr
from rmq import thermal


def test_a_driver_call_that_hangs_or_crashes_only_stalls_its_own_thread():
    t = thermal.DriverThread("test-driver")
    assert t.call(lambda: 42, 1.0) == 42
    with pytest.raises(OSError):
        t.call(lambda: (_ for _ in ()).throw(OSError("access violation")), 1.0)
    release = threading.Event()
    with pytest.raises(TimeoutError):
        t.call(release.wait, 0.2)   # hangs: the caller is back in 0.2 s anyway
    release.set()


class FakeNvml:
    """Stands in for nvml.dll: reads crash or hang on demand."""

    def __init__(self, mode=""):
        self.mode, self.shutdowns, self.hang = mode, 0, threading.Event()

    def nvmlInit_v2(self):
        return 0

    def nvmlDeviceGetHandleByIndex_v2(self, i, h):
        return 0

    def nvmlDeviceGetCurrentClocksEventReasons(self, h, r):
        return 0

    def nvmlDeviceGetTemperature(self, h, kind, t):
        if self.mode == "crash":
            raise OSError("exception: access violation writing 0x0000000000000000")
        if self.mode == "hang":
            self.hang.wait()
        t._obj.value = 61
        return 0

    def nvmlDeviceGetPowerUsage(self, h, v):
        v._obj.value = 95_000
        return 0

    def nvmlDeviceGetClockInfo(self, h, kind, v):
        v._obj.value = 2100
        return 0

    def nvmlShutdown(self):
        self.shutdowns += 1
        return 0


def gpu(mode=""):
    g = thermal.NvidiaGpu.__new__(thermal.NvidiaGpu)
    g.timeout_s, g.extra, g.broken, g.open, g.present = 0.3, {}, "", False, True
    g._nv, g._thread = FakeNvml(mode), thermal.DriverThread("test-nvml")
    return g


def test_gpu_sensor_reads_through_its_thread():
    g = gpu()
    assert g.read(True) == (61.0, False) and g.extra == {"power": 95.0, "clock": 2.1} and g.open
    assert g.read(False) == (None, False) and not g.open and g._nv.shutdowns == 1


@pytest.mark.parametrize("mode", ["crash", "hang"])
def test_a_crashed_or_hung_driver_turns_the_gpu_sensor_off_and_is_never_called_again(mode):
    g = gpu(mode)
    t0 = time.monotonic()
    assert g.read(True) == (None, False)
    assert time.monotonic() - t0 < 2 and g.broken                     # the scheduler is not held up
    assert g.read(False) == (None, False) and g._nv.shutdowns == 0      # a broken library is not even closed
    g.close()
    assert g._nv.shutdowns == 0
    g._nv.hang.set()
    r = thermal.readings_from({"gpu_target_c": 83, "gpu_hot_c": 90, "gpu_normal_c": 65}, (None, False),
                              (None, False, True, g.broken), (None, None))
    assert r[0].note == "датчик отключён: сбой драйвера"


def test_stuck_scheduler_is_seen_without_any_lock():
    m = SimpleNamespace(_tick_since=None, STUCK_S=mgr.Manager.STUCK_S)
    assert mgr.Manager.stuck_s(m) == 0
    m._tick_since = time.monotonic() - 5
    assert mgr.Manager.stuck_s(m) == 0                                  # a slow step is not a stuck one
    m._tick_since = time.monotonic() - 125
    assert mgr.Manager.stuck_s(m) == 125


def test_watchdog_logs_where_the_scheduler_is_stuck(caplog):
    release = threading.Event()

    def stuck_in_a_driver():
        release.wait()

    worker = threading.Thread(target=stuck_in_a_driver, daemon=True)
    worker.start()
    m = SimpleNamespace(_stop=threading.Event(), _tick_since=time.monotonic() - 100, _thread=worker,
                        STUCK_S=60.0, WATCH_EVERY_S=0.05)
    watch = threading.Thread(target=mgr.Manager._watch, args=(m,), daemon=True)
    with caplog.at_level("ERROR", logger="rmq"):
        watch.start()
        time.sleep(0.3)
        m._stop.set()
        watch.join(2)
    release.set()
    stuck = [r.getMessage() for r in caplog.records if "stuck in one step" in r.getMessage()]
    assert len(stuck) == 1 and "stuck_in_a_driver" in stuck[0]   # once per stuck step, with its stack


def test_shutdown_does_not_wait_forever_for_a_stuck_scheduler():
    lock = threading.Lock()
    lock.acquire()  # held by the stuck scheduler
    m = SimpleNamespace(_stop=threading.Event(), wake=threading.Event(), _thread=None, lock=lock,
                        SHUTDOWN_LOCK_S=0.2)
    t0 = time.monotonic()
    r = mgr.Manager.prepare_shutdown(m, "freeze")
    assert time.monotonic() - t0 < 2 and r["stuck"] and r["tasks"] == "keep" and m._stop.is_set()


def test_rmq_tells_agents_the_manager_is_stuck_instead_of_sending_them_to_rmq_start():
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from rmq import cli

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/api/health":
                body = json.dumps({"ok": True, "stuck_s": 1800}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                time.sleep(2)  # waits for the stuck scheduler's lock

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        c = cli.Client.__new__(cli.Client)
        c.url, c.headers = url, {}
        with pytest.raises(cli.CliError) as e:
            c.call("GET", "/api/state", timeout=0.5)
        assert e.value.code == cli.EXIT_STUCK and "завис" in str(e.value) and "30 мин" in str(e.value)
        assert "rmq start" not in str(e.value)
    finally:
        srv.shutdown()
