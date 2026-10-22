import os
import time

import pytest

from rmq.filecache import FileCache, hash_of
from rmq.netbase import NetError, file_sha256


def project(root, files):
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return {rel: [len(data), 0] for rel, data in files.items()}


def test_big_files_are_kept_once_and_filled_into_the_next_tasks_folder(tmp_path):
    fc = FileCache(tmp_path / "cache")
    big = os.urandom(300_000)
    work1 = tmp_path / "w1"
    manifest = project(work1, {"data/big.bin": big, "run.py": b"print(1)"})
    sha = file_sha256(work1 / "data/big.bin")
    assert fc.keep("n-a", "ext", {"data/big.bin": sha}, work1, manifest) == 1
    assert fc.keep("n-a", "ext", {"data/big.bin": sha}, work1, manifest) == 0  # already there
    assert fc.have("n-a", "ext", [sha, "0" * 64]) == [sha]
    assert fc.have("n-b", "ext", [sha]) == [] and fc.have("n-a", "home", [sha]) == []  # every sender has its own shelf

    work2 = tmp_path / "w2"
    manifest2 = project(work2, {"run.py": b"print(2)"})  # the archive came without the big file
    fc.fill("n-a", "ext", {"data/big.bin": sha}, work2, manifest2)
    assert (work2 / "data/big.bin").read_bytes() == big and manifest2["data/big.bin"][0] == len(big)
    (work2 / "data/big.bin").write_bytes(b"changed by the task")  # a copy: the cache stays as it was
    assert file_sha256(fc.path("n-a", "ext", sha)) == sha


def test_a_file_that_does_not_match_its_hash_is_not_cached(tmp_path):
    fc = FileCache(tmp_path / "cache")
    work = tmp_path / "w"
    manifest = project(work, {"big.bin": os.urandom(1000)})
    assert fc.keep("n-a", "ext", {"big.bin": "a" * 64}, work, manifest) == 0
    assert fc.have("n-a", "ext", ["a" * 64]) == []


def test_lost_files_and_bad_lists_are_refused(tmp_path):
    fc = FileCache(tmp_path / "cache")
    with pytest.raises(NetError) as e:
        fc.fill("n-a", "ext", {"x.bin": "b" * 64}, tmp_path / "w", {})
    assert e.value.status == 410
    assert fc.missing("n-a", "ext", {"x.bin": "b" * 64}) == ["x.bin"]
    for cached in ({"../x": "b" * 64}, {"x": "nothex"}, {"C:/x": "b" * 64}, ["x"]):
        with pytest.raises(NetError):
            fc.check_lists(cached, {})
    with pytest.raises(NetError):
        fc.have("n-a", "ext", "not a list")


def test_gc_drops_what_nobody_used_and_keeps_the_cache_in_its_budget(tmp_path):
    fc = FileCache(tmp_path / "cache")
    now = time.time()
    for i, age_days in enumerate((10, 3, 2, 1)):
        p = fc.path("n-a", "ext", str(i) * 64)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 1000)
        os.utime(p, (now - age_days * 86400, now - age_days * 86400))
    assert fc.gc(now, budget=10_000) == 1            # the one a week old
    assert fc.gc(now, budget=1_500) == 2             # then the oldest, until it fits
    assert fc.size() == 1000 and fc.path("n-a", "ext", "3" * 64).exists()
    assert not fc.path("n-a", "ext", "0" * 64).parent.exists()  # empty folders go too


def test_hashes_are_remembered_until_the_file_changes(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"one")
    memo = {}
    first = hash_of(p, memo)
    assert hash_of(p, memo) == first and len(memo) == 1
    p.write_bytes(b"two!")
    assert hash_of(p, memo) == file_sha256(p) != first
