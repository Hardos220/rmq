import pytest

from rmq.planner import (AgentView, Capacity, Debounce, PlanInput, Policy, Pools, TaskView, WindowMax,
                         WindowMean, fairshare_factors, plan, pool_amount, short_factor, water_fill)

NOW = 100_000.0
MIN = 60.0
CAP = Capacity(cpu=20, ram=32, vram=12)


def task(id, agent=1, state="queued", cpu=1, ram=2, vram=0, time_s=3600, **kw):
    kw.setdefault("queued_since", NOW)
    if state == "running":
        kw.setdefault("last_start_at", NOW - 3600)
        kw.setdefault("started_before", True)
    return TaskView(id=id, agent_id=agent, state=state, priority=kw.pop("priority", 1),
                    cpu=cpu, ram=ram, vram=vram, time_s=time_s, **kw)


def agents(*ids, **kw):
    return {i: AgentView(id=i, name=f"a{i}", **kw) for i in ids}


def run(tasks, pools=Pools(18, 22, 11), ag=None, **kw):
    inp = PlanInput(now=kw.pop("now", NOW), capacity=CAP, pools=pools, tasks=tasks,
                    agents=ag or agents(1, 2, 3), **kw)
    return plan(inp)


def acts(p):
    return [(a.kind, a.task_id, a.reason) for a in p.actions]


# ---------------------------------------------------------------- pools
def test_pool_matches_spec_example():
    pool, reserve = pool_amount(32, 8, pct=5, minimum=1)
    assert pool == pytest.approx(22.8) and reserve == pytest.approx(1.2)
    pool, reserve = pool_amount(32, 8 + 10, pct=5, minimum=1)  # game takes 10 GB
    assert pool == pytest.approx(13.0) and reserve == pytest.approx(1.0)
    pool, _ = pool_amount(32, 31.5, pct=5, minimum=1)  # the game wants everything
    assert pool == 0.0


def test_window_max_holds_peak_then_releases():
    w = WindowMax(60)
    assert w.push(0, 5) == 5
    assert w.push(10, 15) == 15
    assert w.push(30, 6) == 15
    assert w.push(71, 6) == 6  # the peak at t=10 left the window


def test_window_mean():
    w = WindowMean(10)
    assert w.push(0, 4) == 4
    assert w.push(5, 0) == 2
    assert w.push(12, 0) == 0  # the sample at t=0 left the window


def test_debounce():
    d = Debounce(on_s=3, off_s=60)
    assert d.update(0, True) is False
    assert d.update(3, True) is True
    assert d.update(10, False) is True
    assert d.update(69, False) is True
    assert d.update(70, False) is False


# ---------------------------------------------------------------- admission
def test_start_when_fits_and_wait_when_not():
    p = run([task(1, ram=10), task(2, ram=10, queued_since=NOW + 1), task(3, ram=10, queued_since=NOW + 2)])
    assert acts(p) == [("start", 1, ""), ("start", 2, "")]
    assert p.waiting[3].reason == "no_fit" and p.waiting[3].detail["lacking"] == ["ram"]


def test_agent_running_limit():
    p = run([task(1), task(2), task(3)], ag=agents(1, max_running=2))
    assert [a[1] for a in acts(p)] == [1, 2]
    assert p.waiting[3].reason == "agent_limit"


def test_high_priority_goes_first():
    p = run([task(1, ram=15), task(2, ram=15, priority=2, queued_since=NOW + 5)])
    assert acts(p) == [("start", 2, "")]


def test_fresh_agent_beats_heavy_user():
    ag = agents(1, 2)
    ag[1].usage = 50_000.0
    p = run([task(1, agent=1, ram=15), task(2, agent=2, ram=15, queued_since=NOW + 5)], ag=ag)
    assert p.fairshare[2] > p.fairshare[1]
    assert acts(p) == [("start", 2, "")]


