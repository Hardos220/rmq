import json

import pytest

from rmq.settings import DEFAULTS, Settings, SettingsError
from rmq.util import parse_duration, parse_priority, parse_size_gb


@pytest.mark.parametrize("text,seconds", [
    ("90", 90), ("90s", 90), ("15m", 900), ("15min", 900), ("2h", 7200), ("1d", 86400), ("1h30m", 5400), (45, 45),
])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("bad", ["", "abc", "2x", "1h-5m", -1])
def test_parse_duration_rejects(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


@pytest.mark.parametrize("text,gb", [("8", 8), ("8G", 8), ("8gb", 8), ("512M", 0.5), (1.5, 1.5), ("1T", 1024)])
def test_parse_size(text, gb):
    assert parse_size_gb(text) == pytest.approx(gb)


def test_parse_priority():
    assert parse_priority("high") == 2 and parse_priority("LOW") == 0 and parse_priority(1) == 1
    with pytest.raises(ValueError):
        parse_priority("urgent")


def test_settings_roundtrip_and_validation(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(path)
    assert s.snapshot() == DEFAULTS
    assert json.loads(path.read_text(encoding="utf-8")) == {"_version": 4}  # only changes are stored
    s.update({"reserve": {"ram_min_gb": 2}})
    assert json.loads(path.read_text(encoding="utf-8")) == {"_version": 4, "reserve": {"ram_min_gb": 2.0}}
    assert Settings(path).section("reserve")["ram_min_gb"] == 2.0
    for bad in ({"reserve": {"nope": 1}}, {"reserve": {"ram_min_gb": "x"}}, {"reserve": {"ram_min_gb": -1}},
                {"server": {"port": 80.5}}, {"game_mode": {"process_names": "game.exe"}}):
        with pytest.raises(SettingsError):
            s.update(bad)
    assert s.section("reserve")["ram_min_gb"] == 2.0  # a failed update changes nothing


def test_old_full_settings_file_gets_new_defaults(tmp_path):
    import copy
    path = tmp_path / "settings.json"
    old = copy.deepcopy(DEFAULTS)
    old["game_mode"]["extra_ram_reserve_gb"] = 0.0      # the old default, never touched by the owner
    old["reserve"]["ram_min_gb"] = 3.0                  # a real change by the owner
    path.write_text(json.dumps(old), encoding="utf-8")
    s = Settings(path)
    assert s.section("game_mode")["extra_ram_reserve_gb"] == DEFAULTS["game_mode"]["extra_ram_reserve_gb"]
    assert s.section("reserve")["ram_min_gb"] == 3.0
    assert json.loads(path.read_text(encoding="utf-8")) == {"_version": 4, "reserve": {"ram_min_gb": 3.0}}


def test_warm_limits_become_regulator_targets(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"_version": 2, "thermal": {"cpu_warm_c": 78.0, "board_warm_c": 72.0,
                                                           "warm_cpu_factor": 0.4}}), encoding="utf-8")
    t = Settings(path).section("thermal")
    assert t["cpu_target_c"] == 78.0                       # the owner's own limit is kept as the target
    assert t["board_target_c"] == DEFAULTS["thermal"]["board_target_c"]
    assert "warm_cpu_factor" not in t and "cpu_warm_c" not in t
    assert json.loads(path.read_text(encoding="utf-8")) == {"_version": 4, "thermal": {"cpu_target_c": 78.0}}


def test_fixed_space_for_members_gives_way_to_a_share_of_the_drives(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"_version": 3, "network": {"max_disk_gb": 50.0, "max_hosted_tasks": 4}}),
                    encoding="utf-8")
    n = Settings(path).section("network")
    assert "max_disk_gb" not in n and n["max_disk_pct"] == 10.0 and n["max_hosted_tasks"] == 4
    assert json.loads(path.read_text(encoding="utf-8")) == {"_version": 4, "network": {"max_hosted_tasks": 4}}


def test_presets_set_the_main_knobs_and_are_recognised(tmp_path):
    from rmq.settings import PRESETS, Settings, active_preset
    s = Settings(tmp_path / "settings.json")
    assert active_preset(s.snapshot()) == "standard"                 # the defaults are the standard preset
    s.update({"game_mode": {"extra_ram_reserve_gb": 3.0}})            # not a preset knob: stays as it is
    data = s.apply_preset("gentle")
    assert data["thermal"]["cpu_target_c"] == 70.0 and active_preset(data) == "gentle"
    assert data["game_mode"]["extra_ram_reserve_gb"] == 3.0
    data = s.apply_preset("quiet")
    assert data["reserve"]["cpu_pct_of_free"] == 40.0 and data["thermal"]["cpu_hot_c"] == 97.0  # gentle's are gone
    s.update({"reserve": {"cpu_pct_of_free": 33.0}})
    assert active_preset(s.snapshot()) is None                        # the owner's own values
    assert set(PRESETS) == {"standard", "gentle", "quiet", "productive"}
    data = s.apply_preset("productive")
    assert (data["reserve"]["cpu_pct_of_free"], data["reserve"]["ram_pct_of_free"], data["reserve"]["cpu_min_cores"],
            data["reserve"]["ram_min_gb"]) == (3.0, 3.0, 1.0, 1.0)
    assert data["gpu"]["user_busy_pct"] == 40.0 and not data["gpu"]["free_vram_for_user"]
    assert not data["game_mode"]["pause_gpu_tasks"] and data["thermal"]["cpu_target_c"] == 90.0
    data = s.apply_preset("standard")                                 # back: every knob the presets touch
    assert data["gpu"]["free_vram_for_user"] and data["game_mode"]["pause_gpu_tasks"]
    assert active_preset(data) == "standard"
