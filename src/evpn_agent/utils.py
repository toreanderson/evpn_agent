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

import json
import logging
import subprocess

log = logging.getLogger(__name__)


def cmd(args, *, check=True, **kwargs):
    log.debug(f"Executing: {args}")
    proc = subprocess.run(args, check=check, **kwargs)
    return proc


def jsoncmd(args):
    proc = cmd(args, capture_output=True)
    data = json.loads(proc.stdout)
    #log.debug(f"Decoded JSON: {data}")
    return data
