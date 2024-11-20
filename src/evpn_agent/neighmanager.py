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
from .config import conf
from .utils import cmd, jsoncmd

log = logging.getLogger(__name__)

state = dict()
known_neighs = []


def update():
    global state
    state = jsoncmd(
        [
            "ip",
            "-j",
            "-d",
            "neigh",
            "show",
            "nud",
            "permanent",
            "proto",
            conf["agent"]["rt_proto"],
        ]
    )


def finalise():
    global known_neighs

    prune()
    update()

    known_neighs = []


def ensure_neigh(*, dst, dev, lladdr):
    global known_neighs

    log.info(f"Ensuring neigh entry {dst}→{lladdr} on {dev}")
    neigh = {
        "dst": dst,
        "dev": dev,
        "lladdr": lladdr,
        "state": ["PERMANENT"],
        "protocol": conf["agent"]["rt_proto"],
    }
    known_neighs.append(neigh)

    if neigh in state:
        log.info("…already present, not needed")
        return

    log.info(state)
    log.warning(f"Adding static neigh entry {dst}→{lladdr} on {dev}")
    cmd(
        [
            "ip",
            "neigh",
            "replace",
            dst,
            "dev",
            dev,
            "lladdr",
            lladdr,
            "nud",
            "permanent",
            "proto",
            conf["agent"]["rt_proto"],
        ]
    )


def prune():
    for neigh in state:
        if not neigh["dev"].startswith("irb-"):
            continue
        if neigh not in known_neighs:
            log.warning(f"Removing orphan neigh entry {neigh}")
            cmd(
                [
                    "ip",
                    "neigh",
                    "del",
                    neigh["dst"],
                    "dev",
                    neigh["dev"],
                    "lladdr",
                    neigh["lladdr"],
                    "proto",
                    conf["agent"]["rt_proto"],
                ]
            )


# Ensure the cache is populated during initial import
update()
