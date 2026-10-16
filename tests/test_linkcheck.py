from rmq.linkcheck import _when, diagnose, is_tailscale

NOW = 1791963432.0                                   # 2026-10-14 10:37:12 MSK
PEER = "100.64.0.2"


def ts(peer=None, state="Running", online=True):
    peers = {} if peer is None else {"k": {"HostName": "PAROVARKA", "TailscaleIPs": [PEER, "fd7a::1"], **peer}}
    return {"BackendState": state, "Self": {"Online": online}, "Peer": peers}


def code(*a, **kw):
    d = diagnose(*a, now=NOW, **kw)
    return d and d["code"]


def test_go_times():
    assert _when("2026-10-14T10:37:12.123456789+03:00") == NOW and _when("2026-10-14T07:37:12Z") == NOW
    assert _when("0001-01-01T00:00:00Z") is None and _when(None) is None


def test_what_breaks_the_link_is_named():
    fresh = {"Online": True, "LastHandshake": "2026-10-14T10:36:00+03:00"}
    assert code(PEER, ts(fresh, state="Stopped"), "Tailscale", []) == "ts_off"
    assert code(PEER, {}, "Tailscale", []) == "ts_off"                         # its service does not answer
    assert code(PEER, ts(fresh, online=False), "Tailscale", []) == "ts_cut"
    assert code(PEER, ts(), "Tailscale", []) == "peer_unknown"
    assert code(PEER, ts({"Online": False}), "Tailscale", []) == "peer_offline"
    assert code(PEER, ts(fresh), "AmneziaVPN", ["AmneziaVPN"]) == "vpn_route"   # the VPN takes Tailscale's traffic
    assert code(PEER, ts(fresh), "Tailscale", []) == "manager"                  # the tunnel works: the manager there
    assert code(PEER, None, "Tailscale", []) is None                            # no Tailscale CLI: nothing to say


def test_a_tunnel_that_never_shook_hands_suggests_reconnecting_and_the_vpn():
    never = {"Online": True, "LastHandshake": "0001-01-01T00:00:00Z", "RxBytes": 0, "TxBytes": 13572}
    d = diagnose(PEER, ts(never), "Tailscale", ["AmneziaVPN"], now=NOW)
    assert d["code"] == "tunnel" and "Disconnect → Connect" in d["hint"]
    assert "«AmneziaVPN»" in d["hint"] and "100.64.0.0/10" in d["hint"]
    stale = {"Online": True, "LastHandshake": "2026-10-14T10:20:00+03:00"}
    d = diagnose(PEER, ts(stale), "Tailscale", [], now=NOW)
    assert d["code"] == "tunnel" and "VPN" not in d["hint"]                     # no VPN here: not blamed


def test_only_tailscale_addresses_are_diagnosed():
    assert is_tailscale(PEER) and not is_tailscale("127.0.0.1") and not is_tailscale("192.168.3.17")
    assert not is_tailscale("not an ip")


def test_how_tailscale_connects_to_each_pc():
    from rmq.linkcheck import paths_from
    ts = {"Peer": {"a": {"TailscaleIPs": ["100.64.0.2", "fd7a::1"], "CurAddr": "", "Relay": "waw"},
                   "b": {"TailscaleIPs": ["100.80.1.2"], "CurAddr": "192.168.3.5:41641", "Relay": "hel"}}}
    p = paths_from(ts)
    assert p["100.64.0.2"] == {"direct": False, "relay": "waw"} and p["fd7a::1"]["relay"] == "waw"
    assert p["100.80.1.2"]["direct"] is True
    assert paths_from(None) == {} and paths_from({}) == {}
