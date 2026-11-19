"""The engine acting on UDM tunnel verdicts: three dead tests in a row switch, unknown counts for nothing, slow only alerts."""
from __future__ import annotations

from conftest import run, tid
from vpn_watchdog.config import DetectionCfg
from vpn_watchdog.models import ProbeResult

DEAD = ProbeResult(False, "dead: no HTTPS answer, 100% ping loss (tested from the UDM)")
SLOW = ProbeResult(True, "slow: 25% ping loss, HTTPS in 0.4s (tested from the UDM)")
OK = ProbeResult(True, "ok")


class Scripted:
    """A tester that answers regular tests from a script (None = no verdict) and passes every pre-test."""

    enabled = True
    can_pretest = True

    def __init__(self, *script):
        self.script, self.regular = list(script), 0

    def test(self, tunnel, snap, *, pre, expect=None):
        if pre:
            return OK
        self.regular += 1
        return self.script.pop(0) if self.script else OK

    def context(self):
        return None

    def settled(self):
        return True


def engine(make_engine, *script):
    eng, un, _, notes, clock = make_engine(order="[Home-Primary, Home-Backup]")
    eng.tester = Scripted(*script)
    eng.cfg = eng.cfg.model_copy(update={"detection": DetectionCfg()})       # the real defaults (the shared test config pins 2 tests every 15 s)
    return eng, un, notes, clock


def rounds(eng, clock, n):
    run(eng, clock, 4 * n)            # one test every 60 s, one cycle every 15 s


def current(eng):
    return eng.store.group("g1").current_id


def test_the_defaults_are_three_tests_every_60_seconds():
    d = DetectionCfg()
    assert d.probe_failure_threshold == 3 and d.probe_interval_seconds == 60


def test_three_dead_tests_in_a_row_fail_the_tunnel_over_but_two_do_not(make_engine):
    eng, un, notes, clock = engine(make_engine, DEAD, DEAD, OK, DEAD, DEAD)
    rounds(eng, clock, 5)
    assert current(eng) == tid("Home-Primary") and "switch" not in notes.kinds()       # dead, dead, ok, dead, dead: never three in a row
    eng.tester.script[:] = [DEAD]
    rounds(eng, clock, 2)
    assert current(eng) == tid("Home-Backup") and "switch" in notes.kinds()


def test_unknown_results_do_not_count_toward_the_three(make_engine):
    eng, un, notes, clock = engine(make_engine, DEAD, None, DEAD, None)
    rounds(eng, clock, 4)
    assert current(eng) == tid("Home-Primary")                                         # two dead tests so far; unknown neither adds nor resets
    eng.tester.script[:] = [DEAD]
    rounds(eng, clock, 2)
    assert current(eng) == tid("Home-Backup")


def test_slow_raises_one_alert_and_never_switches(make_engine):
    eng, un, notes, clock = engine(make_engine, *([SLOW] * 12))
    rounds(eng, clock, 12)
    assert notes.kinds().count("tunnel_slow") == 1 and "switch" not in notes.kinds()  # one alert for 12 minutes of slowness
    eng.tester.script[:] = [SLOW] * 40
    rounds(eng, clock, 40)
    assert notes.kinds().count("tunnel_slow") == 2 and current(eng) == tid("Home-Primary")   # and again after 30 minutes


def test_a_dead_tunnel_that_recovers_raises_tunnel_recovered_once(make_engine):
    eng, un, notes, clock = engine(make_engine, DEAD, DEAD, OK, OK, OK)
    rounds(eng, clock, 5)
    assert notes.kinds().count("tunnel_recovered") == 1 and "switch" not in notes.kinds()


def test_a_single_blip_does_not_raise_recovered(make_engine):
    eng, un, notes, clock = engine(make_engine, DEAD, OK, OK)
    rounds(eng, clock, 3)
    assert "tunnel_recovered" not in notes.kinds()
