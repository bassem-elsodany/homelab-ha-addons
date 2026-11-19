"""The tunnel test that runs on the UDM itself (UniFi's API cannot send a ping or a request through a VPN client).

Everything the watchdog asks the UDM to run is the fixed, read-only text built by `build_script`: no UniFi or user text is ever put into it,
and the only value formatted in is the load limit (a validated number). The output is parsed into a `UdmReading`.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import threading
from typing import Any, Callable

from .config import SshCfg
from .models import TunnelProbe, UdmReading

log = logging.getLogger("vpn_watchdog.udm")


class UdmError(Exception):
    """The UDM could not be reached or answered badly. The message never contains a password."""

_SCRIPT = r"""
l=$(cut -d" " -f1 /proc/loadavg)
echo "#load $l"
if awk -v a="$l" 'BEGIN{exit !(a>@LOADMAX@)}'; then exit 0; fi
echo "#reprov $(journalctl --since "-1min" --no-pager 2>/dev/null | grep -c "vpn/wireguard/clients from [0-9]* to [0-9]*")"
if ping -c1 -W2 -q 1.1.1.1 >/dev/null 2>&1; then echo "#wan ok"; else echo "#wan fail"; fi
for i in $(ip -o link show | grep -o "wgclt[0-9]*" | sort -u); do
  a=$(ip -4 -o addr show dev $i | awk '{print $4}' | cut -d/ -f1)
  p=$(ping -I $i -c4 -W2 -q 1.1.1.1 2>&1 | tail -2 | tr "\n" " ")
  h=$(curl -s -o /dev/null -m6 --interface $i -w "%{http_code} %{time_total}" https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null || echo "000 6.0")
  echo "#tunnel $i $a | $p | $h"
done
"""


def build_script(load_max: float = 4.0) -> str:
    """The script, stopping before any test when the UDM's load is above `load_max`."""
    load_max = float(load_max)
    if not math.isfinite(load_max) or not 0 < load_max <= 100:
        raise ValueError("load_max must be a number between 0 and 100")
    return _SCRIPT.replace("@LOADMAX@", f"{load_max:.2f}")


SCRIPT = build_script()

_LOSS = re.compile(r"([0-9.]+)% packet loss")
_RTT = re.compile(r"= [0-9.]+/([0-9.]+)/")


def _number(text: str, default: float) -> float:
    try:
        return float(text)
    except ValueError:
        return default


def parse_reading(output: str, now: float) -> UdmReading:
    """Turn the script's output into numbers. Anything unreadable is ignored, never an exception."""
    probes: dict[str, TunnelProbe] = {}
    load, reprov, wan_ok = 0.0, 0, False
    for line in output.splitlines():
        if line.startswith("#load "):
            load = _number(line[6:].strip(), 0.0)
        elif line.startswith("#reprov "):
            reprov = int(_number(line[8:].strip(), 0.0))
        elif line.startswith("#wan "):
            wan_ok = line[5:].strip() == "ok"
        elif line.startswith("#tunnel "):
            parts = [x.strip() for x in line[8:].split("|")]
            if len(parts) != 3:
                continue
            head, ping, http = parts
            iface, _, ip = head.partition(" ")
            if not iface or not ip.strip():
                continue
            loss, rtt = _LOSS.search(ping), _RTT.search(ping)
            code, _, secs = http.partition(" ")
            probes[ip.strip()] = TunnelProbe(
                iface, ip.strip(), float(loss.group(1)) if loss else 100.0, float(rtt.group(1)) if rtt else None,
                int(code) if code.isdigit() else 0, _number(secs, 6.0))
    return UdmReading(probes, load, wan_ok, reprov, now)


class _ParamikoClient:
    """The real SSH client (paramiko is only imported when a connection is really made)."""

    def __init__(self) -> None:
        self._c: Any = None

    def connect(self, host: str, port: int, user: str, password: str | None, key_file: str | None, timeout: float) -> None:
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())        # the key is pinned by UdmSsh before anything runs
        c.connect(host, port=port, username=user, password=password or None, key_filename=key_file or None, timeout=timeout,
                  banner_timeout=timeout, auth_timeout=timeout, allow_agent=False, look_for_keys=False)
        self._c = c

    def server_key_fingerprint(self) -> str:
        key = self._c.get_transport().get_remote_server_key()
        return f"{key.get_name()} {hashlib.sha256(key.asbytes()).hexdigest()}"

    def exec(self, script: str, timeout: float) -> tuple[str, int]:
        _, stdout, _ = self._c.exec_command(script, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        return out, stdout.channel.recv_exit_status()

    def close(self) -> None:
        if self._c is not None:
            self._c.close()


class UdmSsh:
    """Runs one fixed script on the UDM and returns what it printed. One connection per call, closed afterwards, a hard time limit, and the
    UDM's host key is remembered on first use and refused if it ever changes (nothing runs on a host that does not match)."""

    def __init__(self, cfg: SshCfg, host_key_file: str | None = None, client_factory: Callable[[], Any] | None = None):
        self.cfg = cfg
        self.host_key_file = host_key_file
        self._factory = client_factory or _ParamikoClient

    def _clean(self, text: str) -> str:
        secret = self.cfg.password.get_secret_value() if self.cfg.password else ""
        return text.replace(secret, "***") if secret else text

    def _check_host_key(self, fingerprint: str) -> None:
        if not self.host_key_file:
            return
        try:
            known = open(self.host_key_file).read().strip()
        except OSError:
            known = ""
        if not known:
            os.makedirs(os.path.dirname(self.host_key_file) or ".", exist_ok=True)
            with open(self.host_key_file, "w") as f:
                f.write(fingerprint + "\n")
            log.info("remembered the UDM's SSH host key")
        elif known != fingerprint:
            raise UdmError("the UDM's SSH host key changed: refusing to run anything (delete the remembered key file if you replaced the UDM)")

    def run(self, script: str) -> str:
        c, client = self.cfg, self._factory()
        done: dict[str, Any] = {}

        def work() -> None:
            try:
                client.connect(c.host, c.port, c.user, c.password.get_secret_value() if c.password else None, c.key_file, c.timeout_seconds)
                self._check_host_key(client.server_key_fingerprint())
                done["out"], done["rc"] = client.exec(script, c.timeout_seconds)
            except BaseException as e:      # noqa: BLE001 - reported below, with the secret removed
                done["err"] = e

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(c.timeout_seconds)
        try:
            client.close()
        except Exception:       # noqa: BLE001
            pass
        if t.is_alive():
            raise UdmError(f"the UDM did not answer within the {c.timeout_seconds:g}s time limit")
        err = done.get("err")
        if err is not None:
            if isinstance(err, UdmError):
                raise UdmError(str(err)) from None
            raise UdmError(self._clean(f"ssh to the UDM failed: {type(err).__name__}: {err}")) from None
        if done.get("rc") != 0:
            raise UdmError(f"the test script on the UDM exited with {done.get('rc')}")
        return done["out"]
