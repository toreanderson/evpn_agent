# OpenStack EVPN Agent

This is an EVPN agent for use on OpenStack compute nodes, making it possible to have
them connected to the physical data centre network with point-to-point L3 BGP uplinks,
yet still support the use of VLAN-based provider network from within OpenStack.

This obviates the need for transporting VLANs in the physical data centre infrastructure
and trunking them to the hypervisors, allowing for a pure L3 data centre network fabric.

Connectivity between VMs on different hypervisors connected to the same provider network
are handled by EVPN-signaled L2VNIs. Routing between subnets connected on different
hypervisors are done via EVPN-signaled L3VNIs bound to specific VRFs (symmetric IRB).

It tries hard to do everything in a standards-compliant way, so that it can interoperate
fine with non-OpenStack network devices. This allows for connecting physical devices to
hardware VTEPs and hooking them up to OpenStack provider networks via the L2VNI, as well
as exchanging L3 routing with external routers via the VRF-bound L3VNIs, allowing for
connectivity to the Internet at large (or private IPVPN clouds for that matter).

It is not meant as a competitor to
[ovn-bgp-agent](https://github.com/openstack/ovn-bgp-agent), but rather as a proof of
concept and inspiration in the hope that it too will support EVPN L2VNIs and symmetric
IRB in the future.

# Supported features

* Provisioning of L2VNIs for VLAN-based provider networks
* Interconnection of OVS br-ex to EVPN bridge via a veth device pair
* L2VNI MTU set according to network object in OpenStack database
* Static assignment of bridge FDB entries for MAC addresses known to OpenStack
* Dynamic learning of MAC addresses not known to OpenStack
* Advertisement of MAC addresses via EVPN Type-2 MACIP routes (without IP)
* Supports both explicit (per-network) and automatic L2VNI assignment (the latter
  derived from the VLAN ID)
* Provisioning of VRFs and L3VNIs (symmetric IRB)
* Configuration of per-network IRB configured with an anycast gateway address/subnet (as
  specified in the subnet object in the OpenStack database)
* Advertisement of subnet prefixes on the provider networks as EVPN Type-5 Prefix routes
* Advertisement of static routes configured on subnet objects as EVPN Type-5 Prefix
  routes
* Advertisement of static routes to tenant networks behind routers as EVPN Type-5 Prefix
  routes (when address scopes match)
* Static assignment of neighbour entries (ARP, ND) for IP addresses known to OpenStack 
* Dynamic learning of neighbour entries for IP addresse not known to OpenStack
* Advertisement of neighbour entries as EVPN Type-2 MACIP routes (with IP)
* L3 routing between provider networks and the underlay/default VRF (by leaking
  IPv4/IPv6 unicast routes)
* Per-network EVPN configuration in custom database table
* Dynamically provisions resources only if they are needed on the compute node
* Automatic removal of resources that are no longer needed on the compute node
* Self-contained - no changes needed to OpenStack, OVS or OVN (except a new database
  table)
* Safe to restart - will not tear down any configured resources when it shuts down or
  crashes, and will adopt any pre-existing resources when it starts up
* Automatic per-VRF BGP instance creation/removal in FRR
* IPv6 router advertisement configuration according to the ipv6_ra_mode subnet
  attributes in database

# Planned features

* Configurable suppression of EVPN Type-3 Inclusive Multicast routes in order to limit
  broadcast traffic on networks where all IPs/MACs are known to OpenStack (and therefore
  preprovisioned)
* Configurable suppression of EVPN Type-5 Prefix routes (or unicast routes for
  underlay-routed networks) for networks where all IPs are known to OpenStack (and
  therefore preprovisioned and advertised as EVPN Type-2 MACIP routes), thus preventing
  traffic to unused IP address from reaching the compute node and triggering futile ARP
  queries
* Dynamic BGP listener on provider networkss, to allow VMs to use BGP to dynamically
  advertise anycast or failover addresses for their applications.

# Security considerations

It is assumed that only the admin will be able to insert rows in the `evpnnetworks` db
table, and that this is only done for networks that are managed by trusted entities.
(Typically only admin is able to create provider networks in the first place.)

If this is not the case, e.g., if a provider network found in `evpnnetworks` is created
in a project belonging to an (untrusted) tenant, that tenant may potentially hijack
other traffic by creating routes or subnets that conflict with legitimate use elsewhere
in the network. These will be advertised automatically in EVPN. So don't do that...

# Quick start

## Installation

The agent is implemented as a executable Python package which can be installed from
source like so:

```
python3 -m build
pip3 install dist/evpn_agent-*.whl
```

## Invocation

To start the from the command line, simply run:

```
python3 -m evpn_agent
```

Supported command line options:

```
  -h, --help     show this help message and exit
  -1, --oneshot  Run main loop once and then exit
  -d, --debug    Set log level to DEBUG
  -v, --verbose  Set log level to INFO
```

See `evpn_agent.service` for an example systemd unit file that can be used to start the
agent at boot, which will also restart it if it crashes.

## Configuration

See `evpn_agent.ini` for the config file, which contains descriptions of all the
available configuration options and their default values.

It is mandatory to configure the `user`, `host` and `password` options in the `[db]`
section so that the agent can access the Neutron database. All other settings can be
left at the defaults.

## Database table

The EVPN agent stores some extra per-network metadata in a separate table in the neutron
database.

Create it like so:

```
CREATE TABLE evpnnetworks (
  id VARCHAR(36) NOT NULL,
  l2vni MEDIUMINT UNSIGNED DEFAULT NULL,
  l3vni MEDIUMINT UNSIGNED DEFAULT NULL,
  PRIMARY KEY (id),
  FOREIGN KEY (id) REFERENCES networks(id) ON DELETE CASCADE
);
```

## FRR

The agent relies on FRR to speak BGP with the external data centre network. Here's an
example minimal config `frr.conf` file:

```

! The compute node's underlay IP address is assigned to the loopback interface, so it
! does not depend on the status of a single physical network interface, thus providing
! redundancy if the hypervisor has multiple interfaces. This can of course be configured
! outside of FRR as well, using NetworkManager, systemd-networkd or what have you.
interface lo
 ip address 192.0.2.1/32
exit

! The BGP instance for the underlay, here using unnumbered eBGP with two uplink
! interfaces. As long as the routes advertised by one compute node is are received by
! all the others it may of course be adpated freely to suit the speicfic network the
! agent is being deployed in.
router bgp 4200000000
 bgp router-id 192.0.2.1

 ! Use ECMP to load balance outbound traffic across both eth0 and eth1 
 bgp bestpath as-path multipath-relax

 ! Establish unnumbered eBGP sessions to the uplink swiches eth0/eth1 are connected to
 neighbor UPLINK peer-group
 neighbor UPLINK remote-as external
 neighbor eth0 interface peer-group UPLINK
 neighbor eth1 interface peer-group UPLINK
 
 address-family ipv4 unicast
  ! Ensure our own loopback address are advertised to the uplinks.
  network 192.0.2.1/32

  ! See below
  neighbor UPLINK route-map UPLINK-IN in
 exit-address-family

 ! address-family ipv6 unicast is probably only necessary if you plan on routing between
 ! provider networks and the underlay using VRF route leaking (using l3vni=0)
 address-family ipv6 unicast
  neighbor UPLINK activate
  neighbor UPLINK route-map UPLINK-IN in
 exit-address-family

 ! This enables the exchange of EVPN routes - which is of course essential
 address-family l2vpn evpn
  neighbor UPLINK activate

  ! See below
  neighbor UPLINK route-map UPLINK-IN in

  ! This ensures that FRR advertises EVPN Type-2 and Type-3 routes for all VNIs
  ! configured by the EVPN Agent
  advertise-all-vni
 exit-address-family
exit

! This ensures that routes received on eth0 aren't re-advertised out eth1 and vice
! versa, ensuring that the compute node does not inadvertently act as a transit router
! or spine switch for traffic between the two switches connected to the two interfaces
route-map UPLINK-IN permit 1
 set community no-export
exit
```

## Routing policy

The Linux kernel lets packets to "fall through" from a VRF to the underlay, if there is
no route to the destination IP in the VRF routing table. This is because the routing
policy rules are tried in sequence until a matching route is found.

To prevent tenants from injecting traffic in the underlay, a custom rule can be added
so that any packets within a VRF is dropped:

```
ip -4 rule add priority 1001 l3mdev unreachable
ip -6 rule add priority 1001 l3mdev unreachable
```

This gets installed after the VRF rule (which by default is installed with prio 1000),
ensuring packets within a VRF aren't allowed to fall through to the main (underlay)
routing table (by default at priority 32766). (`unreachable` will generate ICMP errors
as the packets are dropped, if you want a silent drop, use `blackhole` instead.)

Additionally, the Linux kernel will by default route packets destined for local IPs
with higher priority than the VRF routing lookup. This means that a packet confined
within a VRF will reach local interfaces outside of the VRF (e.g., the primary underlay
IP assigned to the loopback interface), instead of being routed according to the VRF's
routing table. To prevent this, it is necessary to move the routing policy rule
governing local traffic to a priority after the VRF l3mdev rules, e.g.:

```
ip -4 rule add priority 2000 table local
ip -4 rule del priority 0 table local
ip -6 rule add priority 2000 table local
ip -6 rule del priority 0 table local
```

The included systemd unit will apply all of the above changes at startup.

# Usage

Create VLAN-based provider networks as normal. For the VLAN-based provider network that
the admin decides should be advertised in EVPN, it is neccesary to create a row in the
`evpnnetworks` database table, for example:

```
INSERT INTO evpnnetworks (id, l2vni, l3vni)
VALUES ('90e3fc3a-edfb-41a3-93fc-e779b02cf4a3', 12345, 67890);
```

This will make the EVPN agent associate the network with a VXLAN segment with L2VNI
12345, and create a IRB device (a layer-3 device to which the default gateway addresses
on the network is assigned), which will be associated with a VRF using L3VNI 67890.

If the `l2vni` column is left at its default value `NULL`, no VXLAN segment will be
created for the network, except if the `l2vni_offset` option is set in `evpn_agent.ini`.
If it is, then a VXLAN segment will be created with an L2VNI equal to the VLAN ID +
`l2vni_offset`.

If the `l2vni` column is set to `0`, no VXLAN segment will be created, regardless of the
`l2vni_offset` option being set.

If the `l3vni` column is left at its default value `NULL` or set to `0`, no L3VNI will
be created and bound to the VRF. The VRF created will be named after the VLAN ID of the
provider network.

If `l3vni` is set to a positive integer (not including `0`), the network will be bound
to an VRF with that ID, and a L3VNI + IRB for external L3 communication will be created
and bound to that VRF.

If `l3vni` is `NULL`, the anycast gateway address(es) will not be configured on the
provider network's VNI, nor any routes. Except for static neighbour entries (which
causes the advertisement of EVPN Type-2 MACIP routes that allows remote VTEPs to perform
neighbour suppression), no L3 information will be configured at all. This is meant to
facilitate EVPN centralised routing where the L3 gateway on the provider network is
located on a device external to OpenStack (reached via the L2VNI).

If `l3vni` is not `NULL`, the anycast gateway address(es) and any L3 routes will be
configured on the provider network's IRB device. If an L3VNI has been created (due to
`l3vni > 0`), these will be advertised as EVPN Type-5 prefix routes by FRR.

If `l3vni` is `0`, the routes in the VRF will be leaked imported into the default
(underlay) VRF and vice versa. Additionally, static host routes will be created for each
active port, so that these host routes are also leaked into the default VRF, ensuring
optimal routing for traffic to known hosts. (Normally, within a VRF bound to an L3VNI,
Type-2 MACIP routes ensure optimal routing, but these do not get leaked between VRFs.)