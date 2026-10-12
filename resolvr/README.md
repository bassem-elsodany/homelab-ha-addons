# Resolvr

[![CI](https://github.com/bassem-elsodany/homelab-ha-addons/actions/workflows/ci.yml/badge.svg)](https://github.com/bassem-elsodany/homelab-ha-addons/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](../LICENSE)

A monitoring dashboard for [Technitium DNS Server](https://technitium.com/dns/) in a Home Assistant sidebar panel: live query traffic, clients, query logs, cache, zones, DHCP, and a block-list status page with an Update now button. It uses your Home Assistant sign-in, so there is no separate login.

![Resolvr overview](https://raw.githubusercontent.com/bassem-elsodany/homelab-ha-addons/main/resolvr/docs/screenshots/overview.png)

This add-on runs the published [Resolvr](https://github.com/bassem-elsodany/resolvr-dns-dashboard) image: the dashboard itself lives in its own repository, with screenshots and the full feature list. This folder holds only the Home Assistant packaging.

## Screenshots

All screenshots use invented data (made-up devices, networks and addresses). The two cluster screenshots show a Technitium cluster; with a standalone server those views are not shown.

| Clients | Blocked Zones |
|---|---|
| ![Clients](https://raw.githubusercontent.com/bassem-elsodany/homelab-ha-addons/main/resolvr/docs/screenshots/clients.png) | ![Blocked Zones](https://raw.githubusercontent.com/bassem-elsodany/homelab-ha-addons/main/resolvr/docs/screenshots/blocked-zones.png) |
| **Compare nodes** (Technitium cluster) | **Zones** (Technitium cluster) |
| ![Compare nodes](https://raw.githubusercontent.com/bassem-elsodany/homelab-ha-addons/main/resolvr/docs/screenshots/overview-compare-nodes.png) | ![Zones on a cluster](https://raw.githubusercontent.com/bassem-elsodany/homelab-ha-addons/main/resolvr/docs/screenshots/zones.png) |

## Install
1. Add this repository to Home Assistant (see the [catalogue](../README.md)).
2. Install **Resolvr**, open its **Configuration** tab and set `technitium_url` and `technitium_token` (see the [add-on documentation](DOCS.md)).
3. Start it and open **Resolvr** in the sidebar.

## License

[MIT](../LICENSE)
