"""Home Assistant add-on entrypoint: maps /data/options.json to environment variables, seeds the config, runs."""
from __future__ import annotations

import json
import os
import shutil
import sys
from importlib import resources
from pathlib import Path

OPTIONS = Path(os.environ.get("HA_OPTIONS_FILE", "/data/options.json"))
CONFIG = Path(os.environ.get("WATCHDOG_CONFIG", "/config/config.yaml"))


def main() -> int:
    opts = json.loads(OPTIONS.read_text()) if OPTIONS.exists() else {}
    mapping = {"unifi_url": "UNIFI_URL", "unifi_api_key": "UNIFI_API_KEY", "log_level": "LOG_LEVEL",
               "notify_service": "NOTIFY_SERVICE", "control_token": "WATCHDOG_CONTROL_TOKEN",
               "mqtt_host": "MQTT_HOST", "mqtt_port": "MQTT_PORT", "mqtt_username": "MQTT_USERNAME",
               "mqtt_password": "MQTT_PASSWORD", "udm_ssh_host": "UDM_SSH_HOST", "udm_ssh_user": "UDM_SSH_USER",
               "udm_ssh_password": "UDM_SSH_PASSWORD", "udm_ssh_key_file": "UDM_SSH_KEY_FILE", "udm_on_stop": "UDM_ON_STOP"}
    for key, env in mapping.items():
        if opts.get(key):
            os.environ[env] = str(opts[key])
    key = os.environ.get("UDM_SSH_KEY_FILE")
    if key and not os.path.isabs(key):          # a file name in the add-on's config folder, next to config.yaml
        os.environ["UDM_SSH_KEY_FILE"] = str(CONFIG.parent / key)
    if not os.environ.get("UNIFI_API_KEY"):
        print("Set `unifi_api_key` in the add-on Configuration tab, then start the add-on again.", file=sys.stderr)
        return 1
    if not CONFIG.exists():
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        with resources.as_file(resources.files("vpn_watchdog").joinpath("templates/ha_config.yaml")) as tpl:
            shutil.copy(tpl, CONFIG)
        print(f"created {CONFIG} from the template; open the add-on panel and add a VPN group")
    from .cli import main as cli_main

    return cli_main(["run", "--config", str(CONFIG)])


if __name__ == "__main__":
    raise SystemExit(main())
