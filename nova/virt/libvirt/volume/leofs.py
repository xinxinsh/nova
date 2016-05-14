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

from nova.virt.libvirt.volume import volume as libvirt_volume


class LibvirtLeofsVolumeDriver(libvirt_volume.LibvirtBaseVolumeDriver):
    """Class implements libvirt part of volume driver for leofs."""

    def __init__(self, connection):
        super(LibvirtLeofsVolumeDriver,
              self).__init__(connection, is_block_dev=False)

    def connect_volume(self, connection_info, mount_device):
        """Connect the volume. Returns xml for libvirt."""
        conf = super(LibvirtLeofsVolumeDriver,
                     self).connect_volume(connection_info, mount_device)

        data = connection_info['data']

        path = data['device_path']
        conf.source_type = 'file'
        conf.source_path = path

        conf.driver_format = connection_info['data'].get('format', 'raw')

        return conf
