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
