import base64
import os
import time
import zipfile

import pytest

from rmq.net import NetService
from rmq.netbase import (NetError, NetStore, NonceCache, body_hash, changed_files, collect_files, make_invite,
                         make_zip, matches_any, parse_invite, requirements_hash, safe_extract, safe_name, sign_headers,
                         tailscale_address, verify)
from test_planner import acts, run, task

KEY = b"k" * 32
NET = {"id": "net-1", "name": "Друзья", "key": base64.urlsafe_b64encode(KEY).decode()}


# ---------------------------------------------------------------- invites and signatures
def test_invite_round_trip_and_garbage():
    inv = parse_invite(make_invite(NET, "100.101.102.103:8778", "Home-PC"))
    assert inv["n"] == "net-1" and inv["a"] == "100.101.102.103:8778" and inv["name"] == "Друзья"
    code = make_invite(NET, "100.1.1.1:8778", "x")
    assert parse_invite(code[:20] + "\n" + code[20:])["n"] == "net-1"   # a messenger may wrap the line
    for bad in ("", "hello", "rmq1-!!!", "rmq1-" + base64.urlsafe_b64encode(b'{"n": 1}').decode()):
        with pytest.raises(NetError):
            parse_invite(bad)


class H(dict):
    def get(self, k, d=None):
        return super().get(k, d)


def signed(method="POST", path="/net/v1/hello", body=b"{}", key=KEY, net="net-1"):
    return H(sign_headers(key, net, "n-a", method, path, body_hash(body)))


def test_only_the_network_key_opens_the_door_and_only_once():
    nonces = NonceCache()
    h = signed()
    assert verify(h, KEY, "net-1", "POST", "/net/v1/hello", nonces) == "n-a"
    with pytest.raises(NetError):
        verify(h, KEY, "net-1", "POST", "/net/v1/hello", nonces)          # replayed
    with pytest.raises(NetError):
        verify(signed(key=b"x" * 32), KEY, "net-1", "POST", "/net/v1/hello", nonces)   # another key
    with pytest.raises(NetError):
        verify(signed(), KEY, "net-1", "POST", "/net/v1/tasks", nonces)   # signed for another path
    with pytest.raises(NetError):
        verify(signed(net="net-2"), KEY, "net-1", "POST", "/net/v1/hello", nonces)
    old = signed()
    old["X-RMQ-Time"] = str(int(time.time()) - 3600)
    with pytest.raises(NetError):
        verify(old, KEY, "net-1", "POST", "/net/v1/hello", nonces)        # an hour-old request


def test_membership_newest_record_wins_and_departures_spread(tmp_path):
    st = NetStore(tmp_path / "network.json")
    st.members = {"n-b": {"name": "B", "address": "1:1", "joined_at": 1, "updated_at": 10, "left_at": None}}
    assert not st.merge({"n-b": {"name": "B-old", "address": "1:1", "joined_at": 1, "updated_at": 5}})
    assert st.merge({"n-b": {"name": "B", "address": "2:2", "joined_at": 1, "updated_at": 20, "left_at": None},
                     "n-c": {"name": "C", "address": "3:3", "joined_at": 2, "updated_at": 2, "left_at": None}})
    assert st.members["n-b"]["address"] == "2:2" and "n-c" in st.active_members()
    st.merge({"n-c": {"name": "C", "address": "3:3", "joined_at": 2, "updated_at": 30, "left_at": 30}})
    assert "n-c" not in st.active_members()
    st.merge({st.node_id: {"name": "impostor", "updated_at": 99}})          # nobody rewrites this PC's record
    assert st.node_id not in st.members or st.members[st.node_id].get("name") != "impostor"
    assert NetStore(tmp_path / "network.json").members["n-b"]["address"] == "2:2"   # saved


def test_each_side_knows_whether_its_own_requests_get_through(tmp_path):
    class Manager:
        netstore = NetStore(tmp_path / "network.json")

    svc = NetService(Manager())
    assert svc.reaches("n-b") is None                     # has not tried yet: no verdict
    svc.live["n-b"] = {"out_try": time.time()}
    assert svc.reaches("n-b") is False                    # tried, nothing got through
    svc._note_peer({"node_id": "n-b"})                    # its hellos to us arrive: says nothing about ours
    assert svc.reaches("n-b") is False and svc.online("n-b")
    svc._note_peer({"node_id": "n-b"}, rtt=18.0)          # our own hello got an answer
    assert svc.reaches("n-b") is True and svc.live["n-b"]["rtt_ms"] == 18
    svc.pool.shutdown()


