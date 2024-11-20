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

import configparser
import logging
import optparse

log = logging.getLogger(__name__)

conf = configparser.ConfigParser()

# Set defaults
conf["agent"] = {
    "interval": 1,
    "loglevel": "WARNING",
    "physical_network": "physnet1",
    "rt_proto": "255",
    "rt_table_offset": "100000000",
}
conf["bridge"] = {
    "address": "00:00:5e:00:01:00",
    "name": "br-evpn",
    "mtu": 9216,
    "veth": "veth-to-ovs",
}
conf["db"] = {
    "database": "neutron",
}
conf["ovs"] = {
    "name": "br-ex",
    "veth": "veth-to-evpn",
}

# Read config file
conf.read("/etc/neutron/evpn_agent.ini")

# Add config overrides from commmand line
parser = optparse.OptionParser()
parser.add_option(
    "-1",
    "--oneshot",
    dest="oneshot",
    default=False,
    action="store_true",
    help="Run main loop once and then exit",
)
parser.add_option(
    "-d",
    "--debug",
    dest="debug",
    default=False,
    action="store_true",
    help="Set log level to DEBUG",
)
parser.add_option(
    "-v",
    "--verbose",
    dest="verbose",
    default=False,
    action="store_true",
    help="Set log level to INFO",
)

opts, remainder = parser.parse_args()

if opts.debug:
    conf["agent"]["loglevel"] = "DEBUG"
elif opts.verbose:
    conf["agent"]["loglevel"] = "INFO"

if opts.oneshot:
    conf["agent"]["oneshot"] = str(opts.oneshot)
