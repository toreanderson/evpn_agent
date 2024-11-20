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

state = dict()
known_addresses = []


def update():
    global state
    state = jsoncmd(["ip", "-j", "-d", "address", "show"])


def finalise():
    global known_addresses

    prune()
    update()

    known_addresses = []


def get_primary_loopback_ipv4():
    lo = [dev for dev in state if dev["ifname"] == "lo"][0]
    for ai in lo["addr_info"]:
        if ai["family"] == "inet" and ai["scope"] == "global":
            return ai["local"]


def ensure_address(*, dev, address):
    log.info(f"Ensuring IP address {address} on {dev}")
    known_addresses.append({"dev": dev, "address": address})

    # Check if address is already present
    for device in state:
        if device["ifname"] != dev:
            continue
        for ai in device["addr_info"]:
            if ai["local"] == address.split("/")[0] and ai["prefixlen"] == int(
                address.split("/")[1]
            ):
                log.debug(f"…already present, nothing to do")
                return

    log.warning(f"Adding address {address} to {dev}")
    cmd(
        ["ip", "address", "add", "dev", dev, address]
        + (["nodad"] if ":" in address else [])
    )


def prune():
    for device in state:
        dev = device["ifname"]
        if not dev.startswith("irb-"):
            continue
        for ai in device["addr_info"]:
            # Leave IPv6 link-locals alone
            if ai["family"] == "inet6" and ai["scope"] == "link":
                continue
            address = ai["local"] + "/" + str(ai["prefixlen"])
            if {"dev": dev, "address": address} not in known_addresses:
                log.warning(f"Removing orphan address {address} from {dev}")
                cmd(["ip", "address", "del", "dev", dev, address])


# Ensure the cache is populated during initial import
update()
