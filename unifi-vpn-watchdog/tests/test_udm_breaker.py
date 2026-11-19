"""When the switching limit is reached the group stops and asks; nothing repeats until you resume it."""
from __future__ import annotations

from test_udm_gates import DEAD, OK, Gate, engine, enables, gs, rounds
from conftest import tid
from vpn_watchdog.config import ProbeCfg
from vpn_watchdog.engine import Engine
from vpn_watchdog.state import StateStore

ORDER = "[Home-Primary, Home-Backup, Cousin vpn 2]"
ALL = ["Home-Primary", "Home-Backup", "Cousin vpn 2"]


def stopped(make_engine, **kw):
    tester = Gate(dead=ALL)
    eng, un, notes, clock = engine(make_engine, ORDER, tester, **kw)
    rounds(eng, clock, 20)
    return eng, un, notes, clock, tester


def test_reaching_the_hourly_cap_pauses_the_group_and_sends_one_check_stopped_alert(make_engine):
    eng, un, notes, clock, _ = stopped(make_engine)
    assert gs(eng).paused and gs(eng).stopped
    assert notes.kinds().count("check_stopped") == 1
    assert gs(eng).last_decision.startswith("stopped:") and "Resume" in gs(eng).last_decision
    assert len(enables(un)) == 3                                            # three attempts, then it stopped


def test_a_paused_group_makes_no_further_unifi_writes_until_resumed(make_engine):
    eng, un, notes, clock, tester = stopped(make_engine)
    writes = len(un.calls)
    rounds(eng, clock, 40)
    assert len(un.calls) == writes and notes.kinds().count("check_stopped") == 1
    tester.dead = {tid("Home-Primary")}                                     # you fixed Backup
    eng.submit("resume", "g1")
    rounds(eng, clock, 4)
    assert not gs(eng).paused and not gs(eng).stopped
    assert gs(eng).current_id == tid("Home-Backup")                         # history and bans were cleared: it chooses again at once


def test_on_stop_keep_leaves_the_vpn_clients_as_they_are(make_engine):
    eng, un, notes, clock, _ = stopped(make_engine)
    assert tid("Home-Primary") in un.enabled and not [c for c in un.calls if c[0] == "enable" and c[2] is False and c[1] == tid("Home-Primary")]


def test_on_stop_direct_switches_the_groups_vpn_clients_off_through_the_toggle_budget(make_engine):
    tester = Gate(dead=ALL)
    eng, un, notes, clock = engine(make_engine, ORDER, tester)
    eng.cfg = eng.cfg.model_copy(update={"probe": ProbeCfg(on_stop="direct")})
    rounds(eng, clock, 20)
    assert gs(eng).paused and un.active_names() == []                       # the VLAN now uses the normal internet
    assert len(eng._tunnel_toggles) >= 3                                    # and every switch-off was counted by the budget
    rounds(eng, clock, 20)
    assert un.active_names() == []                                          # nothing switches a client back on while stopped


def test_counters_and_bans_survive_a_restart(make_engine, tmp_path):
    eng, un, notes, clock, _ = stopped(make_engine)
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.groups, store.tunnels = eng.store.groups, eng.store.tunnels
    store.touch()
    store.save()
    again = StateStore(path)
    g = again.group("g1")
    assert g.paused and g.stopped and len(g.switches) == 3 and g.last_attempt_ts == gs(eng).last_attempt_ts
    assert any(t.fail_streak >= 1 for t in again.tunnels.values())            # the bans' history is kept too
    new = Engine(eng.cfg, un, eng.tester, notes, again, clock)
    rounds(new, clock, 8)
    assert new.store.group("g1").paused and len(enables(un)) == 3           # the new process does not start over