def test_the_tailscale_adapter_wins_over_a_provider_address_in_the_same_range(monkeypatch):
    psutil = pytest.importorskip("psutil")
    from collections import namedtuple
    import socket
    addr, stat = namedtuple("addr", "family address"), namedtuple("stat", "isup")
    monkeypatch.setattr(psutil, "net_if_addrs", lambda: {"Mobile": [addr(socket.AF_INET, "100.70.1.2")],
                                                         "Tailscale": [addr(socket.AF_INET, "100.101.1.1")]})
    monkeypatch.setattr(psutil, "net_if_stats", lambda: {"Mobile": stat(True), "Tailscale": stat(True)})
    assert tailscale_address() == "100.101.1.1"
    monkeypatch.setattr(psutil, "net_if_stats", lambda: {"Mobile": stat(True), "Tailscale": stat(False)})
    assert tailscale_address() == "100.70.1.2"            # Tailscale is off: the only address in the range


# ---------------------------------------------------------------- the project goes there, the results come back
def project(tmp_path):
    root = tmp_path / "proj"
    for rel, text in {"main.py": "print(1)", "data/input.csv": "a,b\n1,2", ".venv/lib.py": "x",
                      "__pycache__/m.pyc": "x", "results/old.txt": "old"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def test_the_project_is_packed_without_environments_and_caches(tmp_path):
    root = project(tmp_path)
    names = [rel for rel, _ in collect_files(root, None)]
    assert names == ["data/input.csv", "main.py", "results/old.txt"]
    assert [rel for rel, _ in collect_files(root, ["*.py", "data"])] == ["data/input.csv", "main.py"]
    with pytest.raises(NetError):
        collect_files(root, ["missing/*.bin"])
    with pytest.raises(NetError):
        collect_files(root, None, max_bytes=10)


def test_unpacking_never_leaves_the_folder(tmp_path):
    root = project(tmp_path)
    z = tmp_path / "b.zip"
    make_zip(root, collect_files(root, None), z)
    manifest = safe_extract(z, tmp_path / "work")
    assert set(manifest) == {"data/input.csv", "main.py", "results/old.txt"}
    for evil in ("../evil.txt", "C:/Windows/evil.txt", "/abs.txt", "a/../../evil.txt", "con.txt"):
        bad = tmp_path / "evil.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr(evil, "x")
        with pytest.raises(NetError):
            safe_extract(bad, tmp_path / "work2")
    assert not (tmp_path / "evil.txt").exists()
    assert safe_name("a\\b.txt") == "a/b.txt" and safe_name("../x") is None


def test_results_are_the_files_the_task_created_or_changed(tmp_path):
    root = project(tmp_path)
    z = tmp_path / "b.zip"
    make_zip(root, collect_files(root, None), z)
    work = tmp_path / "work"
    before = safe_extract(z, work)
    time.sleep(0.01)
    (work / "results" / "new.txt").write_text("new", encoding="utf-8")
    (work / "main.py").write_text("print(2)  # changed", encoding="utf-8")
    (work / ".rmq" / "tmp").mkdir(parents=True)
    (work / ".rmq" / "tmp" / "scratch").write_text("x", encoding="utf-8")
    files, skipped = changed_files(work, before, 1 << 20)
    assert [f for f, _ in files] == ["main.py", "results/new.txt"] and not skipped
    files, skipped = changed_files(work, before, 5)
    assert skipped                                                        # over the limit: reported, not sent
    assert matches_any("results/new.txt", ["results"]) and not matches_any("main.py", ["results/*"])


def test_one_environment_per_set_of_requirements():
    a = requirements_hash("numpy==2.1\n# comment\nscipy\n", "py")
    assert a == requirements_hash("scipy\nnumpy==2.1\n", "py") != requirements_hash("scipy\n", "py")


# ---------------------------------------------------------------- the owner closes his PC to the network
def test_members_tasks_stand_still_while_the_pc_is_closed():
    hosted = task(1, state="running")
    waiting = task(2)
    mine = task(3)
    p = run([hosted, waiting, mine], net_hold={1, 2})
    assert acts(p) == [("suspend", 1, "net_closed"), ("start", 3, "")]
    assert p.waiting[2].reason == "net_closed"
    frozen = task(1, state="suspended", suspend_reason="net_closed", started_before=True)
    assert acts(run([frozen])) == [("resume", 1, "")]


# ---------------------------------------------------------------- "Мои ПК" and the network with other people
def test_codes_say_which_network_they_lead_to():
    assert parse_invite(make_invite(NET, "100.1.1.1:8778", "A", kind="home"))["t"] == "home"
    assert parse_invite(make_invite(NET, "100.1.1.1:8778", "A"))["t"] == "ext"


def test_own_pcs_and_the_shared_network_are_kept_apart(tmp_path):
    st = NetStore(tmp_path / "network.json")
    st.network = dict(NET)
    st.home = {"id": "home-1", "name": "Мои ПК", "key": base64.urlsafe_b64encode(b"h" * 32).decode(),
               "main": st.node_id, "main_at": 10.0}
    st.merge({"n-b": {"name": "B", "address": "1:1", "updated_at": 1}}, "home")
    st.merge({"n-c": {"name": "C", "address": "2:2", "updated_at": 1}}, "ext")
    assert set(st.active_members("home")) == {"n-b"} and set(st.active_members("ext")) == {"n-c"}
    assert st.kind_of("home-1") == "home" and st.kind_of("net-1") == "ext" and st.kind_of("net-x") is None
    assert st.key_of("home") == b"h" * 32 and st.key_of("ext") == KEY and st.is_main
    assert not st.merge_home_meta({"main": "n-b", "main_at": 5.0})        # an older assignment loses
    assert st.merge_home_meta({"main": "n-b", "main_at": 20.0}) and not st.is_main
    again = NetStore(tmp_path / "network.json")                             # both networks are saved
    assert again.home["main"] == "n-b" and "n-b" in again.home_members and "n-c" in again.members



def test_connection_failures_are_explained():
    from rmq.net import explain

    class E(OSError):
        def __init__(self, code):
            super().__init__("x")
            self.winerror = code

    assert "VPN" in explain("100.1.1.1:8778", E(10013)) and "100.1.1.1:8778" in explain("100.1.1.1:8778", E(10013))
    assert "брандмауэр" in explain("a:1", E(10060)) and "Офлайн" in explain("a:1", E(10061))
    assert explain("a:1", OSError("boom")).endswith("boom")



def test_the_firewall_state_says_whether_other_pcs_get_through():
    from rmq.firewall import state_from
    assert state_from({"enabled": False}) == "ok"                                  # firewall off for that network
    assert state_from({"enabled": True, "allow": 1, "block": 0, "inbound": 0}) == "ok"
    assert state_from({"enabled": True, "allow": 1, "block": 1, "inbound": 0}) == "blocked"  # a block rule wins
    assert state_from({"enabled": True, "allow": 0, "block": 0, "inbound": 0}) == "missing"
    assert state_from({"enabled": True, "allow": 0, "block": 0, "inbound": 1}) == "ok"   # inbound allowed by default


def test_the_main_page_shows_what_runs_on_ones_other_pcs(tmp_path):
    class Manager:
        netstore = NetStore(tmp_path / "network.json")

    st = Manager.netstore
    st.home = {"id": "home-1", "name": "Мои ПК", "key": base64.urlsafe_b64encode(b"h" * 32).decode(),
               "main": st.node_id, "main_at": 1.0}
    st.merge({n: {"name": n.upper(), "address": "1:1", "updated_at": 1} for n in ("n-b", "n-c", "n-d")}, "home")
    svc = NetService(Manager())
    mine = {"id": 7, "name": "train", "origin_node": st.node_id}
    svc._note_peer({"node_id": "n-b", "running": [mine, {"id": 8, "origin_node": None}]}, kind="home")
    svc._note_peer({"node_id": "n-c"}, kind="home")       # an older manager: says nothing about its tasks
    svc.live["n-d"] = {"seen": time.time() - 600, "info_by": {"home": {"running": [mine]}}}   # gone quiet
    by = {p["node_id"]: p for p in svc.home_running()}
    assert [t["id"] for t in by["n-b"]["tasks"]] == [7, 8]
    assert by["n-b"]["tasks"][0]["here"] and not by["n-b"]["tasks"][1]["here"]   # 7 was sent from this PC
    assert by["n-c"]["online"] and not by["n-c"]["known"]
    assert not by["n-d"]["online"] and by["n-d"]["tasks"] == []                  # nothing stale on the main page
    svc.pool.shutdown()


def test_a_task_done_elsewhere_keeps_the_time_it_really_ended():
    from rmq.manager import remote_end_time
    now = 1_000_000.0
    assert remote_end_time({"finished_at": now - 8 * 3600}, now - 9 * 3600, now) == now - 8 * 3600  # PC was off overnight
    assert remote_end_time({}, now - 60, now) == now                     # an older version does not say
    assert remote_end_time({"finished_at": now + 120}, now - 60, now) == now   # its clock is a bit ahead
    assert remote_end_time({"finished_at": now - 7200}, now - 60, now) == now  # before it was even sent: wrong