def test_fairshare_formula():
    ag = agents(1, 2)
    ag[1].usage = 100.0
    f = fairshare_factors(ag, {1, 2})
    assert f[1] == pytest.approx(0.25) and f[2] == pytest.approx(1.0)


def test_short_factor_monotonic():
    assert short_factor(60) == 1.0 and short_factor(90_000) == 0.0
    assert short_factor(600) > short_factor(3600) > short_factor(36_000)


def test_suspended_task_resumes_when_room_returns():
    t = task(1, state="suspended", suspend_reason="ram", resume_ram=8, started_before=True, queued_since=NOW - 5)
    assert acts(run([t], pools=Pools(18, 5, 11))) == []
    assert acts(run([t], pools=Pools(18, 9, 11))) == [("resume", 1, "")]


def test_user_suspended_task_is_not_auto_resumed():
    t = task(1, state="suspended", suspend_reason="user", started_before=True)
    p = run([t])
    assert acts(p) == [] and p.waiting[1].reason == "user"


def test_commit_headroom_blocks_start():
    p = run([task(1, ram=4)], commit_total=50, commit_limit=59)  # 54 > 59 * 0.9
    assert acts(p) == [] and "commit" in p.waiting[1].detail["lacking"]


# ---------------------------------------------------------------- the user comes first
def test_ram_pressure_suspends_overgrown_task_first():
    a = task(1, state="running", ram=2, used_ram=10)                    # outgrew its request
    b = task(2, state="running", ram=6, used_ram=6, priority=0)
    p = run([a, b], pools=Pools(18, 4, 11), ram_deficit=3.0)
    assert acts(p) == [("suspend", 1, "ram")]


def test_ram_pressure_requeues_restartable_and_does_not_restart_same_tick():
    a = task(1, state="running", ram=2, used_ram=10, preempt="requeue")
    p = run([a], pools=Pools(18, 20, 11), ram_deficit=3.0, ram_critical=True)
    assert acts(p) == [("requeue", 1, "ram")]


def test_requeued_task_needs_its_peak_next_time():
    t = task(1, ram=2, peak_ram=10)
    assert acts(run([t], pools=Pools(18, 8, 11))) == []
    assert acts(run([t], pools=Pools(18, 12, 11))) == [("start", 1, "")]


def test_settle_period_skips_non_critical_shedding():
    a = task(1, state="running", ram=4, used_ram=4)
    assert acts(run([a], ram_deficit=1, last_shed_at=NOW - 3)) == []
    assert acts(run([a], ram_deficit=1, last_shed_at=NOW - 3, ram_critical=True)) == [("suspend", 1, "ram")]


def test_game_pauses_gpu_tasks_only():
    g = task(1, state="running", vram=4, used_vram=4)
    c = task(2, state="running", cpu=4)
    q = task(3, vram=2)
    p = run([g, c, q], game_active=True)
    assert acts(p) == [("suspend", 1, "game")]
    assert p.waiting[3].reason == "game"


def test_user_gpu_load_pauses_gpu_tasks():
    g = task(1, state="running", uses_gpu=True)
    assert acts(run([g], gpu_user_busy=True)) == [("suspend", 1, "gpu_user")]


def test_vram_pressure_prefers_restartable():
    a = task(1, state="running", vram=3, used_vram=3, preempt="requeue")
    b = task(2, state="running", vram=3, used_vram=3)
    p = run([a, b], vram_deficit=2.0)
    assert acts(p) == [("requeue", 1, "vram")]


def test_commit_critical_requeues():
    a = task(1, state="running", ram=6, used_ram=6, commit=6)
    p = run([a], commit_excess=2.0, commit_total=58, commit_limit=59)
    assert acts(p)[0] == ("requeue", 1, "commit")


def test_pause_mode_suspends_everything():
    p = run([task(1, state="running"), task(2)], mode="paused")
    assert acts(p) == [("suspend", 1, "paused")]
    assert p.waiting[2].reason == "paused"


