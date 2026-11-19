"""The tunnel test that runs on the UDM itself (UniFi's API cannot send a ping or a request through a VPN client).

Everything the watchdog asks the UDM to run is the fixed, read-only text built by `build_script`: no UniFi or user text is ever put into it,
and the only value formatted in is the load limit (a validated number). The output is parsed into a `UdmReading`.
"""
from __future__ import annotations

import math
import re

from .models import TunnelProbe, UdmReading

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
