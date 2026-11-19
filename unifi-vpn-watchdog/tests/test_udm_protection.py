"""The watchdog must never keep the UDM busy: UniFi re-provisions the whole network for every routing-policy write
(2026-11-19: Remote-Work was re-created every 15 s for hours and every VLAN got slow, VPN or not)."""
from __future__ import annotations

from test_device_groups import TV, own, setup
from conftest import run
from vpn_watchdog import routing
from vpn_watchdog.routing import Action, key


def route_writes(un):
    return [c for c in un.calls if c[0] in ("route-create", "route-delete", "route-update")]


def place_new_policies_first(un):
    """UniFi decides where a new policy lands in its list (it is NOT always the end): the worst case for a VLAN policy is above everything."""
    real = un.create_own_route

    def create(body):
        real(body)
        if not any(t.get("type") == "CLIENT" for t in body["target_devices"]):
            un.routes.insert(0, un.routes.pop())      # the VLAN copy that was just made goes to the top (above the device policy)

    un.create_own_route = create


def test_a_policy_that_unifi_puts_above_the_device_policy_is_not_re_created_forever(make_engine):
    eng, un, _, notes, clock = make_engine()
    setup(eng, un, devices=(), routing=True)
    run(eng, clock, 3)
    place_new_policies_first(un)
    setup(eng, un, devices=(TV,))                                              # the device policy appears: the VLAN policies are above it
    run(eng, clock, 6)
    settled = len(route_writes(un))
    run(eng, clock, 200)                                                       # 50 minutes
    assert len(route_writes(un)) == settled, "kept writing to UniFi although the copies never landed below the device policy"
    assert len([c for c in un.calls if c[0] == "route-create" and "vlan" in c[1]]) <= 2 * 2 + 2     # at most two tries per VLAN policy
    assert "udm_guard" in notes.kinds()                                    # and it said so, once
    assert notes.kinds().count("udm_guard") == 1


def test_the_attempt_count_resets_when_the_order_is_fine_again(make_engine):
    eng, un, _, notes, clock = make_engine()
    setup(eng, un, devices=(), routing=True)
    run(eng, clock, 3)
    place_new_policies_first(un)
    setup(eng, un, devices=(TV,))
    run(eng, clock, 30)
    assert "udm_guard" in notes.kinds()
    dev = next(r for r in un.routes if r.target_macs)                          # you move the device policy to the top yourself
    un.routes.remove(dev)
    un.routes.insert(0, dev)
    run(eng, clock, 5)
    assert eng._order_tries == {} and eng._order_blocked == set()


def test_a_burst_of_routing_writes_trips_the_budget_and_pauses_the_writes(make_engine):
    """Whatever the bug is, more than ROUTE_WRITES_MAX writes in the window stops all routing writes and raises an alert."""
    eng, un, _, notes, clock = make_engine()
    setup(eng, un, devices=(), routing=True)
    run(eng, clock, 3)
    flip = {"n": 0}

    def flapping_plan(g, snap):                                                # a stand-in for any future bug: a change wanted every cycle
        r = next(r for r in snap.routes if routing.owned_vlan(r))
        flip["n"] += 1
        return [Action("update", r.id, None, f"id-flap-{flip['n']}", r.description, "flap")]

    eng._routing_plan = flapping_plan
    run(eng, clock, 100)                                                       # 25 minutes
    updates = [c for c in un.calls if c[0] == "route-update" and c[2].startswith("id-flap")]
    assert len(updates) <= eng.ROUTE_WRITES_MAX + 1
    assert notes.kinds().count("udm_guard") >= 1
    first_trip = clock.t
    n = len(updates)
    run(eng, clock, 20)                                                        # while paused nothing is written
    assert len([c for c in un.calls if c[0] == "route-update" and c[2].startswith("id-flap")]) == n


def test_normal_operation_stays_far_below_the_budget(make_engine):
    eng, un, _, notes, clock = make_engine()
    setup(eng, un, devices=(TV,), routing=True)
    run(eng, clock, 200)
    assert len(route_writes(un)) <= 10 and "udm_guard" not in notes.kinds()      # 3 creates + 2 moves below the device policy = 7


