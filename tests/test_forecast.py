import pytest

from rmq.forecast import Job, median, simulate, speed_ratio

NOW = 1_000_000.0
LIM = {1: 4, 2: 4}


def run(jobs, cpu=16, ram=16, vram=0, limits=LIM):
    return simulate(NOW, cpu, ram, vram, jobs, limits)


def test_queued_tasks_start_as_room_frees():
    out = run([Job(1, 1, 16, 2, 0, 600, running=True), Job(2, 1, 16, 2, 0, 300), Job(3, 1, 8, 2, 0, 300)])
    assert out[1] == (NOW, NOW + 600)
    assert out[2] == (NOW + 600, NOW + 900)            # the whole CPU: after the running one
    assert out[3] == (NOW + 900, NOW + 1200)           # it keeps its place: no room next to #2 either


def test_a_small_task_fills_a_gap_and_memory_must_fit():
    out = run([Job(1, 1, 12, 10, 0, 600, running=True), Job(2, 1, 2, 4, 0, 300), Job(3, 1, 2, 8, 0, 100)])
    assert out[2][0] == NOW                            # 12 + 2 cores fit (squeezed by a quarter), 10 + 4 GB fit
    assert out[3][0] == NOW + 600                      # 10 + 8 GB never fit: it waits for #1 to end


def test_series_after_and_agent_limits():
    jobs = [Job(1, 1, 1, 1, 0, 100, series="s"), Job(2, 1, 1, 1, 0, 100, series="s"),
            Job(3, 2, 1, 1, 0, 50), Job(4, 2, 1, 1, 0, 50, after=3)]
    out = run(jobs)
    assert out[1][0] == NOW and out[2][0] == NOW + 100    # one of a series at a time, in order
    assert out[3][0] == NOW and out[4][0] == NOW + 50     # after #3 ends
    out = run([Job(i, 1, 1, 1, 0, 100) for i in range(1, 6)], limits={1: 2})
    assert sorted(s for s, _ in out.values()) == [NOW, NOW, NOW + 100, NOW + 100, NOW + 200]


def test_a_task_that_can_never_fit_is_left_out():
    out = run([Job(1, 1, 1, 64, 0, 100), Job(2, 1, 1, 1, 0, 100)])
    assert 1 not in out and out[2][0] == NOW


def test_speed_of_another_pc_from_the_same_commands():
    here = {"a": [100, 110, 90], "b": [60], "c": [50]}
    there = {"a": [210, 190], "b": [120, 130, 125], "x": [999]}
    ratio, runs = speed_ratio(here, there)
    assert ratio == pytest.approx(2.0, rel=0.05) and runs == 5
    assert speed_ratio(here, {"x": [1]}) is None
    assert median([3, 1, 2]) == 2 and median([1, 2, 3, 4]) == 2.5 and median([]) is None
