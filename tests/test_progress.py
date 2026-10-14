import pytest

from rmq.progress import parse_explicit, parse_tqdm


@pytest.mark.parametrize("text,pct,note", [
    ("37/120 epoch 3", 30.8, "epoch 3"),
    ("  37 / 120  ", 30.8, ""),
    ("45%", 45.0, ""),
    ("45,5 % сборка", 45.5, "сборка"),
    ('{"done": 3, "total": 4, "note": "fold"}', 75.0, "fold"),
    ('{"percent": 12}', 12.0, ""),
    ("130/120", 100.0, ""),                       # never above 100
])
def test_explicit_formats(text, pct, note):
    r = parse_explicit(text)
    assert r["pct"] == pct and r["note"] == note


@pytest.mark.parametrize("text", ["", "hello", "3/0", "{bad json", '{"note": "x"}', "-5%"])
def test_explicit_rejects_garbage(text):
    assert parse_explicit(text) is None


def test_tqdm_takes_the_last_update_and_its_description():
    log = ("loading data\n"
           "epoch 3:  10%|█         | 10/100 [00:01<00:09, 9.9it/s]\r"
           "epoch 3:  45%|████▌     | 45/100 [00:05<00:06, 9.0it/s]")
    r = parse_tqdm(log)
    assert r["done"] == 45 and r["total"] == 100 and r["pct"] == 45.0 and r["note"] == "epoch 3"


def test_tqdm_scaled_counts_and_ansi_codes():
    r = parse_tqdm("\x1b[A sweep: 12%|█▏        | 1.2k/10.0k [00:30<03:40, 40.0it/s]")
    assert r["done"] == 1200 and r["total"] == 10000 and r["note"] == "sweep"


def test_no_bar_no_progress():
    assert parse_tqdm("step 37 of 120, loss 0.5\n45it [00:10, 4.5it/s]") is None


def test_runs_of_one_script_over_parts_count_as_the_same_command():
    from rmq.manager import command_key
    assert command_key("python run.py --part 3 --seed 0.25") == command_key("python  run.py --part 17 --seed 1.5")
    assert command_key("python run.py") != command_key("python other.py")


def test_a_second_estimate_from_the_recent_pace():
    from collections import deque
    from rmq.manager import progress_pace
    h = deque()
    # the share done says "90%", but over the last ten minutes it grew only from 88% to 90%
    progress_pace(h, 0, {"pct": 88.0})
    out = progress_pace(h, 900, {"pct": 90.0, "eta_s": 100})
    assert out["eta_pace_s"] == 4500 and out["eta_conflict"]       # 10% left at 2% per 15 min
    h = deque()
    progress_pace(h, 0, {"pct": 10.0})
    out = progress_pace(h, 1200, {"pct": 20.0, "eta_s": 4800})
    assert out == {"eta_pace_s": 9600, "eta_conflict": True}       # 80% left at 10% per 20 min, not 80 min
    h = deque()
    progress_pace(h, 0, {"pct": 50.0})
    out = progress_pace(h, 700, {"pct": 50.0, "eta_s": 700})
    assert out == {"stalled_s": 700}                               # not moving for the last ten minutes
