"""24 simulated hours of bad situations with the real UDM tester: the number of switches and of writes to UniFi stays small and fixed."""
from __future__ import annotations

from conftest import TUNNELS, run, tid
from vpn_watchdog.config import DetectionCfg, ProbeCfg, SshCfg, SwitchingCfg
from vpn_watchdog.probe import UdmTester
from vpn_watchdog.udm import UdmError

OK_PING = "4 packets transmitted, 4 received, 0% packet loss, time 3002ms rtt min/avg/max/mdev = 90.0/96.6/99.0/1.9 ms"
BAD_PING = "4 packets transmitted, 0 received, 100% packet loss, time 3098ms"
DAY = 24 * 3600


class FakeUdm:
    """Answers the UDM test from a scenario: dead(tunnel_name, seconds) -> bool; plus load / re-provision / failure switches."""

    def __init__(self, un, clock, dead=lambda name, t: False, load=1.0, reprov=0, fail=False):
        self.un, self.clock, self.dead, self.load, self.reprov, self.fail, self.calls = un, clock, dead, load, reprov, fail, 0

    def run(self, script):
        self.calls += 1
        if self.fail:
            raise UdmError("ssh to the UDM failed")
        lines = [f"#load {self.load}"]
        if self.load > 4.0:
            return lines[0] + "\n"
        lines += [f"#reprov {self.reprov}", "#wan ok"]
        for i, n in enumerate(TUNNELS):
            if tid(n) in self.un.enabled:
                bad = self.dead(n, self.clock.t - START)
                lines.append(f"#tunnel wgclt{i} 10.5.{10 + i}.2 | {BAD_PING if bad else OK_PING} | {'000 6.0' if bad else '200 0.3'}")
        return "\n".join(lines) + "\n"


START = 0.0


def simulate(make_engine, hours=24, order="[Home-Primary, Home-Backup, Cousin vpn 2]", **scenario):
    global START
    eng, un, _, notes, clock = make_engine(order=order)
    START = clock.t
    udm = FakeUdm(un, clock, **scenario)
    cfg = ProbeCfg(ssh=SshCfg(host="udm.example", user="watchdog", key_file="udm_ssh_key"))
    eng.tester = UdmTester(cfg, udm, clock)
    eng.cfg = eng.cfg.model_copy(update={"detection": DetectionCfg(), "switching": SwitchingCfg(), "probe": cfg})
    toggles = []
    real = un.set_tunnel_enabled
    un.set_tunnel_enabled = lambda i, en: (toggles.append(clock.t), real(i, en))
    run(eng, clock, hours * 240)                                              # 15 s cycles
    return eng, un, notes, udm, toggles


def worst_window(stamps, seconds=600):
    return max((sum(1 for u in stamps if t <= u < t + seconds) for t in stamps), default=0)


def writes(un):
    return len([c for c in un.calls if c[0].startswith("route-") or c[0] == "enable"])


def test_all_tunnels_dead_all_day(make_engine):
    eng, un, notes, udm, toggles = simulate(make_engine, dead=lambda n, t: True)
    assert notes.kinds().count("switch") == 0
    assert notes.kinds().count("check_stopped") == 1 and eng.store.group("g1").paused      # it stopped and asked, once
    assert writes(un) <= 12 and notes.kinds().count("check_hold") <= 48


def test_a_tunnel_that_keeps_dying_while_another_is_fine_never_loops(make_engine):
    # each tunnel is dead for 3 minutes out of every 6, at a different phase: the tunnel in use keeps dying
    dead = lambda n, t: ((t // 60) + 3 * TUNNELS.index(n)) % 6 < 3          # noqa: E731
    eng, un, notes, udm, toggles = simulate(make_engine, dead=dead)
    switches = eng.store.group("g1").switches
    assert len(switches) <= 6 and all(sum(1 for u in switches if t <= u < t + 3600) <= 3 for t in switches)
    assert worst_window(toggles) <= eng.TUNNEL_TOGGLES_MAX and writes(un) <= 40


def test_ssh_unreachable_all_day(make_engine):
    eng, un, notes, udm, toggles = simulate(make_engine, dead=lambda n, t: True, fail=True)
    assert notes.kinds().count("switch") == 0 and writes(un) == 0 and un.active_names() == ["Home-Primary"]
    assert udm.calls <= 100                                                  # 1, 2, 4, 8 minutes, then every 15


def test_udm_load_high_all_day(make_engine):
    eng, un, notes, udm, toggles = simulate(make_engine, dead=lambda n, t: True, load=6.5)
    assert notes.kinds().count("switch") == 0 and un.calls == []            # no verdict, no write


def test_reprovision_storm_reported_by_the_udm_all_day(make_engine):
    eng, un, notes, udm, toggles = simulate(make_engine, dead=lambda n, t: n == "Home-Primary", reprov=5)
    assert notes.kinds().count("switch") == 0 and writes(un) == 0           # the UDM is busy: nothing is added to it


def test_worst_case_never_exceeds_the_ten_minute_budgets_from_1_0_2(make_engine):
    dead = lambda n, t: True                                               # noqa: E731
    eng, un, notes, udm, toggles = simulate(make_engine, dead=dead)
    assert worst_window(toggles) <= eng.TUNNEL_TOGGLES_MAX
    assert len(eng._route_writes) <= eng.ROUTE_WRITES_MAX
