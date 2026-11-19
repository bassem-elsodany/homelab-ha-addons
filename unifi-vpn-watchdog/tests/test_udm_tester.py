"""UdmTester: per-tunnel verdicts from the UDM's own test, with caching, back-off and the reasons to do nothing."""
from __future__ import annotations

from conftest import FakeClock
from vpn_watchdog.config import ProbeCfg, SshCfg
from vpn_watchdog.models import Snapshot, Tunnel
from vpn_watchdog.probe import UdmTester, make_tester
from vpn_watchdog.udm import UdmError

OK = "4 packets transmitted, 4 received, 0% packet loss, time 3002ms rtt min/avg/max/mdev = 90.0/96.6/99.0/1.9 ms"
BAD = "4 packets transmitted, 0 received, 100% packet loss, time 3098ms"
SLOW = "4 packets transmitted, 3 received, 25% packet loss, time 3002ms rtt min/avg/max/mdev = 90.0/96.6/99.0/1.9 ms"


def out(*tunnels, load=1.0, wan="ok", reprov=0):
    lines = [f"#load {load}", f"#reprov {reprov}", f"#wan {wan}"]
    lines += [f"#tunnel wgclt{i} {ip} | {ping} | {http}" for i, (ip, ping, http) in enumerate(tunnels, 1)]
    return "\n".join(lines) + "\n"


class FakeSsh:
    def __init__(self, *outputs):
        self.outputs, self.calls = list(outputs), 0

    def run(self, script):
        self.calls += 1
        o = self.outputs[min(self.calls - 1, len(self.outputs) - 1)]
        if isinstance(o, Exception):
            raise o
        return o


def make(ssh, **probe):
    clock = FakeClock()
    cfg = ProbeCfg(ssh=SshCfg(host="udm.example", user="watchdog", key_file="udm_ssh_key"), **probe)
    return UdmTester(cfg, ssh, clock), clock


def tun(ip="10.5.23.2"):
    return Tunnel("t1", "tunnel-one", True, f"{ip}/24")


SNAP = Snapshot({}, {}, [], {})
LONDON, ROME = ("10.5.23.2", BAD, "000 6.0"), ("10.5.13.2", OK, "200 0.3")


def test_a_dead_tunnel_gives_a_failed_result_and_an_ok_one_gives_ok():
    t, _ = make(FakeSsh(out(LONDON, ROME)))
    bad = t.test(tun("10.5.23.2"), SNAP, pre=False)
    good = t.test(tun("10.5.13.2"), SNAP, pre=False)
    assert bad.ok is False and bad.reason.startswith("dead:")
    assert good.ok is True and good.reason == "ok"


def test_a_slow_tunnel_still_passes_but_says_so():
    t, _ = make(FakeSsh(out(("10.5.23.2", SLOW, "200 0.4"))))
    r = t.test(tun(), SNAP, pre=False)
    assert r.ok is True and r.reason.startswith("slow:")


def test_unknown_is_never_dead():
    t, clock = make(FakeSsh(UdmError("ssh to the UDM failed")))
    assert t.test(tun(), SNAP, pre=False) is None                                    # the test could not run
    t, _ = make(FakeSsh(out(ROME)))
    assert t.test(tun("10.5.99.2"), SNAP, pre=False) is None                         # this tunnel was not in the reading
    t, clock = make(FakeSsh(out(LONDON), UdmError("down")))
    assert t.test(tun(), SNAP, pre=False).ok is False
    clock.t += 400                                                                   # the reading is now old and the UDM cannot be reached
    assert t.test(tun(), SNAP, pre=False) is None


def test_not_configured_means_no_verdicts_and_no_ssh():
    ssh = FakeSsh(out(LONDON))
    t = UdmTester(ProbeCfg(), ssh, FakeClock())
    assert t.enabled is False and t.test(tun(), SNAP, pre=False) is None and ssh.calls == 0


def test_regular_tests_reuse_the_reading_for_55_seconds_but_a_pre_test_always_reads_fresh():
    ssh = FakeSsh(out(ROME))
    t, clock = make(ssh)
    t.test(tun("10.5.13.2"), SNAP, pre=False)
    clock.t += 30
    t.test(tun("10.5.13.2"), SNAP, pre=False)
    assert ssh.calls == 1
    t.test(tun("10.5.13.2"), SNAP, pre=True)                                         # the candidate was not up in the old reading
    assert ssh.calls == 2
    clock.t += 56
    t.test(tun("10.5.13.2"), SNAP, pre=False)
    assert ssh.calls == 3


