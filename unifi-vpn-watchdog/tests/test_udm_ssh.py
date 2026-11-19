"""The SSH runner: a fixed script goes to the UDM, nothing secret ever comes back out of it."""
from __future__ import annotations

import logging
import time

import pytest

from vpn_watchdog.config import ProbeCfg, SshCfg
from vpn_watchdog.udm import UdmError, UdmSsh

PASSWORD = "SECRET-PW-12345"


class FakeClient:
    """Stands in for the real SSH client: records what it was asked, answers from `out`."""

    def __init__(self, out="#load 1.0\n", fingerprint="AA:BB", connect_error=None, delay=0.0):
        self.out, self.fingerprint, self.connect_error, self.delay = out, fingerprint, connect_error, delay
        self.connected_with, self.ran, self.closed = None, [], 0

    def connect(self, host, port, user, password, key_file, timeout):
        if self.connect_error:
            raise self.connect_error
        self.connected_with = (host, port, user, password, key_file)

    def server_key_fingerprint(self):
        return self.fingerprint

    def exec(self, script, timeout):
        time.sleep(self.delay)
        self.ran.append(script)
        return self.out, 0

    def close(self):
        self.closed += 1


def cfg(**kw):
    base = dict(host="udm.example", user="watchdog", password=PASSWORD, timeout_seconds=15)
    base.update(kw)
    return SshCfg(**base)


def test_configured_needs_host_user_and_a_credential():
    assert cfg().configured
    assert cfg(password=None, key_file="udm_ssh_key").configured
    assert not cfg(password=None).configured
    assert not cfg(host=None).configured
    assert not cfg(user=None).configured
    assert not SshCfg().configured and ProbeCfg().ssh.configured is False


def test_the_script_is_sent_unchanged_and_the_connection_is_closed():
    fake = FakeClient()
    out = UdmSsh(cfg(), client_factory=lambda: fake).run("echo fixed script")
    assert out == "#load 1.0\n" and fake.ran == ["echo fixed script"] and fake.closed == 1
    assert fake.connected_with[:3] == ("udm.example", 22, "watchdog")


def test_the_secret_never_appears_in_logs_errors_or_dumps(caplog):
    caplog.set_level(logging.DEBUG)
    c = cfg()
    assert PASSWORD not in repr(c) and PASSWORD not in c.model_dump_json() and PASSWORD not in str(c.model_dump())
    bad = FakeClient(connect_error=RuntimeError(f"authentication failed for password {PASSWORD}"))
    with pytest.raises(UdmError) as e:
        UdmSsh(c, client_factory=lambda: bad).run("x")
    assert PASSWORD not in str(e.value) and "***" in str(e.value)
    UdmSsh(c, client_factory=lambda: FakeClient()).run("x")
    assert PASSWORD not in caplog.text


def test_a_hanging_command_is_cut_off_after_the_time_limit():
    slow = FakeClient(delay=2.0)
    started = time.time()
    with pytest.raises(UdmError, match="time limit"):
        UdmSsh(cfg(timeout_seconds=0.3), client_factory=lambda: slow).run("x")
    assert time.time() - started < 1.5


def test_first_connection_remembers_the_host_key_and_a_changed_key_is_refused(tmp_path):
    key_file = str(tmp_path / "udm_host_key")
    UdmSsh(cfg(), host_key_file=key_file, client_factory=lambda: FakeClient(fingerprint="AA:BB")).run("x")
    assert open(key_file).read().strip() == "AA:BB"
    UdmSsh(cfg(), host_key_file=key_file, client_factory=lambda: FakeClient(fingerprint="AA:BB")).run("x")        # same key: fine
    changed = FakeClient(fingerprint="CC:DD")
    with pytest.raises(UdmError, match="host key changed"):
        UdmSsh(cfg(), host_key_file=key_file, client_factory=lambda: changed).run("x")
    assert changed.ran == [] and changed.closed == 1                                                              # nothing was run on an unknown host


def test_probe_settings_have_the_agreed_defaults():
    p = ProbeCfg()
    assert p.on_stop == "keep" and p.load_max == 4.0 and p.ssh.port == 22 and p.ssh.timeout_seconds == 15
    assert ProbeCfg(mode="udm").mode == "udm"