def test_time_limit_kills():
    t = task(1, state="running", time_limit_s=600, run_s=601)
    assert acts(run([t])) == [("kill", 1, "time_limit")]


# ---------------------------------------------------------------- fairness between agents
def hog(**kw):
    """Agent 1 started a 3-day job that took almost the whole pool an hour ago."""
    kw.setdefault("last_start_at", NOW - 3600)
    return task(1, agent=1, state="running", cpu=16, ram=20, time_s=3 * 86400, run_s=3600, **kw)


def test_small_task_preempts_hog_after_short_wait():
    small = task(2, agent=2, cpu=1, ram=4, time_s=10 * MIN, queued_since=NOW - 3 * MIN)
    p = run([hog(), small])
    assert acts(p) == [("suspend", 1, "fair:2"), ("start", 2, "")]


def test_no_preemption_before_wait_threshold():
    small = task(2, agent=2, cpu=1, ram=4, time_s=10 * MIN, queued_since=NOW - 1 * MIN)
    p = run([hog(), small])
    assert acts(p) == [] and p.waiting[2].reason == "no_fit"


def test_long_task_waits_longer_before_preempting():
    longer = task(2, agent=2, cpu=1, ram=4, time_s=5 * 3600, queued_since=NOW - 5 * MIN)
    assert acts(run([hog(), longer])) == []
    longer.queued_since = NOW - 11 * MIN
    assert acts(run([hog(), longer]))[0] == ("suspend", 1, "fair:2")


def test_min_slice_protects_recently_started_task():
    small = task(2, agent=2, cpu=1, ram=4, time_s=10 * MIN, queued_since=NOW - 3 * MIN)
    assert acts(run([hog(last_start_at=NOW - 5 * MIN), small])) == []


def test_huge_request_cannot_evict_others_before_starving():
    big = task(2, agent=2, cpu=16, ram=20, time_s=86400, queued_since=NOW - 30 * MIN)
    p = run([hog(), big])
    assert acts(p) == []  # agent 2 would itself exceed its fair half (allowed only after an hour)


def test_never_preempt_higher_priority():
    small = task(2, agent=2, cpu=1, ram=4, time_s=10 * MIN, queued_since=NOW - 3 * MIN, priority=0)
    assert acts(run([hog(), small])) == []


def test_overtime_task_is_preemptible_even_within_share():
    ot = task(1, agent=1, state="running", cpu=2, ram=10, time_s=3600, run_s=3 * 3600, last_start_at=NOW - 3 * 3600)
    other = task(3, agent=3, state="running", cpu=2, ram=10, time_s=86400, run_s=3600)
    waiting = task(2, agent=2, cpu=1, ram=4, time_s=10 * MIN, queued_since=NOW - 3 * MIN)
    p = run([ot, other, waiting])
    assert acts(p) == [("suspend", 1, "fair:2"), ("start", 2, "")]


def test_over_share_agent_cannot_backfill_contested_resource():
    a1 = task(1, agent=1, state="running", cpu=4, ram=12)
    b_top = task(2, agent=2, ram=15, queued_since=NOW - 1)
    a_next = task(3, agent=1, ram=4, queued_since=NOW)
    p = run([a1, b_top, a_next], ag=agents(1, 2))
    assert p.waiting[3].reason == "fair_share"


def test_reservation_blocks_long_backfill_but_allows_short():
    ending = task(1, agent=1, state="running", cpu=2, ram=12, time_s=2 * 3600, run_s=3600)  # ends in ~1 h
    top = task(2, agent=2, ram=15, queued_since=NOW - 30 * MIN, priority=2)
    long_bf = task(3, agent=3, ram=2, time_s=5 * 3600, queued_since=NOW)
    short_bf = task(4, agent=3, ram=2, time_s=20 * MIN, queued_since=NOW + 1)
    p = run([ending, top, long_bf, short_bf], ag=agents(1, 2, 3))
    assert p.waiting[3].reason == "reserved"
    assert ("start", 4, "") in acts(p)