def test_ssh_failures_back_off_1_2_4_minutes_capped_at_15_minutes():
    ssh = FakeSsh(UdmError("down"))
    t, clock = make(ssh)
    stamps = []
    for _ in range(7200 // 15):                                                      # 2 hours of 15 s cycles
        before = ssh.calls
        t.test(tun(), SNAP, pre=False)
        if ssh.calls > before:
            stamps.append(clock.t)
        clock.t += 15
    assert len(stamps) <= 12
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert gaps[0] >= 60 and gaps[1] >= 120 and gaps[2] >= 240 and max(gaps) <= 915


def test_a_success_ends_the_back_off():
    ssh = FakeSsh(UdmError("down"), out(ROME))
    t, clock = make(ssh)
    t.test(tun("10.5.13.2"), SNAP, pre=False)
    clock.t += 61
    assert t.test(tun("10.5.13.2"), SNAP, pre=False).ok is True
    clock.t += 56
    t.test(tun("10.5.13.2"), SNAP, pre=False)
    assert ssh.calls == 3                                                            # back to one reading a minute


def test_load_above_the_limit_gives_no_verdict_and_context_says_overloaded():
    t, _ = make(FakeSsh("#load 6.5\n"), load_max=4.0)
    assert t.test(tun(), SNAP, pre=False) is None
    assert "overloaded" in t.context() and "6.5" in t.context()


def test_context_reasons():
    t, _ = make(FakeSsh(out(LONDON, ROME, wan="fail")))
    t.test(tun(), SNAP, pre=False)
    assert t.context() == "the plain internet connection is down"
    t, _ = make(FakeSsh(out(LONDON, ("10.5.13.2", BAD, "000 6.0"))))
    t.test(tun(), SNAP, pre=False)
    assert t.context() == "every VPN failed the test"
    t, _ = make(FakeSsh(out(LONDON, ROME)))
    t.test(tun(), SNAP, pre=False)
    assert t.context() is None                                                       # one dead, one fine: it is the tunnel
    t, _ = make(FakeSsh(out(LONDON)))
    t.test(tun(), SNAP, pre=False)
    assert t.context() is None                                                       # a single probed tunnel is not "every VPN"
    assert make(FakeSsh(out()))[0].context() is None                               # nothing read yet: nothing to say


def test_settled_is_false_while_the_udm_reprovisioned_in_the_last_minute():
    t, clock = make(FakeSsh(out(ROME, reprov=2), out(ROME, reprov=0)))
    t.test(tun("10.5.13.2"), SNAP, pre=False)
    assert t.settled() is False
    clock.t += 61
    t.test(tun("10.5.13.2"), SNAP, pre=False)
    assert t.settled() is True
    assert make(FakeSsh(UdmError("x")))[0].settled() is True                       # cannot ask: not a reason to block


def test_can_pretest_and_make_tester_picks_the_right_class():
    t, _ = make(FakeSsh(out()))
    assert t.can_pretest is True and t.enabled is True
    cfg = ProbeCfg(ssh=SshCfg(host="udm.example", user="watchdog", key_file="udm_ssh_key"))
    assert isinstance(make_tester(cfg, None, FakeClock(), None), UdmTester)
    assert not isinstance(make_tester(ProbeCfg(), None, FakeClock(), None), UdmTester)


def test_one_reading_gives_one_verdict_per_tunnel_even_when_asked_again():
    t, clock = make(FakeSsh(out(LONDON)))
    assert t.test(tun("10.5.23.2"), SNAP, pre=False).ok is False
    clock.t += 15                                                                    # a short probe interval asks again within the cache time
    assert t.test(tun("10.5.23.2"), SNAP, pre=False) is None                         # same measurement: no new evidence
    clock.t += 50
    assert t.test(tun("10.5.23.2"), SNAP, pre=False).ok is False                     # a new measurement counts again


def test_an_old_reading_kept_through_an_ssh_failure_is_not_counted_a_second_time():
    t, clock = make(FakeSsh(out(LONDON), UdmError("down")))
    assert t.test(tun("10.5.23.2"), SNAP, pre=False).ok is False
    clock.t += 60
    assert t.test(tun("10.5.23.2"), SNAP, pre=False) is None                         # the test could not run: nothing new, the old DEAD is not re-used


def test_a_pre_test_reading_is_not_counted_again_as_a_regular_test():
    t, clock = make(FakeSsh(out(ROME)))
    assert t.test(tun("10.5.13.2"), SNAP, pre=True).ok is True
    clock.t += 10
    assert t.test(tun("10.5.13.2"), SNAP, pre=False) is None