# ---------------------------------------------------------------- VPN clients: manual stop, toggle budget, dead exits
from conftest import tid                                                       # noqa: E402
from vpn_watchdog.models import FlowStat                                       # noqa: E402


def test_switching_every_vpn_client_off_yourself_pauses_the_group_instead_of_switching_one_back_on(make_engine):
    eng, un, _, notes, clock = make_engine()
    run(eng, clock, 3)
    n = len(un.calls)
    un.enabled.clear()                                                         # you stop all VPN clients in UniFi
    run(eng, clock, 40)
    assert not [c for c in un.calls[n:] if c[0] == "enable"], "it switched a client back on after you stopped them"
    assert eng.store.group("g1").paused
    assert "switched off outside the watchdog" in eng.store.group("g1").last_decision


def test_the_first_start_with_nothing_on_still_chooses_a_client(make_engine):
    eng, un, _, _, clock = make_engine(enabled=set())
    run(eng, clock, 4)
    assert un.active_names() == ["Home-Primary"]


def test_vpn_client_switching_is_bounded_when_every_tunnel_is_dead(make_engine):
    eng, un, _, notes, clock = make_engine()
    times = []
    real = un.set_tunnel_enabled

    def toggle(i, en):
        times.append(clock.t)
        real(i, en)

    un.set_tunnel_enabled = toggle
    run(eng, clock, 4)
    un.dead.update(tid(n) for n in ("Home-Primary", "Home-Backup", "Cousin vpn 2", "office/berlin", "office/frankfurt", "zzz-last-resort"))
    run(eng, clock, 400)                                                       # 100 minutes
    worst = max(sum(1 for u in times if t <= u < t + eng.TUNNEL_TOGGLES_WINDOW) for t in times)
    assert worst <= eng.TUNNEL_TOGGLES_MAX, f"{worst} VPN client switches in {eng.TUNNEL_TOGGLES_WINDOW:.0f}s"


def flows(**kw):
    base = dict(flows=0, zero_rx=0, tx=0, rx=0)
    base.update(kw)
    return FlowStat(**base)


def test_a_tunnel_that_sends_but_gets_no_answers_is_failed_over_even_though_unifi_says_connected(make_engine):
    eng, un, _, notes, clock = make_engine(order="[Home-Primary, Home-Backup]")
    run(eng, clock, 3)
    un.flows = {tid("Home-Primary"): flows(flows=312, zero_rx=221, tx=1_387_471, rx=163_272)}       # London 2110 on 2026-11-19
    run(eng, clock, 8)
    assert eng.store.group("g1").current_id == tid("Home-Backup") and "switch" in notes.kinds()
    assert "dead exit" in eng.store.tunnel(tid("Home-Primary")).last_reason


def test_busy_tunnels_with_many_unanswered_flows_but_plenty_of_download_are_left_alone(make_engine):
    eng, un, _, notes, clock = make_engine(order="[Home-Primary, Home-Backup]")
    run(eng, clock, 3)
    un.flows = {tid("Home-Primary"): flows(flows=264, zero_rx=204, tx=144_180_047, rx=458_686_003)}  # Manchester 6201: 77% unanswered, 458 MB down
    run(eng, clock, 20)
    assert un.active_names() == ["Home-Primary"] and "switch" not in notes.kinds()


def test_too_little_evidence_does_not_fail_a_tunnel_over(make_engine):
    eng, un, _, notes, clock = make_engine(order="[Home-Primary, Home-Backup]")
    run(eng, clock, 3)
    un.flows = {tid("Home-Primary"): flows(flows=11, zero_rx=9, tx=112_281, rx=1_000)}
    run(eng, clock, 20)
    assert un.active_names() == ["Home-Primary"]


def test_the_status_shows_how_much_the_watchdog_wrote_to_unifi(make_engine):
    eng, un, _, _, clock = make_engine()
    setup(eng, un, devices=(TV,), routing=True)
    run(eng, clock, 4)
    g = eng.status()["guard"]
    assert g["route_writes"] >= 5 and g["route_writes_max"] == eng.ROUTE_WRITES_MAX and g["tunnel_switches_max"] == eng.TUNNEL_TOGGLES_MAX
    assert g["paused_routing"] is False and g["paused_tunnels"] is False
