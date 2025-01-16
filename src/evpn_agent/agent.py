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

from ipaddress import ip_address, ip_network
import logging
import sys
import time

from .config import conf

log = logging.getLogger(__name__)
logfmt = "[%(filename)s:%(lineno)s → %(funcName)s()] %(message)s"
logging.basicConfig(
    format=logfmt, level=conf["agent"]["loglevel"].upper(), stream=sys.stdout
)

from . import addressmanager as AddressManager
from . import bridgemanager as BridgeManager
from . import inventory as Inventory
from . import linkmanager as LinkManager
from . import neighmanager as NeighManager
from . import ovsmanager as OvsManager
from . import routemanager as RouteManager
from . import frrmanager as FrrManager


# Main program loop. The basic work flow of the agent is to determine all the resources
# that should be active on this particular hypervisor, use the ensure_foo() functions in
# the various manager modules to ensure they are present, then finally garbage collect
# any resource that were created previously but should no longer be active on this
# hypervisor (e.g., a port belonging to a VM that has been deleted or migrated to
# to another hypervisor.)
while True:
    # Ensure the main EVPN bridge exist and that it is connected to the OVS bridge via a
    # veth pair.
    log.info("Main loop: ensuring EVPN bridge and OVS downlink")
    LinkManager.ensure_link(
        name=conf["bridge"]["name"],
        type="bridge",
        link_attrs={
            "address": conf["bridge"]["address"],
            "inet6_addr_gen_mode": "none",
            "mtu": int(conf["bridge"]["mtu"]),
        },
        type_attrs={
            "vlan_default_pvid": 0,
            "vlan_filtering": 1,
        },
    )
    LinkManager.ensure_link(
        name=conf["bridge"]["veth"],
        link=conf["ovs"]["veth"],
        type="veth",
        link_attrs={
            "master": conf["bridge"]["name"],
            "inet6_addr_gen_mode": "none",
            "mtu": int(conf["bridge"]["mtu"]),
        },
    )
    LinkManager.ensure_link(
        name=conf["ovs"]["veth"],
        link=conf["bridge"]["veth"],
        type="veth",
        link_attrs={
            "inet6_addr_gen_mode": "none",
            "mtu": int(conf["bridge"]["mtu"]),
        },
    )
    OvsManager.ensure_veth()

    # Load the ports once per loop, not once per network, saving us some db queries
    ports = Inventory.get_ports()

    # Loop through each network active on this hypervisor and ensure all of its
    # resources are properly provisioned.
    log.info("Main loop: evaluationg active networks")
    for net in Inventory.get_networks():
        log.info(f"Processing network: {net}")

        vid = net["segmentation_id"]
        mtu = net["mtu"]
        l2vni = net["l2vni"]
        l3vni = net["l3vni"]
        advertise_connected = net["advertise_connected"]

        # If the network has a L3VNI assigned, associate it to a VRF that is shared
        # between all networks using that L3VNI. Otherwise, associate it to an isolated
        # VLAN specific VRF (mostly useful in order to leak routes into the underlay)
        vrf_id = l3vni if l3vni else vid
        rt_table = vrf_id + int(conf["agent"]["rt_table_offset"])

        # Ensure that the VLAN is added to the veth port (connected to the OVS bridge)
        BridgeManager.ensure_vlan(vid=vid, dev=conf["bridge"]["veth"])

        # The L2VNI might be configured explicitly per-VLAN, or implicitly through the
        # 'l2vni_offset' agent configuration option.
        if l2vni is None and "l2vni_offset" in conf["agent"]:
            l2vni = vid + int(conf["agent"]["l2vni_offset"])

        # If the network has an L2VNI assigned (or implicitly through the use of the
        # 'l2vni_offset' agent option), then create a VXLAN device for that L2VNI, hook
        # it up to the EVPN bridge, and ensure the network's VLAN ID is added to the
        # L2VNI bridge port.
        if l2vni:
            log.info(f"Ensuring L2VNI {l2vni} for {net['id']} (VLAN {vid})")
            devname = "l2vni-" + str(l2vni)
            LinkManager.ensure_link(
                name=devname,
                type="vxlan",
                link_attrs={
                    "master": conf["bridge"]["name"],
                    "inet6_addr_gen_mode": "none",
                    "mtu": mtu,
                    "ifalias": "L2VNI for " + net["id"],
                },
                type_attrs={
                    "id": l2vni,
                    "learning": False,
                    "local": AddressManager.get_primary_loopback_ipv4(),
                    "port": 4789,
                },
                bridge_slave_attrs={
                    "learning": False,
                    "neigh_suppress": True,
                },
            )
            BridgeManager.ensure_vlan(vid=vid, dev=devname, tagged=False)

        # Create an IRB device (also called SVI) for the network, ensure it can send
        # and receive traffic to the network's VLAN tag, and finally add all gateway
        # addresses to it with the correct prefix length. FRR will take care of
        # advertising routes for the link prefixes into the EVPN fabric with BGP thanks
        # to the "redistribute connected" setting.
        log.info(f"Ensuring VRF/IRB/L3VNI for VRF {vrf_id}")
        vrf = "vrf-" + str(vrf_id)
        irb = "irb-" + str(vrf_id)
        LinkManager.ensure_link(
            name=vrf,
            type="vrf",
            link_attrs={
                "ifalias": "VRF " + str(vrf_id),
                "inet6_addr_gen_mode": "none",
            },
            type_attrs={"table": rt_table},
        )

        FrrManager.ensure_vrf(vrf=vrf, l3vni=l3vni)

        # Create an IRB device bound bound to the VRF created above
        log.info(f"Ensuring IRB for {net['id']} (VLAN {vid})")
        dev = "irb-" + str(vid)
        LinkManager.ensure_link(
            name=dev,
            link=conf["bridge"]["name"],
            type="vlan",
            link_attrs={
                "mtu": mtu,
                "ifalias": "IRB for VLAN " + str(vid),
                "master": "vrf-" + str(vrf_id),
            },
            type_attrs={"id": vid},
        )
        BridgeManager.ensure_vlan(vid=vid, dev=conf["bridge"]["name"])

        # If the network has a L3VNI assigned, create it plus an IRB device that can
        # is used to send/receive L3 traffic to/from the VXLAN device.
        if l3vni:
            LinkManager.ensure_link(
                name=irb,
                type="bridge",
                link_attrs={
                    "ifalias": "IRB for VRF " + str(vrf_id),
                    "inet6_addr_gen_mode": "none",
                    "master": vrf,
                    "mtu": int(conf["bridge"]["mtu"]) - 50,
                },
            )
            LinkManager.ensure_link(
                name="l3vni-" + str(l3vni),
                type="vxlan",
                link_attrs={
                    "ifalias": "L3VNI for VRF " + str(vrf_id),
                    "inet6_addr_gen_mode": "none",
                    "master": irb,
                    "mtu": int(conf["bridge"]["mtu"]) - 50,
                },
                type_attrs={
                    "id": l3vni,
                    "learning": False,
                    "local": AddressManager.get_primary_loopback_ipv4(),
                    "port": 4789,
                },
                bridge_slave_attrs={
                    "learning": False,
                    "neigh_suppress": True,
                },
            )

        # When there's a L3VNI, enable Layer-3 IP addressing and routing on the IRB for
        # the provider network.
        #
        # Also do so if the L3VNI is explicitly set to 0 (as opposed to the default
        # NULL). In this case, the routing domain will be isolated on the hypervisor,
        # which is probably only useful if the routes is being leaked to/from another
        # VRF by FRR, such as to the underlay.
        #
        # Don't enable the anycast gateway nor any routes if the L3VNI is NULL, as that
        # is taken to mean the provider network is L2 only, and that the L3 gateway (if
        # any) is located on a device external to OpenStack (behind a remote VTEP).
        if l3vni is not None:
            # If the network is configured for advertisement of its connected prefixes,
            # ensure the redistribute connected route map for the VRF allows that.
            #
            # If this is unset, only known IP addresses associated to OpenStack ports
            # are advertised. This eliminates Internet background radiation (scanning
            # and so on) addressed to unused IP addresses from reaching the IRB and
            # causing pointless ARP/NS queries. However it means that IP addresses not
            # known to OpenStack will not be reachable.
            if advertise_connected:
                FrrManager.ensure_advertise_connected(vrf=vrf, vlanid=vid)

            # Add the default gateway IP for each subnet associated with the network
            # to the IRB device.
            subnets = Inventory.get_subnets(network=net["id"])
            for subnet in subnets:
                log.debug(f"Processing subnet {subnet}")
                gw = subnet["gateway_ip"] + "/" + subnet["cidr"].split("/")[-1]
                AddressManager.ensure_address(dev=dev, address=gw)
                if subnet["enable_dhcp"] and subnet["ipv6_ra_mode"]:
                    FrrManager.ensure_ra(
                        dev=dev, prefix=subnet["cidr"], mode=subnet["ipv6_ra_mode"]
                    )

                # Add any subnet routes (from openstack subnet set --host-route) if the
                # nexthop of the subnet route is local to this hypervisor. It will be
                # advertised upstream by FRR thanks to 'redistribute kernel'.
                for subnetroute in Inventory.get_subnetroutes(subnet_id=subnet["id"]):
                    log.debug(f"Considering subnet route {subnetroute}")

                    # As a special case/hack, if the gateway is set to 0.179.x.y or
                    # ::179:x:y, then instead of creating a regular route, we enable a
                    # dynamic BGP listener that allows VMs on this network to advertise
                    # routes from within the destination prefix to FRR running on the
                    # hypervisor, which in turn will re-advertise those onward to the
                    # data centre fabric as Type-5 EVPN routes (or regular IPvX Unicast
                    # routes if underlay leaking is configured). x and y will be used as
                    # the ge/le values in the FRR prefix list.
                    nh = ip_address(subnetroute["nexthop"])
                    if (nh in ip_network("0.179.0.0/16")) or (
                        nh in ip_network("::179:0:0/96")
                    ):
                        FrrManager.ensure_bgp_listener(
                            dev=dev,
                            vrf=vrf,
                            subnet=subnet["cidr"],
                            route=subnetroute,
                        )
                        continue

                    if not [
                        p
                        for p in ports
                        if p["segmentation_id"] == vid
                        and p["ip_address"] == subnetroute["nexthop"]
                    ]:
                        log.debug("Skipping because the nexthop has no local port")
                        continue
                    RouteManager.ensure_route(
                        RouteManager.Route(
                            dst=subnetroute["destination"],
                            gateway=subnetroute["nexthop"],
                            dev=dev,
                            table=str(rt_table),
                        )
                    )

                # Add any routes to tenant subnets located behind router gateway ports
                # (lrp) ports attached to this subnet, if the inside/outside address
                # scopes match
                if subnet["address_scope_id"]:
                    log.info(
                        "Looking for tenant networks with address scope "
                        + subnet["address_scope_id"]
                    )
                    for port in ports:
                        log.debug(f"Considering {port}")
                        if port.get("subnet_id") != subnet["id"]:
                            log.debug(f"…does not belong to {subnet['id']}, skipping")
                            continue
                        if port.get("device_owner") != "network:router_gateway":
                            log.debug(f"…is not a router gateway, skipping")
                            continue

                        tenantnets = Inventory.get_tenant_networks(
                            device_id=port["device_id"],
                            address_scope_id=subnet["address_scope_id"],
                        )
                        log.info(f"Tenant networks found: {tenantnets}")

                        for tenantnet in tenantnets:
                            RouteManager.ensure_route(
                                RouteManager.Route(
                                    dst=tenantnet["cidr"],
                                    gateway=port["ip_address"],
                                    dev=dev,
                                    table=str(rt_table),
                                )
                            )

        # Configure static FDB and neighbor entries for each of the known ports on the
        # network. This reduces the reliance on flooding and learning, and may help
        # reducing BGP churn (consider rather silent host that would otherwise drop in
        # and out of the FDB and/or the neighbour cache).
        log.info(f"Ensuring static FDB/neigh entries for {net['id']} (VLAN {vid})")
        for port in [p for p in ports if p["segmentation_id"] == vid]:
            log.info(f"Processing port {port}")
            # If the port has multiple IP addresses, we'll ensure the same FDB multiple
            # times here - but ensure_fdb() is idempotent, so whatever.
            BridgeManager.ensure_fdb(
                lladdr=port["mac_address"], vid=port["segmentation_id"]
            )

            if port["ip_address"]:
                log.info("Adding static neighbour entry")
                NeighManager.ensure_neigh(
                    dst=port["ip_address"],
                    lladdr=port["mac_address"],
                    dev="irb-" + str(port["segmentation_id"]),
                )

                # If the IRB is not bound to an L3VNI, the Type-2 MACIP routes for the
                # static neigh entries added above will not be leaked into other VRFs
                # as regular host routes, only the on-link prefix would. Therefore,
                # routing to the IP addresses in question would follow the route to the
                # subnet prefix on the network. Since the subnet prefix will be
                # advertised by all hypervisors where the network is active, this will
                # lead to inefficient routing, as the external routers might send the
                # traffic to a hypervisor where the port is not active, which will in
                # turn have to transmit it onwards to the correct hypervisor via the
                # L2VNI (assuming there is one).
                #
                # Upstream bug report: https://github.com/FRRouting/frr/issues/16161
                #
                # To work around this, and ensure that traffic to known ports is routed
                # directly to the correct hypervisor by external routes, add a static
                # host route for the IP address as well. This host route can then be
                # leaked as a regular unicast route to other VRFs (or the underlay), and
                # be advertised onwards from there, ensuring efficient routing.
                if l3vni == 0:
                    log.info("Adding static host route in underlay")
                    RouteManager.ensure_route(
                        RouteManager.Route(
                            dst=port["ip_address"],
                            dev="irb-" + str(port["segmentation_id"]),
                            table=str(rt_table),
                        )
                    )

    # Prune any orphaned resources (i.e., not ensured previously in the main loop),
    # before proceeding to the next iteration of the main loop. This makes sure that
    # deleted resources are garbage collected.
    log.info("Main loop: garbage collecting orphaned resources")
    FrrManager.finalise()
    NeighManager.finalise()
    RouteManager.finalise()
    AddressManager.finalise()
    BridgeManager.finalise()
    LinkManager.finalise()

    log.info("Main loop: complete")
    if "oneshot" in conf["agent"]:
        break
    time.sleep(int(conf["agent"]["interval"]))
