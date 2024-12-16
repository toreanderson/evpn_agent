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

import pymysql.cursors
import socket

from .config import conf

dbconn = pymysql.connect(**conf["db"])


def run_query(sql, param=None):
    """Executes an SQL query and returns the result"""
    cur = dbconn.cursor(pymysql.cursors.DictCursor)
    cur.execute(sql, param)
    dbconn.commit()
    return cur.fetchall()


def get_ports():
    """Returns a list of active ports (either normal of floating IPs) on this particular
    compute node"""
    return run_query(
        """SELECT
            networksegments.segmentation_id AS segmentation_id,
            ports.mac_address               AS mac_address,
            ports.device_id                 AS device_id,
            ports.device_owner              AS device_owner,
            ipallocations.ip_address        AS ip_address,
            ipallocations.subnet_id         AS subnet_id
        FROM
            ports LEFT JOIN ipallocations ON ports.id = ipallocations.port_id,
            ml2_port_bindings,
            networks,
            networksegments
        WHERE
            ports.network_id = networks.id
            AND ports.id = ml2_port_bindings.port_id
            AND networks.id = networksegments.network_id
            AND networksegments.network_type = 'vlan'
            AND networksegments.physical_network = %(physnet)s
            AND ports.status = 'ACTIVE'
            AND ml2_port_bindings.host = %(host)s
        UNION
        SELECT
            networksegments.segmentation_id AS segmentation_id,
            ports.mac_address               AS mac_address,
            ports.device_id                 AS device_id,
            ports.device_owner              AS device_owner,
            floatingips.floating_ip_address AS ip_address,
            NULL                            AS subnet_id
        FROM
            floatingips,
            ports,
            ml2_port_bindings,
            networks,
            networksegments
        WHERE
            floatingips.floating_network_id = networks.id
            AND floatingips.fixed_port_id = ml2_port_bindings.port_id
            AND floatingips.floating_port_id = ports.id
            AND networks.id = networksegments.network_id
            AND networksegments.network_type = 'vlan'
            AND networksegments.physical_network = %(physnet)s
            AND ml2_port_bindings.status = 'ACTIVE'
            AND ml2_port_bindings.host = %(host)s""",
        {"host": socket.getfqdn(), "physnet": conf["agent"]["physical_network"]},
    )


def get_networks():
    """Returns a list of networks with active ports (ether normal of floating IPs) on
    this particular compute node"""
    return run_query(
        """SELECT DISTINCT
            networks.id                      AS id,
            evpnnetworks.l2vni               AS l2vni,
            evpnnetworks.l3vni               AS l3vni,
            evpnnetworks.advertise_connected AS advertise_connected,
            networksegments.segmentation_id  AS segmentation_id,
            networks.mtu                     AS mtu
        FROM
            evpnnetworks,
            ports,
            ml2_port_bindings,
            networks,
            networksegments
        WHERE
            evpnnetworks.id = networks.id
            AND networksegments.network_id = networks.id
            AND ports.network_id = networks.id
            AND ports.id = ml2_port_bindings.port_id
            AND networksegments.network_type = 'vlan'
            AND networksegments.physical_network = %(physnet)s
            AND ports.status = 'ACTIVE'
            AND ml2_port_bindings.host = %(host)s
        UNION
        SELECT
            networks.id                      AS id,
            evpnnetworks.l2vni               AS l2vni,
            evpnnetworks.l3vni               AS l3vni,
            evpnnetworks.advertise_connected AS advertise_connected,
            networksegments.segmentation_id  AS segmentation_id,
            networks.mtu                     AS mtu
        FROM
            evpnnetworks,
            floatingips,
            ml2_port_bindings,
            networks,
            networksegments
        WHERE
            evpnnetworks.id = networks.id
            AND floatingips.floating_network_id = networks.id
            AND networksegments.network_id = networks.id
            AND floatingips.fixed_port_id = ml2_port_bindings.port_id
            AND networksegments.network_type = 'vlan'
            AND networksegments.physical_network = %(physnet)s
            AND ml2_port_bindings.status = 'ACTIVE'
            AND ml2_port_bindings.host = %(host)s""",
        {"host": socket.getfqdn(), "physnet": conf["agent"]["physical_network"]},
    )


def get_subnets(*, network):
    """Returns a list of subnets on a given network object"""
    return run_query(
        """SELECT
            subnets.id                   AS id,
            subnets.gateway_ip           AS gateway_ip,
            subnets.cidr                 AS cidr,
            subnets.enable_dhcp          AS enable_dhcp,
            subnets.ipv6_ra_mode         AS ipv6_ra_mode,
            subnetpools.address_scope_id AS address_scope_id
        FROM
            subnets LEFT JOIN subnetpools ON subnets.subnetpool_id = subnetpools.id
        WHERE            
            subnets.network_id = %(network_id)s""",
        {"network_id": network},
    )


def get_subnetroutes(*, subnet_id):
    """Returns a list of static routes on a given subnet object"""
    return run_query(
        """SELECT
            destination,
            nexthop
        FROM
            subnetroutes
        WHERE
            subnetroutes.subnet_id = %(subnet_id)s""",
        {"subnet_id": subnet_id},
    )


def get_tenant_networks(*, device_id, address_scope_id):
    """Return a list of tenant network prefixes behind a given router, where the
    tenant network address scope matches that of the router's external gateway"""
    return run_query(
        """SELECT
            subnets.cidr AS cidr
        FROM
            ipallocations,
            ports,
            subnets,
            subnetpools
        WHERE
            ports.id = ipallocations.port_id
            AND subnets.id = ipallocations.subnet_id
            AND subnetpools.id = subnets.subnetpool_id
            AND ports.device_owner = "network:router_interface"
            AND ports.device_id = %(device_id)s
            AND subnetpools.address_scope_id = %(address_scope_id)s""",
        {"device_id": device_id, "address_scope_id": address_scope_id},
    )