# ---------------------------------------------------------------- CPU: elastic sharing
def test_water_fill_is_max_min_fair():
    assert water_fill(10, {"a": 2, "b": 20}, {}) == {"a": 2, "b": 8}
    assert water_fill(10, {"a": 20, "b": 20}, {"a": 3, "b": 1}) == {"a": 7.5, "b": 2.5}
    assert water_fill(10, {"a": 1, "b": 2}, {}) == {"a": 1, "b": 2}  # leftovers are not invented


def test_cpu_caps_within_one_agent():
    p = run([task(1, state="running", cpu=2), task(2, state="running", cpu=6)], pools=Pools(8, 22, 11))
    assert p.cpu_caps[1] == pytest.approx(2.0) and p.cpu_caps[2] == pytest.approx(6.0)
    p = run([task(1, state="running", cpu=2), task(2, state="running", cpu=6)], pools=Pools(4, 22, 11))
    assert p.cpu_caps[1] == pytest.approx(2.0) and p.cpu_caps[2] == pytest.approx(2.0)
    p = run([task(1, state="running", cpu=2)], pools=Pools(0, 22, 11))
    assert p.cpu_caps[1] == pytest.approx(0.2)  # never zero: 1% of the machine at idle priority


def test_idle_cpu_is_never_wasted():
    p = run([task(1, state="running", cpu=4)], pools=Pools(18, 22, 11))
    assert p.cpu_caps[1] == pytest.approx(18.0)


def test_request_for_all_cpu_starts_on_idle_pc_and_gets_the_pool():
    p = run([task(1, cpu=20, ram=4)], pools=Pools(18.5, 22, 11))
    assert acts(p) == [("start", 1, "")]
    assert p.cpu_caps[1] == pytest.approx(18.5)


def test_second_agent_gets_cpu_without_freezing_the_first():
    a = task(1, agent=1, state="running", cpu=20, ram=4)
    b = task(2, agent=2, cpu=4, ram=2)
    p = run([a, b], pools=Pools(18, 22, 11))
    assert acts(p) == [("start", 2, "")]          # nobody is suspended: CPU is just re-split
    assert p.cpu_caps[2] == pytest.approx(4.0) and p.cpu_caps[1] == pytest.approx(14.0)


def test_agents_split_cpu_evenly_when_both_want_everything():
    a = task(1, agent=1, state="running", cpu=20, ram=4)
    b = task(2, agent=2, cpu=20, ram=4)
    p = run([a, b], pools=Pools(18, 22, 11))
    assert acts(p) == [("start", 2, "")]
    assert p.cpu_caps[1] == pytest.approx(9.0) and p.cpu_caps[2] == pytest.approx(9.0)


def test_agent_over_its_cpu_share_waits_when_squeezing_is_not_enough():
    a = task(1, agent=1, state="running", cpu=18, ram=4)
    a2 = task(2, agent=1, cpu=10, ram=2)                  # needs more than a quarter of the running tasks' CPU
    other = task(3, agent=2, state="running", cpu=1, ram=1)
    p = run([a, a2, other], pools=Pools(18, 22, 11))
    assert p.waiting[2].reason == "no_fit" and p.waiting[2].detail["lacking"] == ["cpu"]


def test_small_cpu_task_squeezes_in_next_to_a_whole_cpu_task_of_the_same_agent():
    # the reported case: one agent runs an "all CPU" task; its GPU task needs only 4 threads
    hog_task = task(3, agent=1, state="running", cpu=20, ram=4.4)
    gpu_check = task(4, agent=1, cpu=4, ram=3, vram=2, time_s=12 * MIN)
    p = run([hog_task, gpu_check], pools=Pools(17.2, 12.7, 11), ag=agents(1))
    assert acts(p) == [("start", 4, "")]
    assert p.cpu_caps[4] == pytest.approx(4.0) and p.cpu_caps[3] == pytest.approx(13.2)
    second_hog = task(5, agent=1, cpu=20, ram=2)
    p = run([hog_task, second_hog], pools=Pools(17.2, 12.7, 11), ag=agents(1))
    assert p.waiting[5].reason == "no_fit"                 # a second "all CPU" task still waits


