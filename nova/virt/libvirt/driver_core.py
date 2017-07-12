# Copyright 2016 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
# Copyright (c) 2010 Citrix Systems, Inc.
# Copyright (c) 2011 Piston Cloud Computing, Inc
# Copyright (c) 2012 University Of Minho
# (c) Copyright 2013 Hewlett-Packard Development Company, L.P.
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

"""
A connection to a hypervisor through libvirt.

Supports KVM, LXC, QEMU, UML, XEN and Parallels.

"""

import libvirt
import os
import socket
import time
import uuid

from eventlet import tpool
from lxml import etree
from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import excutils
from oslo_utils import strutils
from oslo_utils import units
from six.moves import range

from nova.api.metadata import base as instance_metadata
from nova.compute import power_state
from nova.compute import utils as compute_utils
import nova.conf
from nova import context as nova_context
from nova import exception
from nova.i18n import _
from nova.i18n import _LE
from nova.i18n import _LI
from nova.i18n import _LW
from nova import image
from nova.image import glance
from nova import objects
from nova.pci import utils as pci_utils
from nova import utils
from nova.virt import configdrive
from nova.virt import driver as virt_driver
from nova.virt import event as virtevent
from nova.virt.libvirt import config as vconfig
from nova.virt.libvirt import host
from nova.virt.libvirt import imagebackend
from nova.virt.libvirt import imagecache
from nova.virt.libvirt.storage import rbd_utils
from nova.virt.libvirt import utils as libvirt_utils
from nova.virt.libvirt import vif as libvirt_vif
from nova import volume


LOG = logging.getLogger(__name__)

# Downtime period in milliseconds
LIVE_MIGRATION_DOWNTIME_MIN = 100
# Step count
LIVE_MIGRATION_DOWNTIME_STEPS_MIN = 3
# Delay in seconds
LIVE_MIGRATION_DOWNTIME_DELAY_MIN = 10

libvirt_opts = [
    cfg.StrOpt('virt_type',
               default='kvm',
               choices=('kvm', 'lxc', 'qemu', 'uml', 'xen', 'parallels'),
               help='Libvirt domain type'),
    ]

qga_proxy_opts = [
    cfg.StrOpt('qga_proxy_host',
               default='127.0.0.1',
               help='The qga proxy server host'),
    cfg.IntOpt('qga_proxy_port',
               default=8500,
               help='The qga proxy server port'),
    cfg.IntOpt('timeout',
               default=20,
               help='Time out for accessing qga-proxy http API'),
]

CONF = nova.conf.CONF
CONF.register_opts(libvirt_opts, 'libvirt')
CONF.register_opts(qga_proxy_opts, group='qga_proxy')
CONF.import_opt('host', 'nova.netconf')
CONF.import_opt('my_ip', 'nova.netconf')
CONF.import_opt('enabled', 'nova.compute.api',
                group='ephemeral_storage_encryption')
CONF.import_opt('cipher', 'nova.compute.api',
                group='ephemeral_storage_encryption')
CONF.import_opt('key_size', 'nova.compute.api',
                group='ephemeral_storage_encryption')
CONF.import_opt('live_migration_retry_count', 'nova.compute.manager')
CONF.import_opt('server_proxyclient_address', 'nova.spice', group='spice')
CONF.import_opt('vcpu_pin_set', 'nova.conf.virt')
CONF.import_opt('hw_disk_discard', 'nova.virt.libvirt.imagebackend',
                group='libvirt')
CONF.import_group('workarounds', 'nova.utils')
CONF.import_opt('iscsi_use_multipath', 'nova.virt.libvirt.volume.iscsi',
                group='libvirt')
CONF.import_opt('iso_path', 'nova.compute.manager')

# The libvirt driver will prefix any disable reason codes with this string.
DISABLE_PREFIX = 'AUTO: '
# Disable reason for the service which was enabled or disabled without reason
DISABLE_REASON_UNDEFINED = None


def patch_tpool_proxy():
    """eventlet.tpool.Proxy doesn't work with old-style class in __str__()
    or __repr__() calls. See bug #962840 for details.
    We perform a monkey patch to replace those two instance methods.
    """
    def str_method(self):
        return str(self._obj)

    def repr_method(self):
        return repr(self._obj)

    tpool.Proxy.__str__ = str_method
    tpool.Proxy.__repr__ = repr_method


patch_tpool_proxy()

# fsFreeze/fsThaw requirement
MIN_LIBVIRT_FSFREEZE_VERSION = (1, 2, 5)

VIR_DOMAIN_NOSTATE = 0
VIR_DOMAIN_RUNNING = 1
VIR_DOMAIN_BLOCKED = 2
VIR_DOMAIN_PAUSED = 3
VIR_DOMAIN_SHUTDOWN = 4
VIR_DOMAIN_SHUTOFF = 5
VIR_DOMAIN_CRASHED = 6
VIR_DOMAIN_PMSUSPENDED = 7

LIBVIRT_POWER_STATE = {
    VIR_DOMAIN_NOSTATE: power_state.NOSTATE,
    VIR_DOMAIN_RUNNING: power_state.RUNNING,
    # NOTE(maoy): The DOMAIN_BLOCKED state is only valid in Xen.
    # It means that the VM is running and the vCPU is idle. So,
    # we map it to RUNNING
    VIR_DOMAIN_BLOCKED: power_state.RUNNING,
    VIR_DOMAIN_PAUSED: power_state.PAUSED,
    # NOTE(maoy): The libvirt API doc says that DOMAIN_SHUTDOWN
    # means the domain is being shut down. So technically the domain
    # is still running. SHUTOFF is the real powered off state.
    # But we will map both to SHUTDOWN anyway.
    # http://libvirt.org/html/libvirt-libvirt.html
    VIR_DOMAIN_SHUTDOWN: power_state.SHUTDOWN,
    VIR_DOMAIN_SHUTOFF: power_state.SHUTDOWN,
    VIR_DOMAIN_CRASHED: power_state.CRASHED,
    VIR_DOMAIN_PMSUSPENDED: power_state.SUSPENDED,
}


