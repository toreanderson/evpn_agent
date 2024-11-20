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
from typing import NamedTuple
from .config import conf
from .utils import cmd, jsoncmd

log = logging.getLogger(__name__)

state = []
known_routes = []


class Route(NamedTuple):
    dst: str
    gateway: str = None
    dev: str = None
    # Default Linux kernel values at the time of writing
    type: str = "unicast"
    metric: int = 1024
    table: str = "main"


def update():
    global state

    state = []
    for ipverflag in ("-4", "-6"):
        for rt in jsoncmd(
            [
                "ip",
                ipverflag,
                "-j",
                "-d",
                "route",
                "show",
                "proto",
                conf["agent"]["rt_proto"],
                "table",
                "all",
            ]
        ):
            if rt["dst"] == "default" and ipverflag == "-4":
                rt["dst"] = "0.0.0.0/0"
            elif rt["dst"] == "default" and ipverflag == "-6":
                rt["dst"] = "::/0"

            state.append(
                Route(
                    dst=rt["dst"],
                    gateway=rt.get("gateway"),
                    dev=rt.get("dev"),
                    type=rt.get("type"),
                    metric=rt.get("metric"),
                    table=str(rt.get("table")),
                )
            )


def finalise():
    global known_routes

    prune()
    update()

    known_routes = []


def ensure_route(route: Route):
    global known_routes

    log.info(f"Ensuring {route}")
    known_routes.append(route)

    if route in state:
        log.info("…already present in RIB, addition needed")
        return

    log.warning(f"Adding {route}")
    cmd(
        ["ip", "route", "add"]
        + ([route.type] if route.type else [])
        + [route.dst]
        + (["via", route.gateway] if route.gateway else [])
        + (["dev", route.dev] if route.dev else [])
        + (["metric", str(route.metric)] if route.metric else [])
        + (["table", str(route.table)] if route.table else [])
        + ["proto", conf["agent"]["rt_proto"]]
    )


def prune():
    for route in state:
        if route not in known_routes:
            log.warning(f"Removing orphan {route}")
            cmd(
                [
                    "ip",
                    "route",
                    "del",
                    route.dst,
                    "table",
                    route.table,
                    "proto",
                    conf["agent"]["rt_proto"],
                ]
            )


# Ensure the cache is populated during initial import
update()