def test_low_priority_gets_only_leftovers():
    a = task(1, agent=1, state="running", cpu=10)
    bg = task(2, agent=2, state="running", cpu=16, priority=0)
    p = run([a, bg], pools=Pools(18, 22, 11))
    assert p.cpu_caps[1] == pytest.approx(10.0) and p.cpu_caps[2] == pytest.approx(8.0)


# ---------------------------------------------------------------- review fixes
def test_overtime_task_loses_priority_protection():
    ot = task(1, agent=1, state="running", cpu=16, ram=20, time_s=3600, run_s=3 * 3600, priority=2,
              last_start_at=NOW - 3 * 3600)
    waiting = task(2, agent=2, cpu=1, ram=4, time_s=10 * MIN, queued_since=NOW - 3 * MIN)
    assert acts(run([ot, waiting]))[0] == ("suspend", 1, "fair:2")


def test_starving_big_task_may_exceed_its_share():
    big = task(2, agent=2, cpu=16, ram=20, time_s=86400, queued_since=NOW - 30 * MIN)
    assert acts(run([hog(), big])) == []                      # not starving yet: waits
    big.queued_since = NOW - 61 * MIN
    assert acts(run([hog(), big])) == [("suspend", 1, "fair:2"), ("start", 2, "")]


def test_frozen_gpu_task_is_restarted_when_user_still_needs_vram():
    held = task(1, state="suspended", suspend_reason="game", vram=6, used_vram=6, started_before=True)
    assert acts(run([held], game_active=True, vram_deficit=3.0)) == [("requeue", 1, "vram")]
    no_kill = Policy(free_vram_for_user=False)
    assert acts(run([held], game_active=True, vram_deficit=3.0, policy=no_kill)) == []


def test_running_gpu_task_is_frozen_before_being_restarted():
    g = task(1, state="running", vram=6, used_vram=6)
    assert acts(run([g], vram_deficit=3.0)) == [("suspend", 1, "vram")]


# ---------------------------------------------------------------- a nearly full drive
def test_full_drive_freezes_and_holds_only_the_tasks_that_could_write_there():
    busy = task(1, state="running")
    waiting = task(2)
    elsewhere = task(3)
    p = run([busy, waiting, elsewhere], disk_hold={1, 2})
    assert acts(p) == [("suspend", 1, "disk"), ("start", 3, "")]
    assert p.waiting[2].reason == "disk"


def test_tasks_go_on_once_the_drive_has_room_again():
    t = task(1, state="suspended", suspend_reason="disk", started_before=True)
    assert acts(run([t], disk_hold={1})) == []
    assert acts(run([t])) == [("resume", 1, "")]


# ---------------------------------------------------------------- graceful stop of requeue tasks
def leaving(id=1, since=NOW - 10, reason="ram", **kw):
    return task(id, state="running", preempt="requeue", stopping=True, stop_reason=reason,
                stop_requested_at=since, **kw)


def test_requeue_task_may_save_a_checkpoint_when_ram_is_not_critical():
    a = task(1, state="running", ram=2, used_ram=6, preempt="requeue")
    b = task(2, state="running", ram=2, used_ram=2)
    p = run([a, b], pools=Pools(18, 20, 11), ram_deficit=3.0)
    assert acts(p) == [("stop", 1, "ram")]                              # asked, not killed; b untouched


def test_a_stopping_task_counts_as_freeing_its_memory():
    b = task(2, state="running", ram=2, used_ram=2)
    p = run([leaving(used_ram=6), b], pools=Pools(18, 20, 11), ram_deficit=3.0)
    assert acts(p) == []                                                # nobody else is frozen meanwhile
    p = run([leaving(used_ram=6), b], pools=Pools(18, 20, 11), ram_deficit=3.0, ram_critical=True)
    assert acts(p) == [("requeue", 1, "ram")]                           # no time left to wait


