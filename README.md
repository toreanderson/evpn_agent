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
* L3 routing of provider networks directly in the underlay/default VRF (advertised as
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
* Configurable suppression of EVPN Type-5 Prefix routes (or unicast routes for
  underlay-routed networks) for networks where all IPs are known to OpenStack (and
  therefore preprovisioned and advertised as EVPN Type-2 MACIP routes), thus preventing
  traffic to unused IP address from reaching the compute node and triggering futile ARP
  queries
* Dynamic BGP listener on provider networks, to allow VMs to use BGP to dynamically
  advertise anycast or failover addresses for their applications.

# Planned features

* Configurable suppression of EVPN Type-3 Inclusive Multicast routes in order to limit
  broadcast traffic on networks where all IPs/MACs are known to OpenStack (and therefore
  preprovisioned)

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

## Upgrading

Install the new version as described above, and restart the agent. It will adopt any
pre-existing resources and change them as necessary. Downgrading is done the same way.

### Upgrading from version 0.0.8 or earlier

Provider networks with `l3vni=0` are no longer placed in a separate VRF, but are routed
directly in the underlay (see [l3vni](#l3vni) below). The agent migrates existing
networks automatically, which will cause a brief interruption of traffic to and from
the VMs on those networks. Before upgrading hypervisors with such networks, ensure
that:

* `no bgp default ipv4-unicast` is configured in the underlay BGP instance in
  `frr.conf`, and that all neighbours that should exchange IPv4 unicast routes (e.g.,
  the uplinks) are explicitly activated in the `ipv4 unicast` address family. See the
  example FRR config below.
* There are no `redistribute connected` or `redistribute kernel` statements in the
  underlay BGP instance in `frr.conf`, as the agent now configures these itself.
* `ip protocol bgp route-map` is configured with a route map that sets the primary
  loopback address as the source address, if the networks use the dynamic BGP
  listener. See the example FRR config below.
* `/etc/gai.conf` is configured to prefer IPv4, if the networks have IPv6 subnets. See
  [Underlay routing and source address
  selection](#underlay-routing-and-source-address-selection) below.
* Any firewall rules on the hypervisor that match traffic from the VMs on the networks
  by the VRF devices (e.g., `-i vrf-+`), such as rules allowing incoming BGP sessions to
  the [dynamic BGP listener](#dynamic-bgp-listener), also match the IRB devices (e.g.,
  `-i irb-+`), as that is where this traffic will be seen instead. The same applies to
  rules matching outgoing traffic to the VMs (e.g., `-o vrf-+`).

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
  advertise_connected BOOLEAN DEFAULT TRUE,
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

 ! Do not implicitly activate all neighbours for the IPv4 unicast address family. This
 ! is required, as the EVPN agent relies on it to ensure that BGP speakers connecting to
 ! the dynamic BGP listener (see below) on provider networks with l3vni=0 are only
 ! activated for the address family of the listener. Neighbours that should exchange
 ! IPv4 unicast routes must therefore be explicitly activated, as done for the UPLINK
 ! peer group below.
 no bgp default ipv4-unicast

 ! Workaround for an FRR bug present in FRR v8 (at least), that causes BGP routes
 ! with a next-hop covered by a on-link kernel route to be considered as "not connected"
 ! and therefore installed as "inactive". The EVPN agent adds such on-link kernel host
 ! routes for each port on underlay-routed provider networks (l3vni=0), so this affects
 ! routes received from VMs via the dynamic BGP listener (see below) on such networks.
 ! If running a later version of FRR, or not using underlay routing, then this setting
 ! is probably not necessary.
 bgp disable-ebgp-connected-route-check

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

  neighbor UPLINK activate

  ! See below
  neighbor UPLINK route-map UPLINK-IN in
 exit-address-family

 ! address-family ipv6 unicast is probably only necessary if you plan on routing
 ! provider networks with IPv6 subnets directly in the underlay (using l3vni=0).
 !
 ! Note that the EVPN agent adds "redistribute connected" and "redistribute kernel"
 ! (with route maps named underlay-redistribute-connected/kernel) to the ipv4 unicast
 ! and ipv6 unicast address families here if there are provider networks with l3vni=0
 ! active on the hypervisor. Do not configure these redistribute statements yourself.
 ! If you need to redistribute other connected or kernel routes, add entries to the
 ! route maps with sequence numbers between 4095 and 65534 (the agent uses the VLAN
 ! IDs for its own entries, and 65535 for a final deny entry).
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

! This makes the hypervisor use its primary loopback address as the source address when
! it communicates with destinations reached via IPv4 BGP routes in the underlay. This is
! necessary for routes received from VMs via the dynamic BGP listener (see below) on
! provider networks with l3vni=0, as the anycast gateway address on the provider
! network would otherwise be used, cf. the "Underlay routing and source address
! selection" section below. Note that zebra will not install routes with a source
! address that is not present on the hypervisor, so the loopback address must be
! configured before any BGP routes are received.
route-map UNDERLAY-SRC permit 10
 set src 192.0.2.1
exit
ip protocol bgp route-map UNDERLAY-SRC
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

## Underlay routing and source address selection

Provider networks with `l3vni=0` have their IRBs placed directly in the default VRF,
which means that the anycast gateway addresses configured on them become candidates when
the hypervisor selects the source address for connections it initiates itself (e.g., to
package repositories). Using an anycast gateway address as the source address for
anything other than a VM running on the same hypervisor will not work, as the return
traffic will be routed to whichever hypervisor happens to be closest to the sender.

For IPv4, this is not a problem, as the kernel will prefer the primary IPv4 address on
the loopback interface for destinations reached via the uplinks, and the agent sets it
as the preferred source address on the routes via the IRBs (both the routes it adds
itself, such as the host routes for the ports on the network, and the prefix routes
the kernel automatically creates for the on-link subnets). The exception is routes
received from VMs via the [dynamic BGP listener](#dynamic-bgp-listener), which are
installed by FRR. To make FRR set the preferred source address on these, configure
`ip protocol bgp route-map` with a route map that uses `set src`, as shown in the
example FRR config above.

For IPv6, however, if the hypervisor has no global IPv6 address of its own, the anycast
gateway addresses on the IPv6 subnets will be the only candidates. This in turn makes
`getaddrinfo()` in glibc sort IPv6 destination addresses before IPv4 ones, so dual-stack
destinations will be attempted over IPv6 first, which will fail. To make glibc prefer
IPv4, add the following to `/etc/gai.conf`:

```
precedence ::ffff:0:0/96  100
```

Note that `/etc/gai.conf` is only honoured by glibc. Software using other resolvers,
such as Go programs using the pure Go resolver or programs linked with musl libc, will
still attempt IPv6 first, and fall back to IPv4 only after a timeout, if at all. (Go's
default "Happy Eyeballs" fallback delay is 300 milliseconds.)

### Optional hardening

If the above turns out to be a problem, locally originated TCP and UDP traffic to
global IPv6 destinations can be made to fail immediately with routing policy rules:

```
ip -6 rule add priority 2099 iif lo ipproto tcp sport 179 lookup main
ip -6 rule add priority 2100 iif lo ipproto tcp to 2000::/3 prohibit
ip -6 rule add priority 2101 iif lo ipproto udp to 2000::/3 prohibit
```

This makes `connect()` fail with `EACCES`, which causes all resolvers and clients to move
on to IPv4 straight away. ICMPv6 is not affected, so the hypervisor can still send ICMPv6
errors (e.g., Packet Too Big) while forwarding traffic.

The rules are placed after the VRF rules (priority 1000 and 1001, see above), so that
they do not affect sockets bound to VRFs, and after the rule for the local routing table
(priority 2000), so that the hypervisor can still connect to its own addresses. The rule
with priority 2099 ensures that the dynamic BGP listener (see below) still works for BGP
speakers connecting to it over IPv6 on networks with `l3vni=0`.

Note that these rules also prevent the hypervisor from initiating TCP and UDP connections
to VMs on IPv6 subnets. It would have had to use the anycast gateway address as the
source address anyway, which would only work for VMs on the same hypervisor, and only
until they are live migrated elsewhere. The same applies to any other services on the
hypervisor (other than the dynamic BGP listener) that VMs connect to over IPv6.

To apply these rules at startup, they can be added to the systemd unit like so:

```
ExecStartPre=-ip -6 rule add priority 2099 iif lo ipproto tcp sport 179 lookup main
ExecStartPre=-ip -6 rule add priority 2100 iif lo ipproto tcp to 2000::/3 prohibit
ExecStartPre=-ip -6 rule add priority 2101 iif lo ipproto udp to 2000::/3 prohibit
```

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

## l2vni

If the `l2vni` column is left at its default value `NULL`, no VXLAN segment will be
created for the network, except if the `l2vni_offset` option is set in `evpn_agent.ini`.
If it is, then a VXLAN segment will be created with an L2VNI equal to the VLAN ID +
`l2vni_offset`.

If the `l2vni` column is set to `0`, no VXLAN segment will be created, regardless of the
`l2vni_offset` option being set.

## l3vni

If the `l3vni` column is left at its default value `NULL`, no L3VNI will be created,
and the provider network's IRB will be bound to an isolated VRF named after the VLAN ID
of the provider network.

If `l3vni` is set to a positive integer (not including `0`), the network will be bound
to an VRF with that ID, and a L3VNI + IRB for external L3 communication will be created
and bound to that VRF.

If `l3vni` is set to `0`, no VRF will be created. The provider network's IRB will
instead be placed directly in the default VRF, i.e., the network will be routed in the
underlay.

If `l3vni` is `NULL`, the anycast gateway address(es) will not be configured on the
provider network's VNI, nor any routes. Except for static neighbour entries (which
causes the advertisement of EVPN Type-2 MACIP routes that allows remote VTEPs to perform
neighbour suppression), no L3 information will be configured at all. This is meant to
facilitate EVPN centralised routing where the L3 gateway on the provider network is
located on a device external to OpenStack (reached via the L2VNI).

If `l3vni` is not `NULL`, the anycast gateway address(es) and any L3 routes will be
configured on the provider network's IRB device. If an L3VNI has been created (due to
`l3vni > 0`), these will be advertised as EVPN Type-5 prefix routes by FRR.

If `l3vni` is `0`, the routes will be advertised as regular IPv4/IPv6 unicast routes by
the underlay BGP instance in FRR. Additionally, static host routes will be created for
each active port, so that these are advertised as well, ensuring optimal routing for
traffic to known hosts. (Normally, within a VRF bound to an L3VNI, Type-2 MACIP routes
ensure optimal routing, but these are not available in the underlay.) Since the networks
are routed in the default VRF, the hypervisor itself, as well as any other provider
networks with `l3vni=0`, can reach VMs on these networks directly. See the section
[Underlay routing and source address selection](#underlay-routing-and-source-address-selection)
below for an important caveat.

## advertise_connected

This boolean value (default TRUE) controls whether or not the directly connected subnets
on the provider network will be advertised as an aggregate route in BGP (i.e., an EVPN
Type-5 Prefix route for VRFs with an L3VNI, or an BGP IPv4/IPv6 Unicast route for
networks routed in the underlay).

This does not impact the advertisement of host routes for the IP addresses associated
with each OpenStack port active on the hypervisor, so assuming it is known in advance
that all active IP addresses on the network are associated with OpenStack ports,
disabling this is a good way of reducing the amount of pointless ARP/ND traffic
generated because of junk traffic (vulnerability scanners and so on) targeting inactive
IP addresses within the subnets.

## Dynamic BGP listener

The agent can dynamically configure FRR to listen for BGP connections from hosts on the
provider network, allowing them to advertise host routes or prefixes into the VRF (or
the underlay, for networks with `l3vni=0`). This
can be useful in order to advertise anycast or fail-over service addresses.

To use this feature, create a subnet route with a next-hop within `0.179.0.0/16` or
`::179:0:0/96`. The magic number `179` is chosen because it is the TCP port used by BGP.
The two numbers to the right of `179` is used as the `ge` and `le` values in the
generated FRR prefix list
([documentation](https://docs.frrouting.org/en/latest/filter.html#ip-prefix-list)). For
example:

```
(openstack) subnet set --host-route destination=2001:db8::/64,gateway=::179:64:64 subnet
```

The above will allow BGP speakers in `subnet` to advertise `2001:db8::/64` via BGP, but
no subset of it.

Another example:

```
(openstack) subnet set --host-route destination=192.0.2.0/24,gateway=0.179.28.32 subnet
```

This would allow BGP speakers in `subnet` to advertise any prefix that falls within
192.0.2.0/24 as long as the prefix length of the advertised route is between /28 and
/32, inclusive. Unlike the previous example, advertising the entire prefix
(`192.0.2.0/24`) will not be permitted, because the prefix length of 24 would fail the
`ge 28` check.

The prefix length (right of the slash in `destination`) must be smaller or equal to
`ge`, which in turn must be smaller or equal to `le`. The agent will log an error if
this restriction is not respected.

Note that the `ge` and `le` values are read as strings cast to decimal integers, so if
you want to specify a `ge` and/or `le` of, say, 122, use `::179:122:122` - do *not*
convert decimal 122 to hexadecimal (0x)7a, even though this is written inside a
hexadecimal IPv6 address.

For provider networks with `l3vni=0`, the listener is configured in the underlay BGP
instance in FRR, which must have `no bgp default ipv4-unicast` configured (see the
example FRR config above). Otherwise, BGP speakers connecting to an IPv6 listener would
also be activated for the IPv4 unicast address family, where no prefix list is applied,
allowing them to advertise arbitrary IPv4 routes into the underlay.

Remember to add the addresses or prefixes you want to advertise to the
`allowed_address_pairs` attribute of the BGP speaking VM's port, if it has port security
enabled. Otherwise egress traffic from the advertised address/prefix will be dropped.
