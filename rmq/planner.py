"""Pure scheduling policy: pools, scores, admission, shedding and fairness preemption.

No Windows calls here: the manager feeds measured numbers in and executes the
returned actions, so every rule is unit-testable. Units: CPU in logical cores,
RAM and VRAM in GB, time in seconds.

Rules, in the order they are applied every tick:
  1. Hard time limit       - a task past its --time-limit is killed.
  2. Pause mode            - everything is suspended, nothing starts. So it is
                             while a component is overheated, and for the tasks
                             that could write to a drive that is nearly full.
                             A task without any sign of work for too long is
                             frozen as hung until someone resumes or cancels it.
  3. GPU yield             - while the user's GPU load is high (or a game runs)
                             GPU tasks are suspended and none start.
  4. Pressure shedding     - if free RAM / VRAM drops below the reserve, or the
                             commit charge nears the limit, tasks are suspended
                             (memory paged out) or restarted later ("requeue").
                             Frozen GPU tasks that still hold VRAM the user needs
                             are restarted later as well. Unless the need is
                             urgent, a requeue task is first asked to save a
                             checkpoint and exit ("stop") and keeps its resources
                             until it does or its grace time runs out.
  5. Admission             - suspended and queued tasks start in score order if
                             their declared resources fit the pool, within the
                             agent's limits; EASY backfill protects the top task.
  6. Fairness preemption   - a task that waited long enough may suspend tasks of
                             agents above their fair share (or overtime tasks)
                             that have already run a minimum slice. Priority
                             protects only tasks within their declared time; a
                             task starving for an hour may exceed its own share.
  7. CPU caps              - CPU is elastic: higher priority first, then max-min
                             fair between agents (by weight) and between their
                             tasks; unrequested CPU is spread over everyone.
                             A CPU request larger than the pool means "all of it".
                             A new task needing CPU may start when the running
                             ones can shrink by at most `cpu_squeeze_pct` in total,
                             and an agent within its fair share never waits for
                             CPU at all: the others' caps shrink instead.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field, replace

RESOURCES = ("cpu", "ram", "vram")
EPS = 1e-6
RESUMABLE = {"paused", "game", "gpu_user", "ram", "vram", "resume", "shutdown", "heat", "disk",
             "net_closed"}  # plus "fair:<id>"


# ---------------------------------------------------------------- smoothing
class WindowMax:
    """Maximum of a signal over a sliding time window (monotonic deque)."""

    def __init__(self, window_s: float):
        self.window_s = window_s
        self._q: deque[tuple[float, float]] = deque()

    def push(self, t: float, v: float) -> float:
        q = self._q
        while q and q[-1][1] <= v:
            q.pop()
        q.append((t, v))
        while q[0][0] < t - self.window_s:
            q.popleft()
        return q[0][1]

    @property
    def value(self) -> float:
        return self._q[0][1] if self._q else 0.0


class WindowMean:
    """Average of a signal over a sliding time window."""

    def __init__(self, window_s: float):
        self.window_s = window_s
        self._q: deque[tuple[float, float]] = deque()
        self._sum = 0.0

    def push(self, t: float, v: float) -> float:
        self._q.append((t, v))
        self._sum += v
        while self._q[0][0] < t - self.window_s:
            self._sum -= self._q.popleft()[1]
        return self._sum / len(self._q)


class Debounce:
    """A boolean that turns on after `on_s` of continuous truth and off after `off_s` of falsehood."""

    def __init__(self, on_s: float, off_s: float):
        self.on_s, self.off_s = on_s, off_s
        self.state = False
        self._since: float | None = None

    def update(self, t: float, raw: bool) -> bool:
        if raw == self.state:
            self._since = None
        elif self._since is None:
            self._since = t
        elif t - self._since >= (self.on_s if raw else self.off_s):
            self.state, self._since = raw, None
        return self.state


# ---------------------------------------------------------------- pools
def reserve_amount(free: float, pct: float, minimum: float) -> float:
    return max(minimum, free * pct / 100.0)


def pool_amount(capacity: float, user_used: float, pct: float, minimum: float,
                extra: float = 0.0) -> tuple[float, float]:
    """What tasks may use: 95% (by default) of what the OS and the user leave free.

    Example from the spec: 32 GB total, 8 GB OS, 10 GB game -> free 14 GB,
    reserve max(1 GB, 5% of 14) = 1 GB -> pool 13 GB.
    """
    free = max(0.0, capacity - user_used)
    reserve = reserve_amount(free, pct, minimum) + extra
    return max(0.0, free - reserve), reserve


# ---------------------------------------------------------------- views
@dataclass
class Capacity:
    cpu: float
    ram: float
    vram: float


@dataclass
class Pools:
    cpu: float
    ram: float
    vram: float


@dataclass
class TaskView:
    id: int
    agent_id: int
    state: str                      # queued | running | suspended
    priority: int                   # 0 low, 1 normal, 2 high
    cpu: float                      # declared cores
    ram: float                      # declared GB
    vram: float                     # declared GB
    time_s: float                   # declared expected duration
    preempt: str = "suspend"        # suspend | requeue
    time_limit_s: float | None = None
    queued_since: float = 0.0       # submit/requeue time, or the moment of suspension
    last_start_at: float | None = None
    run_s: float = 0.0              # accumulated running time
    started_before: bool = False
    suspend_reason: str | None = None
    used_cpu: float = 0.0
    used_ram: float = 0.0           # working set, GB
    used_vram: float = 0.0
    commit: float = 0.0             # private commit, GB
    resume_ram: float = 0.0         # working set at the moment of suspension
    peak_ram: float = 0.0           # highest working set seen in any run of this task
    peak_vram: float = 0.0
    uses_gpu: bool = False
    stopping: bool = False          # asked to save a checkpoint and exit; holds its resources until it does
    stop_reason: str = ""           # why it was asked: the reason of the requeue that follows
    stop_requested_at: float | None = None
    held_s: float = 0.0             # running time it got less CPU than it asked for: not counted as overtime
    hold: tuple | None = None       # (reason, detail) while it waits for its series or for another task

    @property
    def gpu(self) -> bool:
        return self.vram > 0 or self.uses_gpu


@dataclass
class AgentView:
    id: int
    name: str = ""
    weight: float = 1.0
    max_running: int = 4
    enabled: bool = True
    usage: float = 0.0


@dataclass
class Policy:
    short_task_s: float = 30 * 60
    preempt_after_short_s: float = 2 * 60
    preempt_after_s: float = 10 * 60
    min_slice_s: float = 20 * 60
    reservation_after_s: float = 20 * 60
    starvation_after_s: float = 60 * 60
    fair_preemption: bool = True
    share_tolerance: float = 0.05
    overtime_grace: float = 0.10
    age_full_s: float = 4 * 3600
    cpu_oversubscribe: float = 4 / 3   # = 1 / (1 - 25%): running tasks may shrink by a quarter
    settle_s: float = 10.0
    commit_max_frac: float = 0.90
    game_pause_gpu: bool = True
    free_vram_for_user: bool = True
    stop_grace_s: float = 60.0      # how long a requeue task may take to save a checkpoint (0 = stop at once)
    w_priority: float = 1000.0
    w_fairshare: float = 500.0
    w_age: float = 300.0
    w_short: float = 150.0
    w_started: float = 100.0
    ramp_s: float = 0.0             # a started task's CPU grows to its claim over this time (0 = at once;
                                    # the settings turn it on)
    ramp_start: float = 0.3         # ... from this part of the claim

    @classmethod
    def from_settings(cls, s: dict) -> "Policy":
        sc, rs, gm = s["scheduler"], s["reserve"], s["game_mode"]
        return cls(
            short_task_s=sc["short_task_max_min"] * 60,
            preempt_after_short_s=sc["preempt_after_short_min"] * 60,
            preempt_after_s=sc["preempt_after_min"] * 60,
            min_slice_s=sc["min_slice_min"] * 60,
            reservation_after_s=sc["reservation_after_min"] * 60,
            starvation_after_s=sc["starvation_after_min"] * 60,
            fair_preemption=sc["fair_preemption"],
            share_tolerance=sc["share_tolerance_pct"] / 100,
            overtime_grace=sc["overtime_grace_pct"] / 100,
            age_full_s=max(60.0, sc["age_full_h"] * 3600),
            cpu_oversubscribe=1.0 / (1.0 - min(90.0, sc["cpu_squeeze_pct"]) / 100.0),
            settle_s=sc["settle_s"],
            commit_max_frac=rs["commit_max_pct"] / 100,
            game_pause_gpu=gm["pause_gpu_tasks"],
            free_vram_for_user=s["gpu"]["free_vram_for_user"],
            stop_grace_s=s["tasks"]["stop_grace_s"],
            w_priority=sc["w_priority"], w_fairshare=sc["w_fairshare"], w_age=sc["w_age"],
            w_short=sc["w_short"], w_started=sc["w_started"],
            ramp_s=max(0.0, float(sc.get("ramp_s", 90))), ramp_start=min(1.0, max(0.05, sc.get("ramp_start_pct", 30) / 100)),
        )


@dataclass
class PlanInput:
    now: float
    capacity: Capacity
    pools: Pools
    tasks: list[TaskView]
    agents: dict[int, AgentView]
    policy: Policy = field(default_factory=Policy)
    mode: str = "auto"              # auto | game | paused
    game_active: bool = False
    thermal_freeze: bool = False    # a component is overheated: everything is frozen until it cools down
    disk_hold: set[int] = field(default_factory=set)  # tasks that could write to a nearly full drive
    hung: set[int] = field(default_factory=set)       # running tasks without any sign of work for too long
    net_hold: set[int] = field(default_factory=set)   # members' tasks while the owner keeps this PC to himself
    gpu_user_busy: bool = False
    ram_deficit: float = 0.0        # reserve - available, if positive
    ram_critical: bool = False      # deficit so deep that the settle period is ignored
    vram_deficit: float = 0.0
    commit_total: float = 0.0
    commit_limit: float = 0.0
    commit_excess: float = 0.0      # GB above the critical commit level
    last_shed_at: float = -1e18


@dataclass
class Action:
    kind: str                       # start | resume | suspend | trim | stop | requeue | kill
    task_id: int
    reason: str


@dataclass
class WaitInfo:
    position: int
    reason: str                     # machine code, rendered to text by the server
    detail: dict = field(default_factory=dict)


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    cpu_caps: dict[int, float] = field(default_factory=dict)
    scores: dict[int, float] = field(default_factory=dict)
    fairshare: dict[int, float] = field(default_factory=dict)
    waiting: dict[int, WaitInfo] = field(default_factory=dict)
    # running tasks: can a waiting task take their place? {"kind": protected|overtime|owner|over|fair|stopping, ...}
    standing: dict[int, dict] = field(default_factory=dict)
    shed: bool = False              # something was taken away this tick (starts the settle period)


# ---------------------------------------------------------------- scoring
def short_factor(time_s: float) -> float:
    """1 for tasks of <= 5 min, 0 for >= 24 h, log-scale in between."""
    lo, hi = 300.0, 86400.0
    if time_s <= lo:
        return 1.0
    if time_s >= hi:
        return 0.0
    return 1.0 - math.log(time_s / lo) / math.log(hi / lo)


def is_overtime(t: TaskView, pol: Policy) -> bool:
    """Running longer than declared - not counting the time it was given less CPU than it asked for."""
    return t.run_s - t.held_s > t.time_s * (1.0 + pol.overtime_grace)


def fairshare_factors(agents: dict[int, AgentView], demanding: set[int]) -> dict[int, float]:
    """Slurm-style fair share 2^(-usage_share / weight_share): 1.0 = used nothing recently."""
    act = [agents[a] for a in demanding if a in agents]
    total_u = sum(max(0.0, a.usage) for a in act)
    total_w = sum(max(EPS, a.weight) for a in act)
    out = {}
    for a in act:
        if total_u <= EPS:
            out[a.id] = 1.0
        else:
            out[a.id] = 2.0 ** (-(max(0.0, a.usage) / total_u) / (max(EPS, a.weight) / total_w))
    return out


def task_score(t: TaskView, fairshare: float, now: float, pol: Policy) -> float:
    prio = (0.0, 0.5, 1.0)[max(0, min(2, t.priority))]
    age = 0.0 if t.state == "running" else min(1.0, max(0.0, now - t.queued_since) / pol.age_full_s)
    s = (pol.w_priority * prio + pol.w_fairshare * fairshare + pol.w_age * age
         + pol.w_short * short_factor(t.time_s))
    if t.started_before or t.state != "queued":
        s += pol.w_started
    if is_overtime(t, pol):
        s -= pol.w_priority * 0.5
    return round(s, 1)


# ---------------------------------------------------------------- helpers
def reserved(t: TaskView) -> dict[str, float]:
    """What a running task holds for admission: the most of declared, used now and its peak."""
    return {"cpu": t.cpu, "ram": max(t.ram, t.used_ram, t.peak_ram),
            "vram": max(t.vram, t.used_vram, t.peak_vram)}


def need_of(t: TaskView) -> dict[str, float]:
    """What a waiting task needs to (re)start; a task that outgrew its request is held to its peak."""
    if t.state == "suspended":  # its VRAM is still held and already accounted for
        return {"cpu": t.cpu, "ram": max(t.ram, t.resume_ram, t.peak_ram),
                "vram": max(0.0, max(t.vram, t.peak_vram) - t.used_vram)}
    return {"cpu": t.cpu, "ram": max(t.ram, t.peak_ram), "vram": max(t.vram, t.peak_vram)}


def fits(need: dict, free: dict) -> list[str]:
    """Resources that do not fit (empty list = fits)."""
    return [r for r in RESOURCES if need[r] > free[r] + EPS]


def expected_remaining(t: TaskView) -> float:
    return max(0.0, t.time_s - t.run_s)


# ---------------------------------------------------------------- the planner
def plan(inp: PlanInput) -> Plan:
    pol, now, pools = inp.policy, inp.now, inp.pools
    P = Plan()

    # a CPU request larger than the pool means "all CPU that is free"
    room = max(pools.cpu, 0.1)
    tasks = [replace(t, cpu=room) if t.cpu > room else t for t in inp.tasks]

    demanding = {t.agent_id for t in tasks}
    P.fairshare = fairshare_factors(inp.agents, demanding)
    for t in tasks:
        P.scores[t.id] = task_score(t, P.fairshare.get(t.agent_id, 1.0), now, pol)
    score = P.scores

    running = {t.id: t for t in tasks if t.state == "running"}
    suspended = {t.id: t for t in tasks if t.state == "suspended"}
    queued = {t.id: t for t in tasks if t.state == "queued"}

    touched: set[int] = set()

    def move(t: TaskView, kind: str, reason: str) -> None:
        if kind == "suspend" and t.stopping:  # it is leaving anyway: no point in freezing it on the way out
            kind, reason = "requeue", t.stop_reason or reason
        touched.add(t.id)
        running.pop(t.id, None)
        suspended.pop(t.id, None)
        if kind == "suspend":
            suspended[t.id] = replace(t, state="suspended", suspend_reason=reason, queued_since=now,
                                      resume_ram=max(t.used_ram, t.resume_ram if t.state == "suspended" else 0.0))
        elif kind == "requeue":
            queued[t.id] = replace(t, state="queued", queued_since=now, started_before=False, run_s=0.0,
                                   peak_ram=max(t.peak_ram, t.used_ram), peak_vram=max(t.peak_vram, t.used_vram),
                                   used_cpu=0.0, used_ram=0.0, used_vram=0.0, commit=0.0, resume_ram=0.0,
                                   stopping=False, stop_reason="", stop_requested_at=None)
        P.actions.append(Action(kind, t.id, reason))

    def evict(t: TaskView, reason: str, urgent: bool) -> bool:
        """A requeue task leaves: at once when the owner needs it urgently, otherwise it may first save a
        checkpoint and keeps its resources until it exits. True if its resources are free right now."""
        if t.stopping:
            if urgent:
                move(t, "requeue", t.stop_reason or reason)
            return urgent
        if urgent or t.state != "running" or pol.stop_grace_s <= 0:
            move(t, "requeue", reason)
            return True
        touched.add(t.id)
        running[t.id] = replace(t, stopping=True, stop_reason=reason, stop_requested_at=now)
        P.actions.append(Action("stop", t.id, reason))
        return False

    def run_task(t: TaskView) -> None:
        queued.pop(t.id, None)
        suspended.pop(t.id, None)
        running[t.id] = replace(t, state="running", last_start_at=now, started_before=True)
        P.actions.append(Action("resume" if t.state == "suspended" else "start", t.id, ""))

    weights = {a: inp.agents[a].weight if a in inp.agents else 1.0 for a in demanding}
    total_w = sum(max(EPS, w) for w in weights.values()) or 1.0

    def fair_frac(agent_id: int) -> float:
        return max(EPS, weights.get(agent_id, 1.0)) / total_w

    def agent_share(agent_id: int, extra: TaskView | None = None) -> float:
        """Dominant share of the current pool held by the agent's running tasks."""
        sums = dict.fromkeys(RESOURCES, 0.0)
        tasks = [t for t in running.values() if t.agent_id == agent_id]
        if extra is not None:
            tasks.append(extra)
        for t in tasks:
            for r, v in (reserved(t) if t is not extra else need_of(t)).items():
                sums[r] += v
        shares = [sums[r] / getattr(pools, r) for r in RESOURCES if getattr(pools, r) > EPS]
        return max(shares, default=0.0)

    # 1. hard time limits
    for t in list(running.values()) + list(suspended.values()):
        if t.time_limit_s and t.run_s >= t.time_limit_s:
            running.pop(t.id, None)
            suspended.pop(t.id, None)
            P.actions.append(Action("kill", t.id, "time_limit"))
    # ...and tasks that were asked to save a checkpoint and exit, but did not in time
    for t in list(running.values()):
        if t.stopping and now - (t.stop_requested_at if t.stop_requested_at is not None else now) >= pol.stop_grace_s:
            move(t, "requeue", t.stop_reason or "ram")

    def trim_frozen(need: float) -> float:
        """Push frozen tasks' memory out of RAM, largest first: cheap and loses nothing."""
        for t in sorted(suspended.values(), key=lambda t: -t.used_ram):
            if need <= 0 or t.used_ram <= 0.1:
                break
            P.actions.append(Action("trim", t.id, "ram"))
            need -= t.used_ram
            suspended[t.id] = replace(t, used_ram=0.0)
        return need

    # 2. pause mode and overheating: everything stands still
    if inp.mode == "paused" or inp.thermal_freeze:
        why = "paused" if inp.mode == "paused" else "heat"
        for t in list(running.values()):
            move(t, "suspend", why)
        if inp.ram_deficit > EPS:
            trim_frozen(inp.ram_deficit + 0.25)
        waiting = sorted(list(queued.values()) + list(suspended.values()), key=lambda t: -score[t.id])
        for i, t in enumerate(waiting, 1):
            P.waiting[t.id] = WaitInfo(i, why if t.state == "queued" or t.suspend_reason in ("paused", "heat")
                                       else (t.suspend_reason or why))
        return P

    # 2b. a nearly full drive: the tasks that could write there stand still, and none of them starts
    for t in list(running.values()):
        if t.id in inp.disk_hold:
            move(t, "suspend", "disk")
    # 2b'. the owner closed this PC to the network: members' tasks stand still until it opens again
    for t in list(running.values()):
        if t.id in inp.net_hold:
            move(t, "suspend", "net_closed")
    # 2c. a hung task is frozen and stays so until its agent or the owner resumes or cancels it
    for t in list(running.values()):
        if t.id in inp.hung:
            move(t, "suspend", "hung")

    # 3. GPU yield
    game_gpu = inp.game_active and pol.game_pause_gpu
    gpu_blocked = game_gpu or inp.gpu_user_busy
    gpu_reason = "game" if game_gpu else "gpu_user"
    if gpu_blocked:
        for t in sorted(running.values(), key=lambda t: score[t.id]):
            if t.gpu:
                move(t, "suspend", gpu_reason)
                P.shed = True

    # 4. pressure shedding
    settling = now - inp.last_shed_at < pol.settle_s
    if inp.commit_excess > EPS:
        victims = sorted(list(running.values()) + list(suspended.values()),
                         key=lambda t: (t.preempt != "requeue", score[t.id], -(t.last_start_at or 0)))
        left = inp.commit_excess
        for t in victims:
            if left <= 0:
                break
            move(t, "requeue", "commit")
            left -= max(t.commit, t.used_ram)
            P.shed = True

    if inp.ram_deficit > EPS and (not settling or inp.ram_critical):
        left = trim_frozen(inp.ram_deficit + 0.25)
        for t in [t for t in running.values() if t.stopping]:
            if inp.ram_critical:  # no time left to wait for its checkpoint
                move(t, "requeue", t.stop_reason or "ram")
                P.shed = True
            left -= t.used_ram    # stopped now, or already on its way out
        victims = sorted((t for t in running.values() if not t.stopping),
                         key=lambda t: (-max(0.0, t.used_ram - t.ram), score[t.id], -(t.last_start_at or 0)))
        for t in victims:
            if left <= 0:
                break
            if t.preempt == "requeue":
                evict(t, "ram", urgent=inp.ram_critical)
            else:
                move(t, "suspend", "ram")
            left -= t.used_ram
            P.shed = True

    if inp.vram_deficit > EPS and not settling:
        left = inp.vram_deficit + 0.1
        holders = [t for t in list(running.values()) + list(suspended.values()) if t.gpu]
        for t in sorted(holders, key=lambda t: (t.preempt != "requeue", t.state == "running", score[t.id])):
            if left <= 0:
                break
            if t.preempt == "requeue" or (t.state == "suspended" and pol.free_vram_for_user and t.used_vram > 0.05):
                move(t, "requeue", "vram")  # only a stopped process gives VRAM back
                left -= t.used_vram
                P.shed = True
            elif t.state == "running":  # first step: the GPU stops being loaded, VRAM may stay allocated
                move(t, "suspend", "vram")
                P.shed = True

    # 5. admission (suspended tasks resume and queued tasks start in one score order)
    free = {
        "cpu": pools.cpu * pol.cpu_oversubscribe - sum(t.cpu for t in running.values()),
        "ram": pools.ram - sum(reserved(t)["ram"] for t in running.values())
               - sum(t.used_ram for t in suspended.values()),  # frozen tasks still resident in RAM
        "vram": pools.vram - sum(reserved(t)["vram"] for t in running.values())
                - sum(t.used_vram for t in suspended.values()),
    }
    commit = inp.commit_total
    active = {}
    for t in list(running.values()) + list(suspended.values()):
        active[t.agent_id] = active.get(t.agent_id, 0) + 1

    def candidates() -> list[TaskView]:
        c = [t for t in suspended.values()
             if t.suspend_reason in RESUMABLE or (t.suspend_reason or "").startswith("fair:")]
        c += list(queued.values())
        return sorted(c, key=lambda t: (-score[t.id], t.queued_since, t.id))

    blocked: list[tuple[TaskView, list[str]]] = []
    top: TaskView | None = None
    top_lacking: list[str] = []
    shadow: float | None = None
    reservation = False

    def cpu_entitled(t: TaskView) -> bool:
        """An agent that holds less CPU than its fair share may start now; the caps are re-split."""
        if t.priority >= 2 or pools.cpu <= EPS:
            return t.priority >= 2
        mine = sum(r.cpu for r in running.values() if r.agent_id == t.agent_id)
        return mine / pools.cpu + EPS < fair_frac(t.agent_id)

    def start_ok(t: TaskView) -> tuple[bool, str, dict]:
        nonlocal commit
        agent = inp.agents.get(t.agent_id)
        if agent is None or not agent.enabled:
            return False, "agent_disabled", {}
        if t.id in inp.disk_hold:
            return False, "disk", {}
        if t.id in inp.net_hold:
            return False, "net_closed", {}
        if t.gpu and gpu_blocked:
            return False, gpu_reason, {}
        if t.state == "queued" and active.get(t.agent_id, 0) >= agent.max_running:
            return False, "agent_limit", {"max_running": agent.max_running}
        need = need_of(t)
        lacking = fits(need, free)
        if "cpu" in lacking and cpu_entitled(t):
            lacking.remove("cpu")  # CPU is elastic: the others' caps shrink to make room
        if t.state == "queued" and inp.commit_limit > 0 and commit + t.ram > inp.commit_limit * pol.commit_max_frac:
            lacking.append("commit")
        if lacking:
            return False, "no_fit", {"lacking": lacking, "need": need, "free": dict(free)}
        if top is not None:
            # EASY backfill: once the top task has waited long enough, only tasks that are
            # expected to finish before it can start may jump ahead of it.
            if reservation and shadow is not None and now + expected_remaining(t) > shadow:
                return False, "reserved", {"for": top.id}
            contested = any(need[r] > EPS for r in top_lacking if r in RESOURCES)
            if top.agent_id != t.agent_id and contested:
                share = agent_share(t.agent_id, t)
                if share > fair_frac(t.agent_id) + pol.share_tolerance:
                    return False, "fair_share", {"share": share, "fair": fair_frac(t.agent_id), "for": top.id}
        return True, "", {}

    def admit(t: TaskView) -> None:
        nonlocal commit
        need = need_of(t)
        for r in RESOURCES:
            free[r] -= need[r]
        if t.state == "queued":
            commit += t.ram
            active[t.agent_id] = active.get(t.agent_id, 0) + 1
        run_task(t)

    waits: dict[int, tuple[str, dict]] = {}
    for t in candidates():
        if t.id in touched or (t.state == "suspended" and t.suspend_reason == "user"):
            continue
        if t.state == "queued" and t.hold:  # its series or the task it goes after comes first
            waits[t.id] = t.hold
            continue
        ok, reason, detail = start_ok(t)
        if ok:
            admit(t)
            continue
        waits[t.id] = (reason, detail)
        if reason == "no_fit":
            blocked.append((t, detail["lacking"]))
            if top is None:
                top, top_lacking = t, detail["lacking"]
                if now - t.queued_since >= pol.reservation_after_s and "commit" not in detail["lacking"]:
                    reservation = True
                    shadow = shadow_time(need_of(t), free, list(running.values()), now, detail["lacking"])

    # 6. fairness preemption: at most one decision per tick, never while settling
    if pol.fair_preemption and blocked and not settling and not P.shed:
        leaving = [t for t in running.values() if t.stopping]
        for w, lacking in blocked:
            if "commit" in lacking:
                continue
            threshold = pol.preempt_after_short_s if w.time_s <= pol.short_task_s else pol.preempt_after_s
            if now - w.queued_since < threshold:
                continue
            starving = now - w.queued_since >= pol.starvation_after_s
            if not starving and agent_share(w.agent_id, w) > fair_frac(w.agent_id) + pol.share_tolerance:
                continue
            need = need_of(w)
            if leaving:  # tasks saving a checkpoint free their share soon: no new victims meanwhile
                soon = dict(free)
                for t in leaving:
                    for r, v in reserved(t).items():
                        soon[r] += v
                if not any(need[r] > soon[r] + EPS for r in lacking if r in RESOURCES):
                    waits[w.id] = ("stopping", {"for": [t.id for t in leaving]})
                    continue
            victims = pick_victims(w, need, free, [t for t in running.values() if not t.stopping], score, now, pol,
                                   agent_share, fair_frac, lacking)
            if victims is None:
                continue
            needs_vram = need["vram"] > free["vram"] + EPS
            pending = []
            for v in victims:
                held = reserved(v)
                if needs_vram and v.preempt == "requeue":  # only a stopped process gives VRAM back
                    if not evict(v, f"fair:{w.id}", urgent=False):
                        pending.append(v.id)                # saves a checkpoint first; not free yet
                        continue
                    gain = held
                else:
                    move(v, "suspend", f"fair:{w.id}")
                    gain = {"cpu": held["cpu"], "ram": held["ram"],
                            "vram": max(0.0, v.vram - v.used_vram)}  # declared-but-unused part
                for r in RESOURCES:
                    free[r] += gain[r]
            if pending:
                waits[w.id] = ("stopping", {"for": pending})
            else:
                waits.pop(w.id, None)
                admit(w)
            P.shed = True
            break

    # 7. CPU caps
    run = list(running.values())
    alloc = allocate_cpu(pools.cpu, run, weights)
    for t in run:
        cap = alloc[t.id]
        # a soft start: the CPU of a task just started or resumed grows to its claim over a minute or two, so the
        # cooling keeps up and the temperature regulator does not swing
        since = now - t.last_start_at if t.last_start_at is not None else None
        if pol.ramp_s > 0 and since is not None and 0 <= since < pol.ramp_s and t.state == "running":
            frac = pol.ramp_start + (1.0 - pol.ramp_start) * since / pol.ramp_s
            cap = min(cap, max(0.5, t.cpu * frac))
        P.cpu_caps[t.id] = max(inp.capacity.cpu * 0.01, min(inp.capacity.cpu, cap))

    # where each running task stands against the waiting ones (the same rules as pick_victims)
    for t in running.values():
        held = now - (t.last_start_at if t.last_start_at is not None else now)
        share, fair = agent_share(t.agent_id), fair_frac(t.agent_id)
        if t.stopping:
            st = {"kind": "stopping"}
        elif held < pol.min_slice_s:
            st = {"kind": "protected", "left_s": round(pol.min_slice_s - held)}
        elif is_overtime(t, pol):
            st = {"kind": "overtime"}
        elif t.priority >= 2:
            st = {"kind": "owner"}
        else:
            st = {"kind": "over" if share - fair > pol.share_tolerance else "fair"}
        st.update(share=round(share, 3), fair=round(fair, 3))
        P.standing[t.id] = st

    # queue annotations, in score order
    order = sorted((t for t in list(queued.values()) + list(suspended.values())),
                   key=lambda t: (-score[t.id], t.queued_since, t.id))
    pos = 0
    for t in order:
        if t.state == "suspended" and t.suspend_reason in ("user", "hung"):  # waits for a manual resume, not in line
            P.waiting[t.id] = WaitInfo(0, t.suspend_reason)
            continue
        pos += 1
        if t.state == "suspended" and t.id not in waits:
            P.waiting[t.id] = WaitInfo(pos, t.suspend_reason or "")
        else:
            reason, detail = waits.get(t.id, ("", {}))
            P.waiting[t.id] = WaitInfo(pos, reason, detail)
    return P