def test_a_task_that_does_not_exit_in_time_is_stopped():
    assert acts(run([leaving(since=NOW - 30)])) == []
    assert acts(run([leaving(since=NOW - 61)])) == [("requeue", 1, "ram")]


def test_a_stopping_task_is_never_frozen_on_its_way_out():
    p = run([leaving(reason="fair:7")], mode="paused")
    assert acts(p) == [("requeue", 1, "fair:7")]
    assert acts(run([leaving(vram=4, used_vram=4)], game_active=True)) == [("requeue", 1, "ram")]


def test_urgent_needs_and_zero_grace_stop_at_once():
    g = task(1, state="running", vram=3, used_vram=3, preempt="requeue")
    assert acts(run([g], vram_deficit=2.0)) == [("requeue", 1, "vram")]  # the owner's game needs VRAM now
    a = task(1, state="running", ram=2, used_ram=6, preempt="requeue")
    assert acts(run([a], ram_deficit=3.0, policy=Policy(stop_grace_s=0))) == [("requeue", 1, "ram")]


def test_fairness_victim_saves_first_and_the_waiting_task_starts_after():
    gpu_hog = task(1, agent=1, state="running", cpu=4, ram=4, vram=10, used_vram=10, preempt="requeue",
                   time_s=86400, run_s=3600, last_start_at=NOW - 3600)
    w = task(2, agent=2, cpu=2, ram=2, vram=6, time_s=10 * MIN, queued_since=NOW - 3 * MIN)
    p = run([gpu_hog, w])
    assert acts(p) == [("stop", 1, "fair:2")]
    assert p.waiting[2].reason == "stopping" and p.waiting[2].detail["for"] == [1]
    stopping = leaving(1, reason="fair:2", agent=1, cpu=4, ram=4, vram=10, used_vram=10, time_s=86400,
                       run_s=3600, last_start_at=NOW - 3600)
    p = run([stopping, w])
    assert acts(p) == [] and p.waiting[2].reason == "stopping"          # no second victim meanwhile


# ---------------------------------------------------------------- hung tasks
def test_hung_task_is_frozen_until_someone_resumes_it():
    assert acts(run([task(1, state="running")], hung={1})) == [("suspend", 1, "hung")]
    frozen = task(1, state="suspended", suspend_reason="hung", started_before=True)
    p = run([frozen])
    assert acts(p) == [] and p.waiting[1].reason == "hung" and p.waiting[1].position == 0
    resumed = task(1, state="suspended", suspend_reason="resume", started_before=True)
    assert acts(run([resumed])) == [("resume", 1, "")]


def test_each_running_task_knows_whether_a_waiting_one_may_take_its_place():
    fresh = task(1, agent=1, state="running", cpu=16, ram=16, last_start_at=NOW - 5 * MIN)
    hog = task(2, agent=1, state="running", cpu=2, ram=4, last_start_at=NOW - 3 * 3600)
    late = task(3, agent=2, state="running", cpu=1, ram=1, time_s=600, run_s=1800, last_start_at=NOW - 40 * MIN)
    waiting = task(4, agent=3, cpu=2, ram=2)
    p = run([fresh, hog, late, waiting])
    st = p.standing
    assert st[1]["kind"] == "protected" and 0 < st[1]["left_s"] <= 15 * MIN   # the first 20 minutes are its own
    assert st[2]["kind"] == "over" and st[2]["share"] > st[2]["fair"]         # its agent holds more than its share
    assert st[3]["kind"] == "overtime"                                      # runs longer than it said
    assert 4 not in st                                                      # only running tasks have a standing
    alone = run([task(5, agent=1, state="running", cpu=16, ram=16)])
    assert alone.standing[5]["kind"] == "fair"                              # nobody else wants the PC
