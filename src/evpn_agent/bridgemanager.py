# evpn_agent - OpenStack EVPN Agent
#
# Copyright (C) 2024  Tore Anderson <tore@redpill-linpro.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import logging
from .utils import cmd, jsoncmd
from .config import conf
from . import linkmanager as LinkManager

log = logging.getLogger(__name__)

state = dict()
known_fdbs = []
known_vlans = {}


def update():
    global state
    if LinkManager.get_link(conf["bridge"]["veth"]):
        state["fdb"] = jsoncmd(
            ["bridge", "-j", "-d", "fdb", "show", "dev", conf["bridge"]["veth"]]
        )
    else:
        state["fdb"] = {}
    state["link"] = jsoncmd(["bridge", "-j", "-d", "link", "show"])
    state["vlan"] = jsoncmd(["bridge", "-j", "-d", "vlan", "show"])


def finalise():
    global known_fdbs
    global known_vlans
    global state

    prune()
    update()
    known_fdbs = []
    known_vlans = {}


def ensure_fdb(*, lladdr, vid):
    global known_fdbs

    known_fdbs.append({"mac": lladdr, "vlan": vid})

    log.info(f"Ensuring FDB entry for {lladdr} on VLAN {vid}")
    for entry in state["fdb"]:
        if (
            entry["mac"] == lladdr
            and entry["vlan"] == vid
            and (
                entry["flags"] == ["sticky"]
                # If the FDB was previously learned from a remote VTEP and installed
                # by FRR, it'll have the extern_learn flag, which will stay there if
                # we take over management of it. However there does no appear to be a
                # way of creating a FDB entry from scratch with both flags in one go,
                # nor a way of clearing the extern_learn flag with 'bridge fdb replace',
                # so just accept both cases for now, even though it would be more
                # appropriate to ensure the extern_learn flag is either always or never
                # present on the fdb entries managed by the agent. 
                or entry["flags"] == ["extern_learn", "sticky"]
            )
            and entry["master"] == conf["bridge"]["name"]
            and entry["state"] == "static"
        ):
            log.debug(f"…already present: {entry}")
            return
    log.warning(f"Adding static sticky FDB entry for {lladdr} on VLAN {vid}")
    cmd(
        [
            "bridge",
            "fdb",
            "replace",
            lladdr,
            "dev",
            conf["bridge"]["veth"],
            "master",
            "vlan",
            str(vid),
            "static",
            "sticky",
        ]
    )


def ensure_vlan(*, dev, vid, tagged=True):
    global known_vlans

    known_vlans.setdefault(dev, [])
    known_vlans[dev].append(vid)

    log.info(f"Ensuring bridge VLAN {vid} is present on {dev} {tagged=}")
    cur_vlans = [port["vlans"] for port in state["vlan"] if port["ifname"] == dev]
    if not cur_vlans or not [vlan for vlan in cur_vlans[0] if vlan["vlan"] == vid]:
        log.warning(f"Adding VLAN {vid} to device {dev} ({tagged=})")
        cmd(
            ["bridge", "vlan", "add", "dev", dev, "vid", str(vid)]
            + (["pvid", "untagged"] if not tagged else [])
            + (["self"] if dev == conf["bridge"]["name"] else [])
        )


def prune():
    # It is necessary to remove FDBs before removing the VLANs, otherwise the FDB entries
    # end up in a state where they cannot be removed, with the kernel complaining
    # 'bridge: RTM_DELNEIGH with unconfigured vlan 1234 on veth-to-ovs'
    for fdb in state["fdb"]:
        if fdb["state"] != "static":
            continue
        if {"mac": fdb["mac"], "vlan": fdb["vlan"]} in known_fdbs:
            continue
        log.warning(f"Removing orphaned FDB entry {fdb}")
        cmd(
            [
                "bridge",
                "fdb",
                "del",
                fdb["mac"],
                "dev",
                conf["bridge"]["veth"],
                "master",
                "vlan",
                str(fdb["vlan"]),
            ]
        )

    for dev in state["vlan"]:
        # Only consider devices that either are the EVPN bridge itself, or have the EVPN
        # bridge as their master. Otherwise we'll end up trying to remove the default
        # VLAN from the untagged IRB bridge device associated with L3VNIs
        if dev["ifname"] != conf["bridge"]["name"] and not [
            x
            for x in state["link"]
            if x["ifname"] == dev["ifname"] and x["master"] == conf["bridge"]["name"]
        ]:
            log.debug(f"Ignoring VLANs on {dev['ifname']}, not part of EVPN bridge")
            continue
        for vlan in dev["vlans"]:
            ifname = dev["ifname"]
            vlan = vlan["vlan"]
            if not vlan in known_vlans.get(ifname, []):
                log.warning(f"Removing orphaned VLAN {vlan} from {ifname}")
                cmd(
                    ["bridge", "vlan", "del", "dev", ifname, "vid", str(vlan)]
                    + (["self"] if ifname == conf["bridge"]["name"] else [])
                )


# Ensure the cache is populated during initial import
update()
