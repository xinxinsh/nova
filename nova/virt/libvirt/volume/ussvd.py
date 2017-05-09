# Copyright 2016 Chinac Corp.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from __future__ import absolute_import
import socket
import sys

from nova.i18n import _LI
from nova.virt.libvirt.volume import volume as libvirt_volume
from oslo_log import log as logging

LOG = logging.getLogger(__name__)

try:
    from ussvd.client import Client as UssvdClient
except Exception as e:
    pass


class LibvirtUssvdVolumeDriver(libvirt_volume.LibvirtBaseVolumeDriver):
    """Class implements libvirt part of volume driver for Ussvd."""

    def __init__(self, connection):
        super(LibvirtUssvdVolumeDriver,
              self).__init__(connection, is_block_dev=False)
        if 'ussvd.client' in sys.modules.keys():
            self.ussvd = UssvdClient()

    def get_config(self, connection_info, mount_device):
        """Connect the volume. Returns xml for libvirt."""
        conf = super(LibvirtUssvdVolumeDriver,
                     self).get_config(connection_info, mount_device)

        data = connection_info['data']

        conf.source_type = data['device_type']
        device_path = self.ussvd.active_volume(socket.gethostname(),
                                               data['datastore'],
                                               data['volume_name'])
        if not device_path:
            raise Exception("device path found")
        LOG.info(_LI('ussvd deivce path: %s'), device_path)
        conf.source_path = device_path
        conf.driver_format = data['format']

        return conf
