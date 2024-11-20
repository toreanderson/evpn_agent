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
from .utils import cmd
from .config import conf

log = logging.getLogger(__name__)


def ensure_veth():
    proc = cmd(
        ["ovs-vsctl", "list-ports", conf["ovs"]["name"]], capture_output=True, text=True
    )
    if not conf["ovs"]["veth"] in proc.stdout.splitlines():
        log.warning(f'Adding {conf["ovs"]["veth"]} to OVS bridge {conf["ovs"]["name"]}')
        cmd(["ovs-vsctl", "add-port", conf["ovs"]["name"], conf["ovs"]["veth"]])
