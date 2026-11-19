"""The gates around switching when the UDM test is active: hold, daily cap, one candidate per attempt, settle, context."""
from __future__ import annotations

from conftest import FakeUniFi, run, tid
from vpn_watchdog.config import DetectionCfg, SwitchingCfg
from vpn_watchdog.models import ProbeResult

DEAD = ProbeResult(False, "dead: no HTTPS answer, 100% ping loss (tested from the UDM)")
OK = ProbeResult(True, "ok")


class Gate:
    """The UDM tester as the engine sees it, with the verdicts and the gate answers set by the test."""

    enabled = True
    can_pretest = True
    one_candidate_per_attempt = True

    def __init__(self, dead=(), pre_fail=()):
        self.dead, self.pre_fail = {tid(n) for n in dead}, {tid(n) for n in pre_fail}
        self.ctx, self.is_settled = None, True

    def test(self, tunnel, snap, *, pre, expect=None):
        if pre:
            return DEAD if tunnel.id in self.pre_fail | self.dead else OK         # one verdict per tunnel, like the real tester
        return DEAD if tunnel.id in self.dead else OK

    def context(self):
        return self.ctx

    def settled(self):
        return self.is_settled


def engine(make_engine, order, tester, **switching):
    eng, un, _, notes, clock = make_engine(order=order)
    eng.tester = tester
    eng.cfg = eng.cfg.model_copy(update={"detection": DetectionCfg(), "switching": SwitchingCfg(**switching)})
    return eng, un, notes, clock


def rounds(eng, clock, n):
    run(eng, clock, 4 * n)            # one test every 60 s


def gs(eng):
    return eng.store.group("g1")


def enables(un):
    return [c[1] for c in un.calls if c[0] == "enable" and c[2] is True]


def test_the_defaults_are_the_agreed_gates():
    s = SwitchingCfg()
    assert (s.min_hold_seconds, s.max_switches_per_hour, s.max_switches_per_day, s.attempt_gap_seconds) == (600, 3, 6, 120)
    from vpn_watchdog.config import FailbackCfg
    assert FailbackCfg().stable_seconds == 600


def test_no_switch_within_the_minimum_hold_of_the_last_one(make_engine):
    tester = Gate(dead=["Home-Primary"])
    eng, un, notes, clock = engine(make_engine, "[Home-Primary, Home-Backup, Cousin vpn 2]", tester)
    rounds(eng, clock, 4)
    assert gs(eng).current_id == tid("Home-Backup")                    # Primary was dead: the first switch
    tester.dead = {tid("Home-Backup")}                                 # now Backup dies and Primary is back
    rounds(eng, clock, 20)
    sw = gs(eng).switches
    assert len(sw) == 2 and sw[1] - sw[0] >= 600                       # the second one only ten minutes after the first
    assert enables(un) == [tid("Home-Backup"), tid("Home-Primary")] and "blocked" in notes.kinds()


def test_daily_cap_blocks_the_seventh_switch_in_24_hours_and_the_history_is_kept_for_24_hours(make_engine):
    eng, un, notes, clock = engine(make_engine, "[Home-Primary, Home-Backup]", Gate(), max_switches_per_hour=100, min_hold_seconds=0)
    g = eng.cfg.groups[0]
    st = eng.cfg.settings_for(g)
    now = clock.now()
    gs(eng).switches = [now - 3600 * 2 - 600 * i for i in range(6)]      # six switches, none in the last hour
    assert eng._rate_ok(g, st, gs(eng), hard=True) is False and "day" in gs(eng).last_decision
    gs(eng).switches = [now - 3600 * 2 - 600 * i for i in range(5)]
    assert eng._rate_ok(g, st, gs(eng), hard=True) is True
    gs(eng).switches = [now - 90_000, now - 3600 * 10]                   # one is older than 24 h
    eng._commit(g, st, gs(eng), eng._snap.tunnels[tid("Home-Backup")] if eng._snap else eng.unifi.snapshot().tunnels[tid("Home-Backup")], "test", "switch")
    assert len(gs(eng).switches) == 2 and now - 90_000 not in gs(eng).switches


def test_one_candidate_is_tried_per_attempt_and_the_next_attempt_waits_120_seconds(make_engine):
    eng, un, notes, clock = engine(make_engine, "[Home-Primary, Home-Backup, Cousin vpn 2]",
                                   Gate(dead=["Home-Primary"], pre_fail=["Home-Backup"]), min_hold_seconds=0)
    rounds(eng, clock, 3)                                              # three dead tests: the first attempt
    first = enables(un)
    assert first == [tid("Home-Backup")]                               # one candidate only, and its pre-test failed
    run(eng, clock, 4)                                                 # 60 s later: still inside the 120 s gap
    assert enables(un) == first
    run(eng, clock, 4)                                                 # 120 s: the ban on Backup is over, so it is the next attempt (the order is yours)
    assert enables(un) == [tid("Home-Backup"), tid("Home-Backup")]
    run(eng, clock, 8)                                                 # 240 s: Backup is banned for longer now, so the next candidate is tried
    assert enables(un) == [tid("Home-Backup"), tid("Home-Backup"), tid("Cousin vpn 2")]
    assert len(gs(eng).switches) == 3                                  # every attempt counted toward the hourly limit


def test_no_switch_while_the_udm_reprovisioned_in_the_last_minute(make_engine):
    tester = Gate(dead=["Home-Primary"])
    tester.is_settled = False
    eng, un, notes, clock = engine(make_engine, "[Home-Primary, Home-Backup]", tester)
    rounds(eng, clock, 8)
    assert enables(un) == [] and gs(eng).current_id == tid("Home-Primary")
    tester.is_settled = True
    rounds(eng, clock, 2)
    assert gs(eng).current_id == tid("Home-Backup")


def test_when_every_tunnel_is_dead_or_the_internet_is_down_nothing_is_switched_and_check_hold_is_sent_once_per_30_minutes(make_engine):
    tester = Gate(dead=["Home-Primary"])
    tester.ctx = "every VPN failed the test"
    eng, un, notes, clock = engine(make_engine, "[Home-Primary, Home-Backup]", tester)
    rounds(eng, clock, 25)
    assert enables(un) == [] and gs(eng).current_id == tid("Home-Primary")
    assert notes.kinds().count("check_hold") == 1
    rounds(eng, clock, 10)                                              # 35 minutes in
    assert notes.kinds().count("check_hold") == 2
    tester.ctx = None                                                   # the cause went away: the normal failover runs
    rounds(eng, clock, 3)
    assert gs(eng).current_id == tid("Home-Backup")


def test_only_tunnel_dead_and_no_other_candidate_means_no_switching_and_one_alert(make_engine):
    eng, un, notes, clock = engine(make_engine, "[Home-Primary]", Gate(dead=["Home-Primary"]))
    rounds(eng, clock, 20)
    assert enables(un) == [] and notes.kinds().count("exhausted") == 1 and "switch" not in notes.kinds()
    writes = len(un.calls)
    rounds(eng, clock, 20)
    assert len(un.calls) == writes                                      # and nothing repeats