class LibvirtDriverCore(virt_driver.ComputeDriver):

    def __init__(self, virtapi, read_only=False):
        super(LibvirtDriverCore, self).__init__(virtapi)

        self._host = host.Host('qemu:///system', read_only,
                               lifecycle_event_handler=self.emit_event,
                               conn_event_handler=self._handle_conn_event)

        self._volume_api = volume.API()
        self._image_api = image.API()

        self.vif_driver = libvirt_vif.LibvirtGenericVIFDriver()
        self.image_backend = imagebackend.Backend(CONF.use_cow_images)

    def emit_event(self, event):
        """Dispatches an event to the compute manager.

        Invokes the event callback registered by the
        compute manager to dispatch the event. This
        must only be invoked from a green thread.
        """

        if not self._compute_event_callback:
            LOG.debug("Discarding event %s", str(event))
            return

        if not isinstance(event, virtevent.Event):
            raise ValueError(
                _("Event must be an instance of nova.virt.event.Event"))

        try:
            LOG.debug("Emitting event %s", str(event))
            self._compute_event_callback(event)
        except Exception as ex:
            LOG.error(_LE("Exception dispatching event %(event)s: %(ex)s"),
                      {'event': event, 'ex': ex})

    def _handle_conn_event(self, enabled, reason):
        LOG.info(_LI("Connection event '%(enabled)d' reason '%(reason)s'"),
                 {'enabled': enabled, 'reason': reason})
        self._set_host_enabled(enabled, reason)

    def _set_host_enabled(self, enabled,
                          disable_reason=DISABLE_REASON_UNDEFINED):
        """Enables / Disables the compute service on this host.

           This doesn't override non-automatic disablement with an automatic
           setting; thereby permitting operators to keep otherwise
           healthy hosts out of rotation.
        """

        status_name = {True: 'disabled',
                       False: 'enabled'}

        disable_service = not enabled

        ctx = nova_context.get_admin_context()
        try:
            service = objects.Service.get_by_compute_host(ctx, CONF.host)

            if service.disabled != disable_service:
                # Note(jang): this is a quick fix to stop operator-
                # disabled compute hosts from re-enabling themselves
                # automatically. We prefix any automatic reason code
                # with a fixed string. We only re-enable a host
                # automatically if we find that string in place.
                # This should probably be replaced with a separate flag.
                if not service.disabled or (
                        service.disabled_reason and
                        service.disabled_reason.startswith(DISABLE_PREFIX)):
                    service.disabled = disable_service
                    service.disabled_reason = (
                       DISABLE_PREFIX + disable_reason
                       if disable_service else DISABLE_REASON_UNDEFINED)
                    service.save()
                    LOG.debug('Updating compute service status to %s',
                              status_name[disable_service])
                else:
                    LOG.debug('Not overriding manual compute service '
                              'status with: %s',
                              status_name[disable_service])
        except exception.ComputeHostNotFound:
            LOG.warn(_LW('Cannot update service status on host "%s" '
                         'since it is not registered.'), CONF.host)
        except Exception:
            LOG.warn(_LW('Cannot update service status on host "%s" '
                         'due to an unexpected exception.'), CONF.host,
                     exc_info=True)

    def _get_connection(self):
        return self._host.get_connection()

    _conn = property(_get_connection)

    def _correct_interface_infos(self, old_xml_str, replace_pci_address):
        """Replace the pci devices  which have already be used by
        other vms in destination host.
        """
        xml_doc = etree.fromstring(old_xml_str)
        for dev in xml_doc.findall('./devices/interface'):

            mac_tag = dev.find('mac')
            tag_address = mac_tag.get('address')
            if tag_address in replace_pci_address:
                pci_addr = replace_pci_address.get(tag_address)
                source_tag = dev.find('source')
                tag_dev = pci_utils.get_ifname_by_pci_address(pci_addr)
                if source_tag is not None:
                    old_ifname = source_tag.get('dev')
                    source_tag.set('dev', tag_dev)
                    LOG.debug('ifname %s => %s', (old_ifname, tag_dev))

        return etree.tostring(xml_doc)

    def list_mounted_cdrom(self, instance):
        return self._get_instance_mounted_cdrom(instance)

    def _get_instance_mounted_cdrom(self, instance):
        instance_dir = libvirt_utils.get_instance_path(instance)
        cdrom_path = os.path.join(instance_dir, 'cdrom.info')
        try:
            cdrom = libvirt_utils.load_file(cdrom_path)
            cdrom = cdrom.strip()
            return cdrom or None
        except Exception:
            return None

    def change_cdrom(self, instance, iso_name):
        mounted = self._get_instance_mounted_cdrom(instance)
        if iso_name == mounted:
            return
        try:
            _iso_name = '' if not iso_name else iso_name
            instance_dir = libvirt_utils.get_instance_path(instance)
            cdrom_path = os.path.join(instance_dir, 'cdrom.info')
            libvirt_utils.write_to_file(cdrom_path, _iso_name)

            virt_dom = self._lookup_by_name(instance['name'])
        except IOError as e:
            raise exception.MountCdromFailed(instance, error=e.strerror)
        except exception.InstanceNotFound:
            # pass if instance is power off
            return

        conf = self._get_guest_cdrom_device(instance, iso_name)

        try:
            flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
            state = LIBVIRT_POWER_STATE[virt_dom.info()[0]]
            if state == power_state.RUNNING:
                flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
            virt_dom.updateDeviceFlags(conf.to_xml(), flags)
        except libvirt.libvirtError as ex:
            libvirt_utils.write_to_file(cdrom_path, '')
            error_code = ex.get_error_code()
            if error_code == libvirt.VIR_ERR_NO_DOMAIN:
                LOG.warn(
                    _LW("During changing cdrom, instance disappeared."),
                    instance=instance)
            else:
                raise exception.MountCdromFailed(instance, error=ex.strerror)

    def set_qos_specs(self, instance, dev, qos_specs):
        if not qos_specs:
            qos_specs = {'total_bytes_sec': 0, 'total_iops_sec': 0}

        try:
            guest = self._host.get_guest(instance)
        except exception.InstanceNotFound:
            raise

        try:
            flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
            state = LIBVIRT_POWER_STATE[guest.get_info()[0]]
            guest.set_qos_specs(dev, qos_specs, flags)

            if state in [power_state.RUNNING, power_state.PAUSED]:
                flags = libvirt.VIR_DOMAIN_AFFECT_LIVE
                guest.set_qos_specs(dev, qos_specs, flags)
        except libvirt.libvirtError:
            LOG.error(_LE('set qos failed.'), instance=instance)
            raise

    def _can_quiesce(self, instance, image_meta):
        if (CONF.libvirt.virt_type not in ('kvm', 'qemu') or
            not self._host.has_min_version(MIN_LIBVIRT_FSFREEZE_VERSION)):
            raise exception.InstanceQuiesceNotSupported(
                instance_id=instance.uuid)

        if 'properties' in image_meta \
                and 'hw_qemu_guest_agent' in image_meta['properties']:
            hw_qga = image_meta.properties.get('hw_qemu_guest_agent', 'yes')
            if hw_qga.lower() == 'no':
                return False

        return True

    def _set_quiesced(self, context, instance, image_meta, quiesced):
        supported = self._can_quiesce(instance, image_meta)
        if not supported:
            raise exception.InstanceQuiesceNotSupported(
                instance_id=instance['uuid'],
                reason='QEMU guest agent is not enabled')

        if quiesced:
            message = {
                "execute": "guest-fsfreeze-freeze",
                "arguments": {}
            }
            ret = self.call_qga_proxy(instance, message, 180)
            if ret['msg'] != 'Sucess':
                raise Exception(ret['msg'])
        else:
            message = {
                "execute": "guest-fsfreeze-thaw",
                "arguments": {}
            }
            ret = self.call_qga_proxy(instance, message)
            if ret['msg'] != 'Sucess':
                raise Exception(ret['msg'])

    def _get_guest_cdrom_device(self, instance, iso_name=None,
                                read_file=False):
        """Get config of cdrom

        `iso_name` specifies that cdrom's name will be mounted to instance,
        `read_file` if True, the file named cdrom.info under instance path will
        be mounted, but only take effect when `cdrom` is None.
        """
        if not iso_name and read_file:
            mounted_cdrom = self._get_instance_mounted_cdrom(instance)
        else:
            mounted_cdrom = iso_name
        conf = vconfig.LibvirtConfigGuestDisk()
        conf.source_device = 'cdrom'
        conf.driver_name = 'qemu'
        conf.driver_format = 'raw'
        conf.target_dev = 'hdc'
        conf.target_bus = 'ide'
        if not mounted_cdrom:
            conf.source_path = ''
        else:
            conf.source_path = mounted_cdrom
        conf.readonly = True
        return conf

    def _check_instance_is_windows2003(self, context, block_device_info):
        # support volume_type: leofs, rbd, fibre_channel, nfs, qcow2

        block_device_mapping = virt_driver.block_device_info_get_mapping(
            block_device_info)

        for vol in block_device_mapping:
            if vol['mount_device'] == '/dev/vda':
                if vol['connection_info']['driver_volume_type'] \
                        in ['leofs', 'rbd', 'fibre_channel', 'nfs']:
                    volume_id = vol['connection_info']['serial']
                    # system volume info
                    volume_info = self._volume_api.get(context, volume_id)

                    # considering those snapshots/volumes which copy from
                    # another region, get image_os_type from
                    # volume_image_metadata firstly.
                    volume_image_metadata = \
                        volume_info['volume_image_metadata']
                    if 'image_os_type' in volume_image_metadata:
                        if volume_image_metadata['image_os_type'] == 'windows':
                            return True
                        else:
                            return False

                    # get image_id from volume_info
                    image_id = volume_info['volume_image_metadata']['image_id']

                    image_info = self._image_api.get(context, image_id)
                    properties = image_info['properties']
                    if 'image_os_type' in properties \
                            and properties['image_os_type'] == 'windows':
                        return True
                elif vol['connection_info']['driver_volume_type'] == 'ext':
                    volume_id = vol['connection_info']['serial']
                    # system volume info
                    volume_info = objects.ExtVolume.get_by_id(context,
                                                              volume_id)

                    # get image_id from volume_info
                    image_id = volume_info['glance_image_id']

                    image_info = self._image_api.get(context, image_id)
                    properties = image_info['properties']
                    if 'image_os_type' in properties \
                            and properties['image_os_type'] == 'windows':
                        return True
                else:
                    continue

        return False

    def _add_rng_device(self, guest, flavor):
        rng_device = vconfig.LibvirtConfigGuestRng()
        rate_bytes = flavor.extra_specs.get('hw_rng:rate_bytes', 0)
        period = flavor.extra_specs.get('hw_rng:rate_period', 0)
        if rate_bytes:
            rng_device.rate_bytes = int(rate_bytes)
            rng_device.rate_period = int(period)
        rng_path = CONF.libvirt.rng_dev_path
        if (rng_path and not os.path.exists(rng_path)):
            raise exception.RngDeviceNotExist(path=rng_path)
        rng_device.backend = rng_path
        guest.add_device(rng_device)

    def _set_qemu_guest_agent(self, guest, flavor, instance, image_meta):
        qga_enabled = True
        # Enable qga only if the 'hw_qemu_guest_agent' is equal to yes
        # if image_meta.properties.get('hw_qemu_guest_agent', False):
        #     LOG.debug("Qemu guest agent is enabled through image "
        #               "metadata", instance=instance)
        #     self._add_qga_device(guest, instance)
        if qga_enabled:
            qga = vconfig.LibvirtConfigGuestChannel()
            qga.type = "unix"
            # NOTE(linlh) Change org.qemu.guest_agent.0 to
            # org.qemu.guest_agent.1,
            # as org.qemu.guest_agent.0 is using by libvirt self
            qga.target_name = "org.qemu.guest_agent.1"
            qga.source_path = ("/var/lib/libvirt/qemu/%s.agent" %
                               instance['uuid'])
            guest.add_device(qga)

        rng_is_virtio = image_meta.properties.get('hw_rng_model') == 'virtio'
        rng_allowed_str = flavor.extra_specs.get('hw_rng:allowed', '')
        rng_allowed = strutils.bool_from_string(rng_allowed_str)
        if rng_is_virtio and rng_allowed:
            self._add_rng_device(guest, flavor)

    def _check_usbclient_list(self, usb_name):

        try:
            out, err = libvirt_utils.execute('usbclnt', '-l',
                                             run_as_root=True)
            if err:
                msg = _('usbclnt: usbclnt -l error \nError:%s') % err
                raise exception.NovaException(msg)
        except Exception:
            msg = _('usbclnt -l error')
            raise exception.NovaException(msg)

        output = [line.strip() for line in out.splitlines()]
        for line_num in range(len(output)):
            if(usb_name in output[line_num]
               and 'Mode: manual-connect   Status: connected' in
                    output[line_num + 2]):
                return True

        return False

    def get_usb_iserial(self, usb_vid, usb_pid, usb_name):
        try:
            out, err = libvirt_utils.execute('lsusb | grep %s:%s' %
                                             (usb_vid, usb_pid), shell=True,
                                             run_as_root=True)
            if err:
                msg = (_('Get usb %(usb_vid)s:%(usb_pid)s iSerial error') %
                       {'usb_vid': usb_vid, 'usb_pid': usb_pid})
                raise exception.NovaException(msg)

            for line in out.splitlines():
                if 'Bus' in line and 'Device' in line:
                    info = line.split(':')[0].split(' ')
                    if len(info) == 4:
                        bus = info[1]
                        device = info[3]
                        out_iSerial, err = \
                            libvirt_utils.execute('lsusb -D '
                                                  '/dev/bus/usb/%s/%s '
                                                  '| grep iSerial'
                                                  % (bus, device),
                                                  shell=True,
                                                  run_as_root=True)
                        iSerial_info = \
                            out_iSerial.strip().replace('  ', '').split(' ')
                        if len(iSerial_info) == 3 \
                                and iSerial_info[2] in usb_name:
                            return iSerial_info[2], bus, device
        except Exception:
            msg = (_('Get usb %(usb_vid)s:%(usb_pid)s iSerial error') %
                   {'usb_vid': usb_vid, 'usb_pid': usb_pid})
            raise exception.NovaException(msg)

        return None, None, None

    def get_usb_host_list(self, context):

        try:
            out, err = libvirt_utils.execute('usbsrv', '-l', run_as_root=True)
            if err:
                msg = _('usbsrv: get_usb_host_list error \nError:%s') % err
                raise exception.NovaException(msg)
        except Exception:
            msg = _('usbsrv -l error')
            raise exception.NovaException(msg)

        usb_list = []
        output = [line.strip() for line in out.splitlines()]

        for line_num in range((len(output) - 6) / 4):
            output_num = 4 * (line_num + 1)

            if('keyboard' in output[output_num].lower()
               or 'mouse' in output[output_num].lower()):
                continue
            else:
                usb_device = {}

                if self._check_usbclient_list(
                        output[output_num].lstrip()[3:].strip()):
                    continue

                usb_device['usb_name'] = \
                    output[output_num].lstrip()[3:].strip()

                usb_info = output[output_num + 1].split()
                usb_device['usb_vid'] = usb_info[1]
                usb_device['usb_pid'] = usb_info[3]
                usb_device['usb_port'] = usb_info[5]

                if usb_device['usb_vid'] == '0557' \
                        and usb_device['usb_pid'] == '2419':
                    continue

                usb_iserial, bus, device = self.get_usb_iserial(
                    usb_device['usb_vid'],
                    usb_device['usb_pid'],
                    usb_device['usb_name'])
                if usb_iserial:
                    usb_device['usb_pid'] = usb_device['usb_pid'] + ':' \
                                            + usb_iserial

                if 'in use by' in output[output_num + 2]:
                    host_ip = output[output_num + 2].split()[4].strip()
                    host_name = socket.gethostbyaddr(host_ip)[0]
                    usb_device['usb_host_status'] = _('in use by %s') % \
                                                    host_name
                elif 'not plugged' in output[output_num + 2]:
                    continue
                else:
                    usb_device['usb_host_status'] = \
                        output[output_num + 2].split(':')[1].strip()

                usb_list.append(usb_device)

        return usb_list

    def usb_shared(self, context, usb_vid, usb_pid, usb_port, shared):

        usb_pid_info = usb_pid.split(':')
        if len(usb_pid_info) == 2:
            usb_pid = usb_pid_info[0]

        if shared == "True":
            shared_flag = '-s'
        else:
            shared_flag = '-t'

        try:
            out, err = libvirt_utils.execute('usbsrv',
                                             shared_flag,
                                             '-vid', usb_vid,
                                             '-pid', usb_pid,
                                             '-usbport', usb_port,
                                             run_as_root=True)
            if err:
                msg = _('usbsrv: set shared error \nError:%s') % err
                raise exception.NovaException(msg)
        except Exception:
            msg = _('usbsrv error')
            raise exception.NovaException(msg)

    def usb_mapped(self, context, src_host_name, usb_vid, usb_pid,
                   usb_port, mapped):

        usb_iserial = None
        usb_pid_info = usb_pid.split(':')
        if len(usb_pid_info) == 2:
            usb_pid = usb_pid_info[0]
            usb_iserial = usb_pid_info[1]

        src_host_ip = socket.gethostbyname(src_host_name)
        if not src_host_ip:
            msg = _('get hostip through hostname:%s error') % src_host_name
            raise exception.NovaException(msg)

        server_address = _('%s:32032') % src_host_ip

        if mapped == "True":
            mapped_flag = '-connect'

            try:
                out_clnt, err = libvirt_utils.execute('usbclnt', '-l')
                if server_address not in out_clnt:
                    out, err = libvirt_utils.execute('usbclnt',
                                                     '-a',
                                                     server_address,
                                                     run_as_root=True)
                    if err:
                        msg = _('usbclnt: usbclnt add usbserver error'
                                ' \nError:%s') % err
                        raise exception.NovaException(msg)
            except Exception:
                msg = _('usbsrv -a error')
                raise exception.NovaException(msg)

            time.sleep(1)

        else:
            mapped_flag = '-disconnect'

        try:
            if mapped_flag == '-connect':
                out_check, err = libvirt_utils.execute('usbsrv', '-l')
                if 'Vid: %s' % usb_vid in out_check \
                        and 'Pid: %s' % usb_pid in out_check:
                    if usb_iserial:
                        if usb_iserial in out_check:
                            return
                    else:
                        return

            cmd = 'usbclnt %s -server %s -vid %s -pid %s -usbport %s' % \
                  (mapped_flag, server_address, usb_vid, usb_pid, usb_port)
            out, err = libvirt_utils.execute(cmd,
                                             shell=True,
                                             run_as_root=True)
            if err:
                msg = _('usbclnt: usb mapped error \nError:%s') % err
                raise exception.NovaException(msg)
        except Exception:
            msg = _('usbclnt error')
            raise exception.NovaException(msg)

    def _check_usb_xml(self, virt_dom, usb_vid, usb_pid, usb_iserial=None):

        try:
            xml = virt_dom.XMLDesc(0)
            tree = etree.fromstring(xml)

            for hostdev in tree.findall("./devices/hostdev"):
                if hostdev.get("type") == "usb":
                    vendor = hostdev.find("./source/vendor")
                    product = hostdev.find("./source/product")
                    address = hostdev.find("./source/address")

                    if(usb_vid in vendor.get("id")
                       and usb_pid in product.get("id")):
                        usb_bus = address.get("bus")
                        usb_device = address.get("device")

                        if usb_iserial:
                            iserial, bus, device = self.get_usb_iserial(
                                usb_vid,
                                usb_pid,
                                usb_iserial)
                            if usb_bus in bus and usb_device in device:
                                return True
                        else:
                            return True
        except libvirt.libvirtError as e:
            msg = _('get usb xml error: %s') % e
            raise exception.NovaException(msg)

        return False

    def usb_mounted(self, context, instance, usb_vid, usb_pid, mounted):

        usb_iserial = None
        usb_pid_info = usb_pid.split(':')
        if len(usb_pid_info) == 2:
            usb_pid = usb_pid_info[0]
            usb_iserial, bus, device = self.get_usb_iserial(
                usb_vid,
                usb_pid,
                usb_pid_info[1])

            usb_xml = '''
            <hostdev mode='subsystem' type='usb' managed='yes'>
                <source>
                    <vendor id='0x%s'/>
                    <product id='0x%s'/>
                    <address bus='%s' device='%s'/>
                </source>
            </hostdev>
            ''' % (usb_vid, usb_pid, bus, device)
        else:
            usb_xml = '''
            <hostdev mode='subsystem' type='usb' managed='yes'>
                <source>
                    <vendor id='0x%s'/>
                    <product id='0x%s'/>
                </source>
            </hostdev>
            ''' % (usb_vid, usb_pid)

        try:
            virt_dom = self._lookup_by_name(instance['name'])
        except exception.InstanceNotFound:
            raise exception.InstanceNotRunning(instance_id=instance['uuid'])

        if mounted == 'True':
            if not self._check_usb_xml(virt_dom, usb_vid,
                                       usb_pid, usb_iserial):
                try:
                    virt_dom.attachDevice(usb_xml)
                except libvirt.libvirtError as e:
                    msg = _('usb attach error: %s') % e
                    raise exception.NovaException(msg)
        else:
            try:
                virt_dom.detachDevice(usb_xml)
            except libvirt.libvirtError as e:
                msg = _('usb detach error: %s') % e
                raise exception.NovaException(msg)

    def usb_status(self, context, instance, usb_vid, usb_pid):

        usb_iserial = None
        usb_pid_info = usb_pid.split(':')
        if len(usb_pid_info) == 2:
            usb_pid = usb_pid_info[0]
            usb_iserial = usb_pid_info[1]

        try:
            virt_dom = self._lookup_by_name(instance['name'])
        except exception.InstanceNotFound:
            raise exception.InstanceNotRunning(instance_id=instance['uuid'])

        return self._check_usb_xml(virt_dom, usb_vid, usb_pid, usb_iserial)

    def get_usb_vm_status(self, context, usb_vid, usb_pid):

        usb_iserial = None
        usb_pid_info = usb_pid.split(':')
        if len(usb_pid_info) == 2:
            usb_pid = usb_pid_info[0]
            usb_iserial = usb_pid_info[1]

        vm_uuid = ''
        virt_doms = self._conn.listAllDomains()
        for virt_dom in virt_doms:
            if self._check_usb_xml(virt_dom, usb_vid, usb_pid, usb_iserial):
                vm_uuid = virt_dom.UUIDString()

        return vm_uuid

    def call_qga_proxy(self, instance, message, timeout=20):
        if 'id' not in message['arguments']:
            message['arguments']['id'] = str(uuid.uuid4())

        msg_json = [jsonutils.dumps(message)]
        post_data = jsonutils.dumps({instance.uuid: msg_json})

        returnval = dict(state='qga_proxy_downtime')
        try:
            uri = 'http://%s:%s' % (CONF.qga_proxy.qga_proxy_host,
                                    CONF.qga_proxy.qga_proxy_port)
            headers = {'TIMEOUT': timeout, 'Content-Type': 'application/json'}

            recv = utils.http_post(uri, post_data, headers)
            body = recv.read().strip()
            if recv.code != 200:
                raise Exception("Send to qga proxf failed: %s" % body)
        except Exception as e:
            returnval['msg'] = e
            return returnval

        try:
            r = jsonutils.loads(body)[0]['return']
            return r
        except Exception:
            raise exception.QgaExecuteFailure(instance=instance.uuid,
                                              error=body,
                                              method=message['execute'])

    def get_qga_is_live(self, instance):
        uri = 'http://%s:%s/test_qga' % (
            CONF.qga_proxy.qga_proxy_host, CONF.qga_proxy.qga_proxy_port)

        returnval = dict(state='qga_proxy_downtime')
        try:
            headers = {}
            recv = utils.http_post(uri, instance.uuid, headers)
            body = recv.read().strip()
            if recv.code != 200:
                raise Exception("Send to qga proxf failed: %s" % body)
        except Exception as e:
            returnval['msg'] = e
            return returnval
        return body

    def qga_getuptime(self, servers_list):
        returnval = dict()
        result = []

        # 1. check qga is live
        for server in servers_list:
            uri = 'http://%s:%s/test_qga' % (
                CONF.qga_proxy.qga_proxy_host, CONF.qga_proxy.qga_proxy_port)

            try:
                headers = {}
                recv = utils.http_post(uri, server['server_id'], headers)
                body = recv.read().strip()
                if recv.code != 200:
                    raise Exception("Send to qga proxf failed: %s" % body)
            except Exception:
                returnval['return'] = {}
                returnval['return']['msg'] = 'qga_proxy_downtime'
                returnval['return']['nova-compute'] = CONF.host
                return [returnval]

            # {u'state': True}
            if 'false' in body:
                servers_list.remove(server)
                ret = {
                    'return': {
                        'vm_uuid': server['server_id'],
                        'vm_status': 'active',
                        'msg': 'instance %s qga is down' % server['server_id'],
                        'value': ''
                    }
                }
                result.append(ret)

        # 2. getuptime
        message = dict()
        message['execute'] = 'guest-terminal-cmd'
        message['arguments'] = {}

        post_data = dict()
        for server in servers_list:
            message['arguments']['id'] = str(uuid.uuid4())
            message['arguments']['cmd'] = server['cmd']
            msg_json = [jsonutils.dumps(message)]
            post_data[server['server_id']] = msg_json

        try:
            uri = 'http://%s:%s' % (CONF.qga_proxy.qga_proxy_host,
                                    CONF.qga_proxy.qga_proxy_port)
            headers = {'TIMEOUT': 10, 'Content-Type': 'application/json'}

            recv = utils.http_post(uri, jsonutils.dumps(post_data), headers)
            body = recv.read().strip()
            if recv.code != 200:
                raise Exception("Send to qga proxf failed: %s" % body)
        except Exception:
            returnval['return'] = {}
            returnval['return']['msg'] = 'qga_proxy_downtime'
            returnval['return']['nova-compute'] = CONF.host
            return [returnval]

        try:
            ret = jsonutils.loads(body)
            for r in ret:
                r['return']['vm_status'] = 'active'

            result.extend(ret)
            return result
        except Exception:
            raise exception.QgaGetuptimeFailure(error=body,
                                                method=message['execute'])

    @staticmethod
    def _get_disk_config_path(instance, suffix=''):
        return os.path.join(libvirt_utils.get_instance_path(instance),
                            'disk.config' + suffix)

    @staticmethod
    def _get_disk_config_image_type():
        # TODO(mikal): there is a bug here if images_type has
        # changed since creation of the instance, but I am pretty
        # sure that this bug already exists.
        return 'rbd' if CONF.libvirt.images_type == 'rbd' else 'raw'

    def setup_config_driver(self, instance, files):
        LOG.info(_LI('Using config drive'), instance=instance)
        network_info = compute_utils.get_nw_info_for_instance(instance)
        extra_md = {}
        inst_md = instance_metadata.InstanceMetadata(instance,
            content=files, extra_md=extra_md, network_info=network_info)
        with configdrive.ConfigDriveBuilder(instance_md=inst_md) as cdb:
            configdrive_path = self._get_disk_config_path(instance)
            LOG.info(_LI('Creating config drive at %(path)s'),
                     {'path': configdrive_path}, instance=instance)
            try:
                if os.path.exists(configdrive_path):
                    libvirt_utils.chown(configdrive_path, os.getuid())
                cdb.make_drive(configdrive_path)
            except processutils.ProcessExecutionError as e:
                with excutils.save_and_reraise_exception():
                    LOG.error(_LE('Creating config drive failed '
                                  'with error: %s'),
                              e, instance=instance)
                    return False

        try:
            suffix = ''
            # Tell the storage backend about the config drive
            config_drive_image = self.image_backend.image(
                instance, 'disk.config' + suffix,
                self._get_disk_config_image_type())

            config_drive_image.import_file(
                instance, configdrive_path, 'disk.config' + suffix)
        finally:
            # NOTE(mikal): if the config drive was imported into RBD, then
            # we no longer need the local copy
            if CONF.libvirt.images_type == 'rbd':
                os.unlink(configdrive_path)
        return True

    def _lookup_by_name(self, instance_name):
        """Retrieve libvirt domain object given an instance name.

        All libvirt error handling should be handled in this method and
        relevant nova exceptions should be raised in response.

        """
        try:
            return self._conn.lookupByName(instance_name)
        except libvirt.libvirtError as ex:
            error_code = ex.get_error_code()
            if error_code == libvirt.VIR_ERR_NO_DOMAIN:
                raise exception.InstanceNotFound(instance_id=instance_name)

            msg = (_('Error from libvirt while looking up %(instance_name)s: '
                     '[Error Code %(error_code)s] %(ex)s') %
                   {'instance_name': instance_name,
                    'error_code': error_code,
                    'ex': ex})
            raise exception.NovaException(msg)

    def ensure_detach_disk_config(self, instance):
        instance_name = instance['name']
        try:
            virt_dom = self._lookup_by_name(instance_name)

            def _get_disk_xml_by_file(xml, basename):
                try:
                    doc = etree.fromstring(xml)
                except Exception:
                    return None
                ret = doc.findall('./devices/disk')
                for node in ret:
                    for child in node.getchildren():
                        if child.tag == 'source':
                            f = child.get('file')
                            if f and os.path.basename(f) == basename:
                                child.set('file', '')
                                return etree.tostring(node)
            xml = _get_disk_xml_by_file(virt_dom.XMLDesc(0), 'disk.config')
            if not xml:
                LOG.info(_LI('Not disk.config driver found'))
                return

            else:
                flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG | \
                        libvirt.VIR_DOMAIN_DEVICE_MODIFY_FORCE
                state = LIBVIRT_POWER_STATE[virt_dom.info()[0]]
                if state == power_state.RUNNING:
                    flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE

                virt_dom.updateDeviceFlags(xml, flags)

        except exception.InstanceNotFound:
            # NOTE(zhaoqin): If the instance does not exist, _lookup_by_name()
            #                will throw InstanceNotFound exception. Need to
            #                disconnect volume under this circumstance.
            LOG.warn(_LW("During detach disk.config, instance disappeared."))
        except libvirt.libvirtError as ex:
            # NOTE(vish): This is called to cleanup volumes after live
            #             migration, so we should still disconnect even if
            #             the instance doesn't exist here anymore.
            error_code = ex.get_error_code()
            if error_code == libvirt.VIR_ERR_NO_DOMAIN:
                # NOTE(vish):
                LOG.warn(_LW("During detach disk.config, instance "
                             "disappeared."))
            else:
                raise

    def instance_actions(self, context, instance, action):

        try:
            virt_dom = self._lookup_by_name(instance['name'])
        except exception.InstanceNotFound:
            raise exception.InstanceNotRunning(instance_id=instance['uuid'])

        if action == 'paused':
            # suspend change power_state to 3
            virt_dom.suspend()
            instance.vm_state = 'paused'
            instance.power_state = power_state.PAUSED
            instance.save()

        if action == 'resume':
            virt_dom.resume()
            instance.vm_state = 'active'
            instance.power_state = power_state.RUNNING
            instance.save()

    def create_memory_snapshot(self, context, instance, image_meta,
                               volume_mapping, vm_active, memory_file):

        metadata = {
            'is_public': False,
            'status': 'active',
            'properties': {
                'kernel_id': instance['kernel_id'],
                'image_state': 'available',
                'owner_id': instance['project_id'],
                'ramdisk_id': instance['ramdisk_id'],
            }
        }

        metadata['disk_format'] = 'raw'
        metadata['container_format'] = 'bare'

        try:
            virt_dom = self._lookup_by_name(instance['name'])
        except exception.InstanceNotFound:
            raise exception.InstanceNotRunning(instance_id=instance['uuid'])

        if memory_file:
            # save change power_state to 4 and vm_state to 'stopped'
            try:
                LOG.info(_LI("Export memory snapshot file to local"),
                         instance=instance)
                virt_dom.save(memory_file)
            except libvirt.libvirtError as ex:
                LOG.error(_LI("Memory snapshot save error, libvirt:%s"), ex)
            time.sleep(1)

            try:
                # restore only change power_state to 1,
                # vm_state still 'stopped'
                LOG.info(_LI("Restore instance from memory snapshot file"),
                         instance=instance)
                self._conn.restore(memory_file)

                if vm_active:
                    virt_dom.resume()
                    instance.vm_state = 'active'
                    instance.power_state = power_state.RUNNING
                else:
                    # change vm_state and power_state to 'paused'
                    # in the dashboard
                    instance.vm_state = 'paused'
                    instance.power_state = power_state.PAUSED
                instance.save()
            except Exception:
                msg = _('restore memory file error')
                raise exception.NovaException(msg)

            LOG.info(_LI("Memory snapshot image beginning upload"),
                     instance=instance)

            libvirt_utils.chown(memory_file, os.getuid())
            with libvirt_utils.file_open(memory_file) as image_file:
                self._image_api.update(context,
                                       image_meta['id'],
                                       metadata,
                                       image_file)
                LOG.info(_LI("Memory snapshot image upload complete"),
                         instance=instance)

            libvirt_utils.file_delete(memory_file)

    def rollback_to_memory_snapshot(self, context, instance,
                                    image_meta, memory_file):

        if instance.vm_state != 'stopped':
            msg = _('instance state must be stopped')
            raise exception.NovaException(msg)

        try:
            virt_dom = self._lookup_by_name(instance['name'])
        except exception.InstanceNotFound:
            raise exception.InstanceNotRunning(instance_id=instance['uuid'])

        image_service = glance.get_default_image_service()
        image_service.download(context,
                               image_meta['properties']['memory_snapshot_id'],
                               dst_path=memory_file)

        if memory_file:
            try:
                # restore only change power_state to 1,
                # vm_state still 'stopped'
                self._conn.restore(memory_file)
                time.sleep(1)

                # becase do the memory snapshot when the
                # vm_state of instance is 'paused', so
                # now resume
                virt_dom.resume()
                instance.vm_state = 'active'
                instance.power_state = power_state.RUNNING
                instance.save()
            except Exception:
                msg = _('restore memory file error')
                raise exception.NovaException(msg)
            finally:
                libvirt_utils.file_delete(memory_file)

    def set_interface_bandwidth(self, instance, vif,
                                inbound_kilo_bytes, outbound_kilo_bytes):

        virt_dom = self._lookup_by_name(instance['name'])
        flavor = objects.Flavor.get_by_id(
            nova_context.get_admin_context(read_deleted='yes'),
            instance['instance_type_id'])

        cfg = self.vif_driver.get_config(instance, vif, None, flavor,
                                         CONF.libvirt.virt_type, self._host)
        cfg.vif_inbound_average = inbound_kilo_bytes
        cfg.vif_outbound_average = outbound_kilo_bytes

        try:
            flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
            state = LIBVIRT_POWER_STATE[virt_dom.info()[0]]
            if state == power_state.RUNNING:
                flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
            virt_dom.updateDeviceFlags(cfg.to_xml(), flags)
        except libvirt.libvirtError as ex:
            error_code = ex.get_error_code()
            if error_code == libvirt.VIR_ERR_NO_DOMAIN:
                LOG.info(_LI("During set_interface_bandwidth, "
                             "instance disappeared."), instance=instance)
            else:
                LOG.error(_('setting network interface bandwidth '
                            'failed.'), instance=instance)
            raise exception.SetInterfaceBandwidthFailed(instance)

    def _try_fetch_image_cache(self, image, fetch_func, context, filename,
                               image_id, instance, size,
                               fallback_from_host=None):
        try:
            image.cache(fetch_func=fetch_func,
                        context=context,
                        filename=filename,
                        image_id=image_id,
                        user_id=instance.user_id,
                        project_id=instance.project_id,
                        size=size)
        except exception.ImageNotFound:
            if not fallback_from_host:
                raise
            LOG.debug("Image %(image_id)s doesn't exist anymore "
                      "on image service, attempting to copy "
                      "image from %(host)s",
                      {'image_id': image_id, 'host': fallback_from_host},
                      instance=instance)

            def copy_from_host(target, max_size):
                libvirt_utils.copy_image(src=target,
                                         dest=target,
                                         host=fallback_from_host,
                                         receive=True)
            image.cache(fetch_func=copy_from_host,
                        filename=filename)

    @staticmethod
    def _get_rbd_driver():
        return rbd_utils.RBDDriver(
                pool=CONF.libvirt.images_rbd_pool,
                ceph_conf=CONF.libvirt.images_rbd_ceph_conf,
                rbd_user=CONF.libvirt.rbd_user)

    def image_rollback(self, context, instance, image_meta):
        LOG.info(_LI('Image_rollback from the given image:%(image)s '
                     'and replace the old.'),
                 {'image': image_meta['id']}, instance=instance)

        LOG.info(_LI('Image_rollback from the given image:%(image)s '
                     'and replace the old.'),
                 {'image': image_meta['id']}, instance=instance)

        suffix = ''
        image_type = CONF.libvirt.images_type

        if image_type == 'default':
            disk_path = libvirt_utils.get_instance_path(instance)
            disk_file_bak = os.path.join(disk_path, 'disk_bak')
            disk_file = os.path.join(disk_path, 'disk')

        disk_images = {'image_id': image_meta['id'],
                       'kernel_id': instance.kernel_id,
                       'ramdisk_id': instance.ramdisk_id}

        try:
            if image_type == 'default':
                # backup old disk file
                if os.path.exists(disk_file_bak):
                    libvirt_utils.file_delete(disk_file_bak)
                if os.path.exists(disk_file):
                    libvirt_utils.file_rename(disk_file, disk_file_bak)
            else:
                rbd_utils = self._get_rbd_driver()
                instance_disk = instance.uuid + "_disk"
                if rbd_utils.exists(instance_disk):
                    rbd_utils.remove_image(instance_disk)

            # NOTE(ndipanov): Even if disk_mapping was passed in, which
            # currently happens only on rescue - we still don't want to
            # create a base image.
            root_fname = imagecache.get_cache_fname(disk_images, 'image_id')
            size = instance.root_gb * units.Gi

            backend = self.image_backend.image(instance, 'disk' + suffix,
                                               image_type)
            if backend.SUPPORTS_CLONE:
                def clone_fallback_to_fetch(*args, **kwargs):
                    try:
                        backend.clone(context, disk_images['image_id'])
                    except exception.ImageUnacceptable:
                        libvirt_utils.fetch_image(*args, **kwargs)
                fetch_func = clone_fallback_to_fetch
            else:
                fetch_func = libvirt_utils.fetch_image
            self._try_fetch_image_cache(backend, fetch_func, context,
                                        root_fname, disk_images['image_id'],
                                        instance, size,
                                        fallback_from_host=None)

            if image_type == 'default':
                libvirt_utils.chown(disk_file, 'qemu:qemu')
        except Exception:
            instance.task_state = None
            instance.save()
            if image_type == 'default':
                if os.path.exists(disk_file_bak):
                    libvirt_utils.file_rename(disk_file_bak, disk_file)
            msg = _('Instance:%s image_rollback error') % instance.uuid
            raise exception.NovaException(msg)

        LOG.info(_LI('Image_rollback sucessfully'), instance=instance)