def water_fill(total: float, demands: dict, weights: dict) -> dict:
    """Max-min fair split of `total`: nobody gets more than asked, the rest is shared by weight."""
    out = dict.fromkeys(demands, 0.0)
    active = {k for k, d in demands.items() if d > EPS}
    left = max(0.0, total)
    while active and left > EPS:
        wsum = sum(weights.get(k, 1.0) for k in active)
        give = {k: left * weights.get(k, 1.0) / wsum for k in active}
        full = {k for k in active if out[k] + give[k] >= demands[k] - EPS}
        if not full:
            for k in active:
                out[k] += give[k]
            break
        for k in full:
            left -= demands[k] - out[k]
            out[k] = demands[k]
            active.discard(k)
    return out


def allocate_cpu(pool: float, running: list[TaskView], agent_weights: dict) -> dict[int, float]:
    """Higher priority first; within a priority max-min fair between agents (by weight), then between an
    agent's tasks; CPU nobody asked for is spread over all tasks in proportion to their requests."""
    alloc = {t.id: 0.0 for t in running}
    left = max(0.0, pool)
    for prio in (2, 1, 0):
        by_agent: dict[int, list[TaskView]] = {}
        for t in running:
            if t.priority == prio:
                by_agent.setdefault(t.agent_id, []).append(t)
        if not by_agent or left <= EPS:
            continue
        shares = water_fill(left, {a: sum(t.cpu for t in ts) for a, ts in by_agent.items()},
                            {a: agent_weights.get(a, 1.0) for a in by_agent})
        for a, ts in by_agent.items():
            alloc.update(water_fill(shares[a], {t.id: t.cpu for t in ts}, {}))
        left -= sum(shares.values())
    if left > EPS and running:
        total = sum(max(0.1, t.cpu) for t in running)
        for t in running:
            alloc[t.id] += left * max(0.1, t.cpu) / total
    return alloc


