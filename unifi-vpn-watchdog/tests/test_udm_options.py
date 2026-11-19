"""The add-on options for the UDM test: in the manifest, passed on by the entrypoint, and applied over any config file."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from vpn_watchdog.config import parse_config

MANIFEST = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text())
BASE = "unifi: {api_key: x}\ngroups: [{name: g1, order: [a]}]\n"


def test_the_manifest_has_the_udm_options_with_safe_defaults():
    o, s = MANIFEST["options"], MANIFEST["schema"]
    assert o["udm_ssh_host"] == "" and o["udm_ssh_user"] == "" and o["udm_ssh_password"] == ""     # off until filled in
    assert o["udm_ssh_key_file"] == "udm_ssh_key" and o["udm_on_stop"] == "keep"
    assert s["udm_ssh_password"] == "password?" and s["udm_on_stop"] == "list(keep|direct)"
    assert {"udm_ssh_host", "udm_ssh_user", "udm_ssh_key_file"} <= set(s)


def test_environment_fills_probe_ssh_even_when_the_config_file_has_no_such_section():
    cfg = parse_config(BASE, env={"UDM_SSH_HOST": "udm.example", "UDM_SSH_USER": "watchdog", "UDM_SSH_KEY_FILE": "/config/udm_ssh_key"})
    assert cfg.probe.ssh.configured and cfg.probe.ssh.host == "udm.example" and cfg.probe.ssh.key_file == "/config/udm_ssh_key"


def test_nothing_or_empty_settings_leave_the_check_off():
    assert not parse_config(BASE, env={}).probe.ssh.configured
    assert not parse_config(BASE, env={"UDM_SSH_HOST": "", "UDM_SSH_USER": "", "UDM_SSH_PASSWORD": "", "UDM_SSH_KEY_FILE": ""}).probe.ssh.configured
    assert not parse_config(BASE, env={"UDM_SSH_HOST": "udm.example", "UDM_SSH_USER": "watchdog"}).probe.ssh.configured     # no credential


def test_a_password_from_the_environment_is_kept_secret():
    cfg = parse_config(BASE, env={"UDM_SSH_HOST": "h", "UDM_SSH_USER": "u", "UDM_SSH_PASSWORD": "TOP-SECRET-PW"})
    assert cfg.probe.ssh.configured and "TOP-SECRET-PW" not in repr(cfg.probe) and "TOP-SECRET-PW" not in cfg.model_dump_json()


def test_on_stop_comes_from_the_environment_and_a_bad_value_is_refused_without_echoing_it():
    assert parse_config(BASE, env={"UDM_ON_STOP": "direct"}).probe.on_stop == "direct"
    assert parse_config(BASE, env={}).probe.on_stop == "keep"
    try:
        parse_config(BASE, env={"UDM_ON_STOP": "explode"})
    except Exception as e:                                   # noqa: BLE001
        assert "explode" not in str(e) and "on_stop" in str(e)
    else:
        raise AssertionError("a bad on_stop was accepted")


def test_the_entrypoint_passes_the_options_on_and_resolves_a_relative_key_file(tmp_path, monkeypatch):
    from vpn_watchdog import ha_entry
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps({"unifi_api_key": "k", "udm_ssh_host": "udm.example", "udm_ssh_user": "watchdog", "udm_ssh_password": "",
                                "udm_ssh_key_file": "udm_ssh_key", "udm_on_stop": "direct"}))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(BASE)
    monkeypatch.setattr(ha_entry, "OPTIONS", opts)
    monkeypatch.setattr(ha_entry, "CONFIG", cfg)
    seen = {}

    def fake_cli(argv):
        import os
        seen.update({k: os.environ.get(k) for k in ("UDM_SSH_HOST", "UDM_SSH_USER", "UDM_SSH_KEY_FILE", "UDM_ON_STOP")})
        return 0

    monkeypatch.setattr("vpn_watchdog.cli.main", fake_cli)
    for k in ("UDM_SSH_HOST", "UDM_SSH_USER", "UDM_SSH_KEY_FILE", "UDM_ON_STOP"):
        monkeypatch.delenv(k, raising=False)
    assert ha_entry.main() == 0
    assert seen == {"UDM_SSH_HOST": "udm.example", "UDM_SSH_USER": "watchdog", "UDM_SSH_KEY_FILE": str(tmp_path / "udm_ssh_key"), "UDM_ON_STOP": "direct"}
