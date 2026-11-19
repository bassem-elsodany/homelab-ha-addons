"""The UDM tunnel test: what the fixed read-only script prints, and what the watchdog makes of it."""
from __future__ import annotations

from vpn_watchdog.models import TunnelProbe, UdmReading
from vpn_watchdog.udm import SCRIPT, build_script, parse_reading

PING_OK = "4 packets transmitted, 4 received, 0% packet loss, time 3002ms rtt min/avg/max/mdev = 94.512/96.601/99.236/1.958 ms"
PING_BAD = "4 packets transmitted, 1 received, 75% packet loss, time 4126ms rtt min/avg/max/mdev = 128.370/128.370/128.370/0.000 ms"

OUTPUT = f"""#load 2.45
#reprov 0
#wan ok
#tunnel wgclt21 10.5.25.2 | {PING_OK} | 200 0.309
#tunnel wgclt26 10.5.23.2 | {PING_BAD} | 000 6.0
"""


def probe(loss=0.0, http=200, seconds=0.3):
    return TunnelProbe("wgclt1", "10.5.1.2", loss, 10.0, http, seconds)


def test_state_dead_slow_ok():
    assert probe(http=0).state == "DEAD"
    assert probe(loss=50).state == "DEAD"
    assert probe(loss=25).state == "SLOW"
    assert probe(seconds=3.5).state == "SLOW"
    assert probe().state == "OK"


def test_parse_reading_sections():
    r = parse_reading(OUTPUT, now=1000.0)
    assert isinstance(r, UdmReading) and r.at == 1000.0
    assert set(r.probes) == {"10.5.25.2", "10.5.23.2"}
    assert (r.wan_ok, r.load, r.reprovisions_1m) == (True, 2.45, 0)
    assert r.probes["10.5.25.2"].state == "OK" and r.probes["10.5.25.2"].rtt_ms == 96.601
    assert r.probes["10.5.23.2"].state == "DEAD" and r.probes["10.5.23.2"].loss == 75.0


def test_renumbered_interface_keeps_the_address_key():
    a = parse_reading(f"#tunnel wgclt26 10.5.23.2 | {PING_OK} | 200 0.3\n", 1.0)
    b = parse_reading(f"#tunnel wgclt31 10.5.23.2 | {PING_OK} | 200 0.3\n", 1.0)
    assert set(a.probes) == set(b.probes) == {"10.5.23.2"}
    assert (a.probes["10.5.23.2"].iface, b.probes["10.5.23.2"].iface) == ("wgclt26", "wgclt31")


def test_a_tunnel_with_no_ping_summary_counts_as_100_percent_loss():
    r = parse_reading("#tunnel wgclt7 10.5.11.2 |  | 000 6.0\n", 1.0)
    p = r.probes["10.5.11.2"]
    assert p.loss == 100.0 and p.rtt_ms is None and p.state == "DEAD"


def test_garbage_output_gives_an_empty_reading_not_an_exception():
    r = parse_reading("Permission denied\n#tunnel broken line\n#load abc\n", 5.0)
    assert r.probes == {} and r.load == 0.0 and r.reprovisions_1m == 0 and r.at == 5.0


def test_a_failed_wan_ping_is_reported():
    assert parse_reading("#wan fail\n", 1.0).wan_ok is False


def test_the_script_is_a_fixed_read_only_text():
    assert SCRIPT == build_script()
    assert "ping -I" in SCRIPT and "--interface" in SCRIPT
    for word in ("rm ", "reboot", "ip link set", "ip route", "iptables", "wg set", "> /", ">>"):
        assert word not in SCRIPT
    assert "4.00" in SCRIPT and "7.50" in build_script(7.5)       # the load limit is the only value put into the text