def shadow_time(need: dict, free: dict, running: list[TaskView], now: float,
                lacking: list[str]) -> float | None:
    """When the blocked top task can expect to fit, from running tasks' declared end times (EASY backfill)."""
    f = dict(free)

    def short() -> bool:
        return any(need[r] > f[r] + EPS for r in lacking if r in RESOURCES)

    if not short():
        return now
    for t in sorted(running, key=expected_remaining):
        for r, v in reserved(t).items():
            f[r] += v
        if not short():
            return now + expected_remaining(t)
    return None


def pick_victims(w: TaskView, need: dict, free: dict, running, score: dict, now: float, pol: Policy,
                 agent_share, fair_frac, lacking: list[str]) -> list[TaskView] | None:
    """Smallest greedy set of running tasks whose suspension lets `w` fit, or None."""
    over = {}
    cands = []
    for r in running:
        overtime = is_overtime(r, pol)
        if r.agent_id == w.agent_id or (r.priority > w.priority and not overtime):
            continue
        if now - (r.last_start_at if r.last_start_at is not None else now) < pol.min_slice_s:
            continue
        if r.agent_id not in over:
            over[r.agent_id] = agent_share(r.agent_id) - fair_frac(r.agent_id)
        if not overtime and over[r.agent_id] <= pol.share_tolerance:
            continue
        cands.append((not overtime, -over[r.agent_id], score[r.id], r.last_start_at or 0.0, r))
    cands.sort(key=lambda c: c[:4])
    deficit = {r: (need[r] - free[r]) if r in lacking else 0.0 for r in RESOURCES}
    chosen = []
    for *_, r in cands:
        if all(d <= EPS for d in deficit.values()):
            break
        held = reserved(r)
        gain = {"cpu": held["cpu"], "ram": held["ram"],
                "vram": held["vram"] if r.preempt == "requeue" else max(0.0, r.vram - r.used_vram)}
        if not any(deficit[k] > EPS and gain[k] > EPS for k in RESOURCES):
            continue
        chosen.append(r)
        for k in RESOURCES:
            deficit[k] -= gain[k]
    if chosen and all(d <= EPS for d in deficit.values()):
        return chosen
    return None
