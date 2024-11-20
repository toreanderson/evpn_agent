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
import re
from .utils import cmd

log = logging.getLogger(__name__)

running_config = []
known_vrfs = {}
known_ra_devs = []


def update():
    global running_config

    running_config = []
    proc = cmd(["vtysh", "-c", "show running-config"], capture_output=True, text=True)
    running_config = proc.stdout.splitlines()


def finalise():
    global known_vrfs
    global known_ra_devs

    prune()
    update()

    known_vrfs = {}
    known_ra_devs = []


def ensure_vrf(*, vrf, l3vni=None):
    global known_vrfs

    log.info(f"Ensuring VRF {vrf} with l3vni {l3vni}")

    # FIXME: this should probably have tried to check if everything is already
    # correctly in the running (in "running_config") and only sent the updated config to
    # vtysh if it is not. leave that for later, because FRR is such a massive pain in
    # the arse to configure due to its transactional way of configuration.

    known_vrfs[vrf] = l3vni

    asn = get_asn()
    vnimap = f"vni {l3vni}" if l3vni else "!"
    leak = "" if l3vni == 0 else "no "

    frrconf = f"""
        configure
        vrf {vrf}
            {vnimap}
        exit-vrf
        router bgp {asn}
            address-family ipv4 unicast
                {leak}import vrf {vrf}
            exit-address-family
            address-family ipv6 unicast
                {leak}import vrf {vrf}
            exit-address-family
        exit
        router bgp {asn} vrf {vrf}
            bgp bestpath as-path multipath-relax
            address-family ipv4 unicast
                redistribute kernel
                redistribute connected
                {leak}import vrf default
            exit-address-family
            address-family ipv6 unicast
                redistribute kernel
                redistribute connected
                {leak}import vrf default
            exit-address-family
            address-family l2vpn evpn
                advertise ipv4 unicast
                advertise ipv6 unicast
            exit-address-family
        exit
    """

    log.debug(f"Pushing the following to vtysh: {frrconf}")
    proc = cmd(["vtysh"], input=frrconf, text=True, capture_output=True)
    log.debug(f"vtysh stdout: {proc.stdout}")
    log.debug(f"vtysh stderr: {proc.stderr}")


def ensure_ra(*, dev, prefix, mode):
    global known_ra_devs

    # FIXME: this should probably have tried to check if everything is already
    # correctly in the running (in "running_config") and only sent the updated config to
    # vtysh if it is not. leave that for later, because FRR is such a massive pain in
    # the arse to configure due to its transactional way of configuration.

    log.info(f"Ensuring ICMPv6 RA on {dev} for {prefix} ({mode})")
    known_ra_devs.append(dev)

    # Set RA flags according depending on ipv6_ra_mode according to
    # https://docs.openstack.org/neutron/latest/admin/config-ipv6.html
    if mode == "slaac":
        aflag = True
        mflag = False
        oflag = False
    elif mode == "dhcpv6-stateful":
        aflag = False
        mflag = True
        oflag = False
    elif mode == "dhcpv6-stateless":
        aflag = True
        mflag = False
        oflag = True

    frrconf = f"""
        configure
        interface {dev}
            {"" if mflag else "!"}ipv6 nd managed-config-flag
            {"" if oflag else "!"}ipv6 nd other-config-flag
            {"!" if aflag else ""}ipv6 nd prefix {prefix} no-autoconfig
            no ipv6 nd suppress-ra
        exit
    """

    log.debug(f"Pushing the following to vtysh: {frrconf}")
    proc = cmd(["vtysh"], input=frrconf, text=True, capture_output=True)
    log.debug(f"vtysh stdout: {proc.stdout}")
    log.debug(f"vtysh stderr: {proc.stderr}")


def get_asn():
    for line in running_config:
        if match := re.search(r"^router bgp (\d+)$", line):
            return match.group(1)


def prune():
    frrconf = ""

    for line in running_config:
        if match := re.search(r"^interface (irb-\S+)$", line):
            if not match.group(1) in known_ra_devs:
                log.warning(f"Removing orphaned RA device {match.group(1)}")
                frrconf = frrconf + f"no {match.string}\n"
        if match := re.search(r"^vrf (vrf-\S+)$", line):
            if not match.group(1) in known_vrfs:
                log.warning(f"Removing orphaned VRF {match.group(1)}")
                frrconf = frrconf + f"no {match.string}\n"
        if match := re.search(r"^router bgp \d+ vrf (vrf-\S+)$", line):
            if not match.group(1) in known_vrfs:
                log.warning(f"Removing orphaned BGP instance for VRF {match.group(1)}")
                frrconf = frrconf + f"no {match.string}\n"

    if not frrconf:
        return

    frrconf = "configure\n" + frrconf
    log.debug(f"Pushing the following to vtysh: {frrconf}")
    proc = cmd(["vtysh"], input=frrconf, text=True, capture_output=True)
    log.debug(f"vtysh stdout: {proc.stdout}")
    log.debug(f"vtysh stderr: {proc.stderr}")


# Ensure the cache is populated during initial import
update()
