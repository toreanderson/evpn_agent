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

log = logging.getLogger(__name__)

state = None
known_links = []


def update():
    global state
    state = jsoncmd(["ip", "-j", "-d", "link", "show"])


def finalise():
    global known_links
    prune()
    update()
    known_links = []


def list_links():
    return [link["ifname"] for link in state]


def get_link(name):
    try:
        return [link for link in state if link["ifname"] == name][0]
    except IndexError:
        return None


def ensure_link(
    *, name, type, link=None, link_attrs={}, type_attrs={}, bridge_slave_attrs={}
):
    global state
    known_links.append(name)

    # Create the device if it does not already exist
    if not get_link(name):
        log.warning(f"Creating link {name}")
        cmdline = ["ip", "link", "add", "name", name]
        if type != "veth" and link:
            cmdline.extend(["link", link])
        for k, v in link_attrs.items():
            # addrgenmode can not be set at creation time
            if k == "inet6_addr_gen_mode":
                continue
            cmdline.extend(_link_attr_to_cmd(k, v))
        cmdline.extend(["type", type])
        if type == "veth" and link:
            cmdline.extend(["peer", "name", link])
        for k, v in type_attrs.items():
            cmdline.extend(_type_attr_to_cmd(k, v))
        cmd(cmdline)
        update()

    log.info(f"Syncing all attributes for {name}")
    link = get_link(name)
    if not link["linkinfo"]["info_kind"] == type:
        log.error(
            f'{name} has the wrong type {link["linkinfo"]["info_kind"]}, should have been {type}'
        )

    for k, v in link_attrs.items():
        cur = link.get(k)
        if cur != v:
            log.warning(f"Updating link attribute {k} on {name}: {cur} → {v}")
            cmd(["ip", "link", "set", name] + _link_attr_to_cmd(k, v))

    for k, v in type_attrs.items():
        cur = link["linkinfo"]["info_data"].get(k)
        if cur != v:
            log.warning(f"Updating type attribute {k} on {name}: {cur} → {v}")
            cmd(["ip", "link", "set", name, "type", type] + _type_attr_to_cmd(k, v))

    # Bridge slave attributes cannot be set at creation time, so always sync those
    for k, v in bridge_slave_attrs.items():
        cur = None
        if link:
            cur = link["linkinfo"].get("info_slave_data", {}).get(k)
        if cur != v:
            log.warning(f"Updating bridge slave attribute {k} on {name}: {cur} → {v}")
            cmd(
                ["ip", "link", "set", name, "type", "bridge_slave"]
                + _bridge_slave_attr_to_cmd(k, v)
            )

    # Finally, set the link UP if necessary
    if not link or "UP" not in link["flags"]:
        log.warning(f"Setting {name} UP")
        cmd(["ip", "link", "set", name, "up"])


def prune():
    for link in list_links():
        if link not in known_links:
            if (
                link.startswith("irb-")
                or link.startswith("l2vni-")
                or link.startswith("l3vni-")
                or link.startswith("vrf-")
            ):
                log.warning(f"Removing orphaned link {link}")
                cmd(["ip", "link", "del", link])


def _link_attr_to_cmd(attr, val):
    if attr == "inet6_addr_gen_mode":
        attr = "addrgenmode"
    if attr == "ifalias":
        attr = "alias"
    return [attr, str(val)]


def _type_attr_to_cmd(attr, val):
    if attr == "learning" and val == False:
        return ["nolearning"]
    if attr == "learning" and val == True:
        return ["learning"]
    if attr == "port":
        return ["dstport", str(val)]
    return [attr, str(val)]


def _bridge_slave_attr_to_cmd(attr, val):
    if attr in ["learning", "neigh_suppress"] and val == True:
        return [attr, "on"]
    if attr in ["learning", "neigh_suppress"] and val == False:
        return [attr, "off"]
    return [attr, str(val)]


# Ensure the cache is populated during initial import
update()
