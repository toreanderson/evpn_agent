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

from ipaddress import ip_address, ip_network
import logging
from typing import NamedTuple
from .config import conf
from .utils import cmd, jsoncmd

log = logging.getLogger(__name__)

state = []
known_routes = []
prefix_routes = []
known_prefix_routes = []


class Route(NamedTuple):
    dst: str
    gateway: str = None
    dev: str = None
    prefsrc: str = None
    # Default Linux kernel values at the time of writing
    type: str = "unicast"
    metric: int = 1024
    table: str = "main"


def update():
    global state
    global prefix_routes

    # The prefix routes automatically created by the kernel for on-link IPv4 subnets
    prefix_routes = [
        (rt["dst"], rt.get("dev"), rt.get("prefsrc"))
        for rt in jsoncmd(
            ["ip", "-4", "-j", "route", "show", "proto", "kernel", "table", "main"]
        )
    ]

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
                    prefsrc=rt.get("prefsrc"),
                    type=rt.get("type"),
                    metric=rt.get("metric"),
                    table=str(rt.get("table")),
                )
            )


def finalise():
    global known_routes
    global known_prefix_routes

    prune()
    update()

    known_routes = []
    known_prefix_routes = []


def ensure_route(route: Route):
    global known_routes

    log.info(f"Ensuring {route}")
    known_routes.append(route)

    if route in state:
        log.info("…already present in RIB, addition needed")
        return

    # Use replace rather than add, so that a pre-existing route that differs only in
    # attributes that are not part of the kernel's route key (e.g., prefsrc) is updated
    log.warning(f"Adding {route}")
    cmd(
        ["ip", "route", "replace"]
        + ([route.type] if route.type else [])
        + [route.dst]
        + (["via", route.gateway] if route.gateway else [])
        + (["dev", route.dev] if route.dev else [])
        + (["src", route.prefsrc] if route.prefsrc else [])
        + (["metric", str(route.metric)] if route.metric else [])
        + (["table", str(route.table)] if route.table else [])
        + ["proto", conf["agent"]["rt_proto"]]
    )


def ensure_prefix_route_prefsrc(*, dst, dev, prefsrc):
    # When an IPv4 address is added to an interface, the kernel automatically creates
    # a prefix route for the on-link subnet, using the address itself as the preferred
    # source address. Change it in place, retaining proto kernel, so that FRR keeps
    # treating the subnet as a connected route. The kernel will restore the prefix
    # route to its default state if the link goes down and up again, but it will not
    # remove it along with the address (as the preferred source address no longer
    # matches), so prune() takes care of that.
    log.info(f"Ensuring preferred source address {prefsrc} on prefix route {dst}")
    known_prefix_routes.append((dst, dev))

    if (dst, dev, prefsrc) in prefix_routes:
        log.info("…already present in RIB, nothing to do")
        return

    log.warning(f"Setting preferred source address {prefsrc} on prefix route {dst}")
    cmd(
        ["ip", "route", "replace", dst, "dev", dev, "proto", "kernel"]
        + ["scope", "link", "src", prefsrc, "table", "main"]
    )


def prune():
    # Refresh the state first, as routes may have been removed by the kernel since it
    # was last read (e.g., when an IRB is moved out of a VRF, the kernel flushes all
    # routes pointing to it), and trying to remove those would fail.
    update()
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

    # Remove prefix routes modified by ensure_prefix_route_prefsrc() that are no longer
    # needed (e.g., because the address has been removed). These are recognised by the
    # preferred source address being outside of the prefix, which is never the case for
    # the prefix routes created by the kernel.
    for dst, dev, prefsrc in prefix_routes:
        if not dev or not dev.startswith("irb-") or not prefsrc or dst == "default":
            continue
        if ip_address(prefsrc) in ip_network(dst, strict=False):
            continue
        if (dst, dev) not in known_prefix_routes:
            log.warning(f"Removing orphan prefix route {dst} dev {dev} src {prefsrc}")
            cmd(
                ["ip", "route", "del", dst, "dev", dev, "proto", "kernel"]
                + ["table", "main"]
            )


# Ensure the cache is populated during initial import
update()
