# UniFi VPN Watchdog

A group is a set of VLANs and/or devices plus an ordered list of your UniFi WireGuard VPN clients. The watchdog switches on the next client
from your fallback order when the one in use stops carrying traffic, can rotate clients on a timer, can keep warm standbys connected, and
(only if you switch it on) keeps its own routing policy for each picked VLAN or device pointed at the active client. VPN clients can have
any names: they are shown exactly as you named them in UniFi and are never interpreted.

## First start
1. **Configuration tab:** set `unifi_api_key` (UniFi > Settings > Integrations > API key) and, if needed, `unifi_url`.
2. Start the add-on. It creates `config.yaml` in the add-on config folder.
3. Open **VPN Watchdog** in the sidebar, go to **Settings > VPN groups** and add a group:
   - tick the VLANs this group is for, and/or add devices (a TV, a camera: a device is picked by its MAC, so use a fixed-address device),
   - add your VPN clients under **Fallback order** and number them 1, 2, 3 ... Until you do, the group only watches,
   - optional: **Warm standbys** (keep the next clients connected so a failover is instant), **Rotation** (move to another client on a
     timer), and **Routing** (the watchdog keeps its own policy per picked VLAN or device on the active client).
4. The **Status** page shows the group's VLANs with their devices, the path through the active VPN client to the exit address, and the
   fallback order. Click a VLAN to open its devices; click a device or a client for details. Settings saves by itself a moment after each change.
5. A VPN client can be in more than one group. A client that another group still uses is never switched off, and a device belongs to one group.

## Home Assistant integration
- **Sidebar panel** (ingress): no extra login, controls are protected by your HA session. On a phone the page switches to an app-like layout.
- **Entities** (MQTT discovery). With the Mosquitto add-on nothing needs configuring. With an external broker (the one HA's MQTT
  integration uses) fill `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password` in the Configuration tab. For the Mosquitto add-on use host
  `core-mosquitto`, port `1883` and a Home Assistant user (create a dedicated one under Settings → People → Users). Entities: active tunnel, fallback step, healthy, last decision,
  "Failover paused" switch and "Force tunnel" select, grouped under a "VPN <group>" device.
- **Notifications:** panel → *Config* tab → *Notifications*: choose from the notify services Home Assistant reports (phones,
  persistent notification, ...), press *Use this*, then *Send test*. The default is a persistent notification.
- **Logs:** the add-on **Log** tab.

## Before you rely on it
The watchdog switches your VPN clients on and off. Unless you switch on **Routing** for a group (Settings > VPN groups > Routing, off by default), it never
touches a routing policy: UniFi's own policies send the VLANs and devices through whichever client is up, so keep a routing policy **switched on** in
UniFi for every VPN client you put in a group. A client whose policy is off would be switched on but carry nothing. The Status page warns when a group's
VLANs do not go through its active client.

With **Routing** on you do not need that: the watchdog keeps one policy of its own per picked VLAN, and one for the group's devices, pointed at the
active client. Those policies are named `vpnwd: <group> › <VLAN or devices>` followed by a note that the add-on made them, so you can recognise them in
UniFi. It never edits or deletes a policy you made. UniFi applies the first live policy in its list, so a policy of yours that covers the same VLAN and
sits above the watchdog's one still wins. The Status page warns about it and offers to switch that policy off, only after you confirm.

## Testing every VPN from the UDM (SSH)

UniFi says "connected" as soon as a tunnel's handshake works, which stays true while the tunnel passes nothing, and its API cannot send a test through a VPN client. With an SSH login to the UDM the add-on tests every VPN that is up, once a minute, and switches away from a dead one by itself.

**Set it up** (nothing changes until you do):
1. Make a key used only for this: `ssh-keygen -t ed25519 -N "" -C "vpn-watchdog" -f udm_ssh_key`.
2. Add the public half (`udm_ssh_key.pub`) to the UDM's SSH keys in UniFi's settings (the menu name depends on your UniFi version).
3. Put the private file `udm_ssh_key` in the add-on's config folder (next to `config.yaml`; in Home Assistant that is the add-on's `addon_config` folder). The key text never goes in an option.
4. In the add-on's Configuration tab set `udm_ssh_host` (the UDM's address), `udm_ssh_user` and leave `udm_ssh_key_file` as `udm_ssh_key`. Restart the add-on. A password in `udm_ssh_password` works too, but a key is safer.

The first connection remembers the UDM's SSH host key; if it ever changes the add-on refuses to run anything until you delete `udm_host_key` from the add-on's data folder.

**What it runs on the UDM:** one fixed, read-only script: the load average, one ping over the normal internet, and for every VPN interface four pings and one HTTPS request sent through it. No text from UniFi or from you is put into it, and nothing on the UDM is changed.

**What it does with the result**
- OK / SLOW (some loss or a slow answer: one alert per 30 minutes, nothing is switched) / DEAD. A test that could not run (SSH down, UDM busy, tunnel not up, an old result) is never a verdict.
- The VPN in use must be DEAD 3 tests in a row. Then the next VPN in your order is tried: one candidate per attempt, tested before it gets any traffic, at least 2 minutes between attempts.
- Limits that protect the UDM: the test is skipped while the UDM load is above 4; SSH failures back off 1, 2, 4 ... 15 minutes; nothing is switched while UniFi re-provisioned in the last minute; nothing is changed when every VPN fails or the plain internet is down (one `check_hold` alert per 30 minutes); at least 10 minutes between switches; at most 3 switches or attempts per hour and 6 per day; plus the write budgets (20 routing-policy writes and 12 VPN-client switches per 10 minutes).
- When a limit is reached the group is paused and one critical `check_stopped` alert says so. Fix the cause, then resume the group (that also clears its attempt history and bans). `udm_on_stop: keep` (default) leaves the group as it is; `direct` switches the group's VPN clients off so its VLANs use the normal internet until you resume.

## Not available yet
Failover on speed or delay (for example: when the download is below a number you choose) is planned. UniFi cannot speed-test a single VPN client, so it
will need a small dedicated test device. Today a group fails over when its client stops carrying traffic.
