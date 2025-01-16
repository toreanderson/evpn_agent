# evpn_agent - OpenStack EVPN Agent
#
# Copyright (C) 2024-2025  Tore Anderson <tore@redpill-linpro.com>
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

import ipaddress
import logging
import re
from tempfile import NamedTemporaryFile
from textwrap import dedent
from importlib.machinery import SourceFileLoader
from .utils import cmd

log = logging.getLogger(__name__)

frrlib = SourceFileLoader("frrlib", "/usr/libexec/frr/frr-reload.py").load_module()

vtysh = frrlib.Vtysh()

running_config = None
target_config = None


def update():
    global running_config
    global target_config

    running_config = frrlib.Config(vtysh=vtysh)
    running_config.load_from_show_running(daemon=None)

    target_config = frrlib.Config(vtysh=vtysh)
    target_config.load_from_file("/etc/frr/frr.conf")


def finalise():
    (add, delete) = frrlib.compare_context_objects(target_config, running_config)

    # The comparison may produce redundant commands, e.g., if the same resource has been
    # ensured multiple times (for example: many networks may have the same L3VNI, if so
    # the VRF/L3VNI mapping will have been ensured once per network). Run them through a
    # dict to get rid of the duplicates (while maintaining the ordering of the first
    # occurrences, which set() unfortunately won't do for us).

    for ctx, line in dict.fromkeys(delete).keys():
        cmd = frrlib.lines_to_config(ctx, line, delete=True)
        log.warning(f"Configuring FRR: {cmd}")
        vtysh(["configure"] + cmd)
    for ctx, line in dict.fromkeys(add).keys():
        cmd = frrlib.lines_to_config(ctx, line, delete=False)
        log.warning(f"Configuring FRR: {cmd}")
        vtysh(["configure"] + cmd)

    update()


def ensure_vrf(*, vrf, l3vni=None):
    asn = get_asn()

    # See the example FRR config in README.md for an explanation for why the BGP
    # connected route check is disabled

    frrconf = dedent(
        f"""\
        route-map {vrf}-redistribute-connected deny 65535
        exit
        router bgp {asn} vrf {vrf}
            no bgp default ipv4-unicast
            bgp disable-ebgp-connected-route-check
            bgp bestpath as-path multipath-relax
            address-family ipv4 unicast
                redistribute kernel
                redistribute connected route-map {vrf}-redistribute-connected
            exit-address-family
            address-family ipv6 unicast
                redistribute kernel
                redistribute connected route-map {vrf}-redistribute-connected
            exit-address-family
            address-family l2vpn evpn
                advertise ipv4 unicast
                advertise ipv6 unicast
            exit-address-family
        exit
        """
    )

    # Configure L3VNI mapping for VRF, if one is provided
    if l3vni:
        frrconf += dedent(
            f"""\
                vrf {vrf}
                    vni {l3vni}
                exit-vrf
            """
        )

    # Configure leaking of routes to/from underlay if l3vni=0 (as opposed to None)
    if l3vni == 0:
        frrconf += dedent(
            f"""\
            router bgp {asn}
                address-family ipv4 unicast
                    import vrf {vrf}
                exit-address-family
                address-family ipv6 unicast
                    import vrf {vrf}
                exit-address-family
            exit
            router bgp {asn} vrf {vrf}
                address-family ipv4 unicast
                    import vrf default
                exit-address-family
                address-family ipv6 unicast
                    import vrf default
                exit-address-family
            exit
            """
        )

    add_config(frrconf)


def ensure_advertise_connected(*, vrf, vlanid):
    add_config(
        dedent(
            f"""\
            route-map {vrf}-redistribute-connected permit {vlanid}
                match interface irb-{vlanid}
            exit
            """
        )
    )


def ensure_ra(*, dev, prefix, mode):
    log.info(f"Ensuring ICMPv6 RA on {dev} for {prefix} ({mode})")

    frrconf = f"interface {dev}\n"

    # Set RA flags according depending on ipv6_ra_mode according to
    # https://docs.openstack.org/neutron/latest/admin/config-ipv6.html
    #
    # SLAAC mode (A,M,O = 1,0,0) is FRR default behaviour

    if mode == "dhcpv6-stateful":  # A,M,O = 0,1,0
        frrconf += "    ipv6 nd managed-config-flag\n"
        frrconf += f"    ipv6 nd prefix {prefix} no-autoconfig\n"
    elif mode == "dhcpv6-stateless":  # A,M,O = 1,0,1
        frrconf += "    ipv6 nd other-config-flag\n"

    frrconf += "    no ipv6 nd suppress-ra\n"
    frrconf += "exit\n"

    add_config(frrconf)


def ensure_bgp_listener(*, dev, vrf, subnet, route):
    log.info(f"Ensuring dynamic BGP listener on {subnet} @ {dev} for {route} in {vrf}")
    asn = get_asn()
    cidr = ipaddress.ip_network(route["destination"])

    if match := re.match(r"(::|0\.)179[:.](\d+)[:.](\d+)$", route["nexthop"]):
        ge = int(match.group(2))
        le = int(match.group(3))

    if not cidr.prefixlen <= ge <= le:
        log.error(f"ERROR: Invalid ge/le for {route}, ensure mask length <= ge <= le")
        return

    if cidr.version == 4:
        afi = "ipv4"
        pltype = "ip"
    elif cidr.version == 6:
        afi = "ipv6"
        pltype = "ipv6"

    frrconf = dedent(
        f"""\
        {pltype} prefix-list {dev}-accept-bgp permit {cidr} ge {ge} le {le}
        router bgp {asn} vrf {vrf}
            neighbor {dev} peer-group
            neighbor {dev} remote-as external
            bgp listen range {subnet} peer-group {dev}
            no bgp default ipv4-unicast
            address-family {afi} unicast
                neighbor {dev} activate
                neighbor {dev} prefix-list {dev}-accept-bgp in
            exit-address-family
        exit
        """
    )

    add_config(frrconf)


def add_config(frrconf):
    log.debug("Adding to FRR target config:")
    for line in frrconf.splitlines():
        log.debug("> " + line)
    with NamedTemporaryFile(mode="w") as tmp:
        tmp.file.write(frrconf)
        tmp.file.flush()
        target_config.load_from_file(tmp.name)


def get_asn():
    for line in running_config.contexts:
        if match := re.match(r"router bgp (\d+)$", line[0]):
            return match.group(1)


# Ensure the cache is populated during initial import
update()
