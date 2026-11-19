"""Exit-IP probing and the canary that lets us test any tunnel before production traffic is moved to it.

A tunnel can report CONNECTED while passing no traffic, so the decisive check is: fetch our public IP through it
and require (a) the request works, (b) the country matches the tunnel's ISO code, (c) it is not the WAN IP (leak).
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from .clock import Clock
from .config import ProbeCfg, ProbeEndpoint
from .models import ProbeResult, Snapshot, Tunnel, UdmReading
from .udm import UdmError, UdmSsh, build_script, parse_reading
from .unifi import UniFiClient

log = logging.getLogger("vpn_watchdog.probe")


def _dig(data: Any, path: str | None) -> Any:
    if not path:
        return None
    for part in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def parse_endpoint(ep: ProbeEndpoint, body: str) -> tuple[str | None, str | None]:
    if ep.format == "text":
        return body.strip() or None, None
    if ep.format == "cloudflare_trace":
        kv = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)
        return kv.get("ip"), kv.get("loc")
    import json

    data = json.loads(body)
    ip, country = _dig(data, ep.ip_field), _dig(data, ep.country_field)
    return (str(ip) if ip else None), (str(country) if country else None)


class Prober:
    def __init__(self, cfg: ProbeCfg, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        if transport is None and cfg.source_address:
            transport = httpx.HTTPTransport(local_address=cfg.source_address)
        self._http = httpx.Client(timeout=cfg.timeout_seconds, transport=transport, headers={"User-Agent": "unifi-vpn-watchdog/0.1"})

    def _accepted(self, expected_iso: str | None) -> set[str]:
        if not expected_iso:
            return set()
        iso = expected_iso.upper()
        return {iso, *[a.upper() for a in self.cfg.country_aliases.get(iso, [])]}

    def probe(self, expected_iso: str | None, wan_ip: str | None) -> ProbeResult:
        wan_ip = self.cfg.wan_ip or wan_ip
        accepted = self._accepted(expected_iso)
        good: list[ProbeResult] = []
        bad: list[str] = []
        leak = False
        for ep in self.cfg.endpoints:
            try:
                r = self._http.get(ep.url)
                r.raise_for_status()
                ip, country = parse_endpoint(ep, r.text)
            except (httpx.HTTPError, ValueError) as e:
                bad.append(f"{ep.name}: {type(e).__name__}")
                continue
            if not ip:
                bad.append(f"{ep.name}: no ip in response")
                continue
            if wan_ip and ip == wan_ip:
                leak = True
                bad.append(f"{ep.name}: LEAK exit ip equals WAN ip {ip}")
                continue
            if self.cfg.check_country and accepted and country and country.upper() not in accepted:
                bad.append(f"{ep.name}: country {country} != {expected_iso}")
                continue
            good.append(ProbeResult(True, ip=ip, country=country))
            if len(good) >= self.cfg.require:
                break
        if len(good) >= self.cfg.require:
            return ProbeResult(True, "ok", ip=good[0].ip, country=good[0].country, details=bad)
        return ProbeResult(False, "; ".join(bad) or "no probe endpoints", leak=leak, details=bad)


class TunnelTester:
    """Single entry point the engine uses to ask 'does traffic really flow through this tunnel?'.

    The only active mode is `direct`: the probe runs from this host's own egress, so it only says something when this host is routed
    through the VPN. Steering a separate test device through each tunnel would mean changing a routing policy, which the watchdog never does."""

    def __init__(self, cfg: ProbeCfg, unifi: UniFiClient, prober: Prober, clock: Clock):
        self.cfg = cfg
        self.unifi = unifi
        self.prober = prober
        self.clock = clock

    @property
    def enabled(self) -> bool:
        return self.cfg.mode == "direct"

    @property
    def can_pretest(self) -> bool:
        return False

    one_candidate_per_attempt = False        # the UDM test tries one candidate per attempt and spaces attempts out

    def context(self) -> str | None:
        """A reason to change nothing right now (not used by this tester)."""
        return None

    def settled(self) -> bool:
        return True

    def test(self, tunnel: Tunnel, snap: Snapshot, *, pre: bool, expect: str | None = None) -> ProbeResult | None:
        """Returns None when this tunnel cannot be tested in the current mode."""
        if self.cfg.mode != "direct" or pre:
            return None
        return self.prober.probe(expect, snap.wan_ip)


class UdmTester(TunnelTester):
    """The real test: a ping and one HTTPS request sent through each VPN client that is up, run on the UDM itself over SSH (UniFi's API
    cannot do this, and its "CONNECTED" stays true while a tunnel passes nothing).

    A verdict is only given when the test really ran and saw the tunnel. Anything else (SSH down, the UDM too busy, the tunnel not up, an old
    reading) is "no verdict" (None), which the engine never acts on."""

    CACHE_SECONDS = 55           # a regular test reuses the last reading for this long: one SSH run a minute
    STALE_SECONDS = 150          # a reading older than this says nothing any more
    BACKOFF_MAX = 900.0          # after a failed SSH run wait 1, 2, 4 ... minutes, at most this many seconds

    def __init__(self, cfg: ProbeCfg, ssh: UdmSsh, clock: Clock):
        self.cfg, self.ssh, self.clock = cfg, ssh, clock
        self.unifi = self.prober = None
        self._last: UdmReading | None = None
        self._fails = 0
        self._retry_at = 0.0

    @property
    def enabled(self) -> bool:
        return self.cfg.ssh.configured

    @property
    def can_pretest(self) -> bool:
        return True

    one_candidate_per_attempt = True

    def _usable(self) -> UdmReading | None:
        last = self._last
        return last if last is not None and self.clock.now() - last.at <= self.STALE_SECONDS else None

    def reading(self, fresh: bool = False) -> UdmReading | None:
        """The latest reading, running the test over SSH when the cached one is too old (or `fresh`). None when nothing usable exists."""
        if not self.enabled:
            return None
        now = self.clock.now()
        if now < self._retry_at:
            return self._usable()
        if not fresh and self._last is not None and now - self._last.at < self.CACHE_SECONDS:
            return self._last
        try:
            output = self.ssh.run(build_script(self.cfg.load_max))
        except UdmError as e:
            self._fails += 1
            wait = min(self.BACKOFF_MAX, 60.0 * 2 ** (self._fails - 1))
            self._retry_at = now + wait
            log.warning("tunnel test on the UDM failed (%s); next try in %.0fs", e, wait)
            return self._usable()
        self._fails, self._retry_at = 0, 0.0
        self._last = parse_reading(output, now)
        return self._last

    def test(self, tunnel: Tunnel, snap: Snapshot, *, pre: bool, expect: str | None = None) -> ProbeResult | None:
        r = self.reading(fresh=pre)       # a candidate was not up in the cached reading, so a pre-test always looks again
        if r is None or r.load > self.cfg.load_max:
            return None
        p = r.probes.get((tunnel.subnet or "").split("/")[0])
        if p is None:
            return None
        if p.state == "DEAD":
            return ProbeResult(False, f"dead: {p.http or 'no'} HTTPS answer, {p.loss:.0f}% ping loss (tested from the UDM)")
        if p.state == "SLOW":
            return ProbeResult(True, f"slow: {p.loss:.0f}% ping loss, HTTPS in {p.seconds:.1f}s (tested from the UDM)")
        return ProbeResult(True, "ok")

    def context(self) -> str | None:
        """Why nothing should be changed right now, when the cause is probably not one VPN. None when it is fine to act."""
        r = self._usable()
        if r is None:
            return None
        if r.load > self.cfg.load_max:
            return f"the UDM is overloaded (load {r.load:g}): nothing is changed until it calms down"
        if not r.wan_ok:
            return "the plain internet connection is down"
        if len(r.probes) >= 2 and all(p.state == "DEAD" for p in r.probes.values()):
            return "every VPN failed the test"
        return None

    def settled(self) -> bool:
        """False while UniFi re-provisioned its network in the last minute. True when it cannot tell (no reading)."""
        r = self._usable()
        return r is None or r.reprovisions_1m == 0


def make_tester(cfg: ProbeCfg, unifi: UniFiClient | None, clock: Clock, state_dir: str | None) -> TunnelTester:
    """The UDM test when an SSH login is filled in, otherwise the old behaviour (nothing changes without it)."""
    if cfg.ssh.configured:
        return UdmTester(cfg, UdmSsh(cfg.ssh, host_key_file=os.path.join(state_dir, "udm_host_key") if state_dir else None), clock)
    return TunnelTester(cfg, unifi, Prober(cfg), clock)
