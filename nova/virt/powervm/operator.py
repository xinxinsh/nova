# Copyright.
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

import decimal
import random
import re
import six
import time
import uuid

from eventlet import timeout as eventlet_timeout
from oslo_concurrency import lockutils
from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_log import log as logging
from oslo_service import loopingcall
from oslo_utils import excutils
from oslo_utils import timeutils

from nova.api.metadata import base as instance_metadata
from nova import exception as n_exc
from nova.i18n import _LI, _LW, _LE
from nova.compute import power_state
from nova.virt import configdrive
from nova.virt import hardware
from nova.virt.powervm import blockdev
from nova.virt.powervm import command
from nova.virt.powervm import common
from nova.virt.powervm import constants
from nova.virt.powervm import exception
from nova.virt.powervm.gettextutils import _
from nova.virt.powervm import lpar as LPAR

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


def get_powervm_operator():
    if CONF.powervm.powervm_mgr_type == 'ivm':
        return IVMOperator(common.Connection(CONF.powervm.powervm_mgr,
                                             CONF.powervm.powervm_mgr_user,
                                             CONF.powervm.powervm_mgr_passwd))
    if CONF.powervm.powervm_mgr_type == 'hmc':
        return HMCOperator(common.Connection(CONF.powervm.powervm_mgr,
                                             CONF.powervm.powervm_mgr_user,
                                             CONF.powervm.powervm_mgr_passwd))


def get_powervm_disk_adapter():
    return blockdev.PowerVMLocalVolumeAdapter(
            common.Connection(CONF.powervm.powervm_mgr,
                              CONF.powervm.powervm_mgr_user,
                              CONF.powervm.powervm_mgr_passwd))


def get_vios_disk_adapter(ip, user, password):
    return blockdev.PowerVMLocalVolumeAdapter(
            common.Connection(ip, user, password))


class PowerVMOperator(object):
    """PowerVM main operator.

    The PowerVMOperator is intended to wrap all operations
    from the driver and handle either IVM or HMC managed systems.
    """

    def __init__(self):
        self._operator = get_powervm_operator()
        self._disk_adapter = get_powervm_disk_adapter()
        self._host_stats = {}
        self._update_host_stats()

    def get_info(self, instance):
        """Get the current status of an LPAR instance.

        Returns a dict containing:

        :state:           the running state, one of the power_state codes
        :max_mem:         (int) the maximum memory in KBytes allowed
        :mem:             (int) the memory in KBytes used by the domain
        :num_cpu:         (int) the number of virtual CPUs for the domain
        :cpu_time:        (int) the CPU time used in nanoseconds

        :raises: PowerVMLPARInstanceNotFound
        """
        try:
            lpar_instance = self._get_instance(instance['display_name'])
        except exception.PowerVMLPARInstanceNotFound:
            lpar_instance = self._get_instance(instance['display_name'])

        state = constants.POWERVM_POWER_STATE.get(
                lpar_instance['state'], power_state.NOSTATE)
        return hardware.InstanceInfo(
            state=state,
            max_mem_kb=lpar_instance['max_mem'],
            mem_kb=lpar_instance['desired_mem'],
            num_cpu=lpar_instance['max_procs'],
            cpu_time_ns=lpar_instance['uptime'])

    def get_disk_size(self, lpar_instance):
        vhost = \
            self._operator.get_vhost_by_instance_id(lpar_instance['lpar_id'])
        if vhost:
            disk_name = self._operator.get_disk_name_by_vhost(vhost)[0]
            disk_size = 0
            try:
                disk_size = self._operator.get_logical_vol_size(disk_name)
            except Exception:
                disk_size = 0
            if disk_size == 0:
                try:
                    disk_size = self._operator.get_hdisk_size(disk_name)
                except Exception:
                    disk_size = 0
        else:
            disk_size = 0
        return disk_size

    def get_instance_flavor_info(self, instance_name, flavor_name, node=None):
        "Get flavor info for instance exists in ivm"
        lpar_instance = self._get_instance(instance_name)
        if lpar_instance['lpar_env'] == 'vioserver':
            return None
        disk_size = self.get_disk_size(lpar_instance)
        flavorid = uuid.uuid4()
        flavorid = six.text_type(flavorid)
        flavor_info = {'root_gb': disk_size, 'name': flavor_name,
                       'ephemeral_gb': 0,
                       'memory_mb': lpar_instance['desired_mem'],
                       'vcpus': lpar_instance['desired_procs'], 'swap': 0,
                       'rxtx_factor': 1.0, 'is_public': True,
                       'flavorid': flavorid}
        return flavor_info

    def get_instance_info(self, instance_name, host, node, flavor):
        lpar_instance = self._get_instance(instance_name)
        disk_size = self.get_disk_size(lpar_instance)
        hypervisor_name = self._operator.get_hostname()
        return self._get_instance_info(lpar_instance, disk_size,
                                       hypervisor_name, host, flavor)

    def _get_instance_info(self, lpar_instance, disk_size,
                           hypervisor_name, host, flavor):
        state = constants.POWERVM_POWER_STATE.get(
                lpar_instance['state'], power_state.NOSTATE)
        if lpar_instance['state'] == constants.POWERVM_RUNNING:
            vm_state = 'active'
        elif lpar_instance['state'] == constants.POWERVM_SHUTDOWN:
            vm_state = 'stopped'
        else:
            vm_state = ''

        if lpar_instance['lpar_env'] == 'vioserver':
            return None
        veth_adapter = lpar_instance['virtual_eth_adapters']
        if veth_adapter != 'none':
            slot_id = int(veth_adapter.split('/')[0])
            if lpar_instance['virtual_eth_mac_base_value']:
                mac = lpar_instance['virtual_eth_mac_base_value'] + \
                    hex(slot_id)[2:]
                mac = re.sub('(..)', r':\1', mac.lower())[1:]
            else:
                is_lower = self._operator.check_power_version(hypervisor_name)
                if is_lower:
                    mac = 'null'
                else:
                    mac = veth_adapter.split('/')[7]
        else:
            mac = 'null'
        network_info = (
            '[{"profile": {}, "preserve_on_delete": false,'
            ' "network": {"bridge": null, "subnets": [{"ips": [{"meta": {},'
            ' "version": 4, "type": "fixed", "floating_ips": [],'
            ' "address": null}], "version": 4, "meta": {}, "dns": [],'
            ' "routes": [], "cidr": null, "gateway": {"meta": {},'
            ' "version": 4, "type": "gateway", "address": null}}],'
            ' "meta": {"injected": false, "mtu": 1450,'
            ' "tenant_id": "%s"}},'
            ' "devname": null, "vnic_type": "normal", "qbh_params": null,'
            ' "meta": {}, "address": "%s", "active": false, "type": "ovs",'
            ' "details": {"port_filter": true, "ovs_hybrid_plug": true},'
            ' "qbg_params": null}]' % (CONF.powervm.powervm_project_id, mac))
        flavor_info = ('{"new": null, "old": null,'
            ' "cur": {"nova_object.version": "1.1",'
            ' "nova_object.name": "Flavor",'
            ' "nova_object.data": {"disabled": false,'
            ' "root_gb": %s, "name": "%s", "flavorid": "%s",'
            ' "deleted": false, "created_at": null,'
            ' "ephemeral_gb": 0, "updated_at": null,'
            ' "memory_mb": %s, "vcpus": %s, "extra_specs": {},'
            ' "swap": 0, "rxtx_factor": 1.0, "is_public": true,'
            ' "deleted_at": null, "vcpu_weight": 0, "id": 2},'
            ' "nova_object.namespace": "nova"}}' %
            (flavor.root_gb, flavor.name, flavor.flavorid,
              flavor.memory_mb, flavor.vcpus))

        inst = {'vm_state': vm_state,
                'host': host,
                'launched_at':
                     timeutils.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                'launched_on': host,
                'node': hypervisor_name,
                'availability_zone': u'nova',
                'instance_type_id': 2,
                'reservation_id': '',
                'security_groups': [],
                'extra': {'vcpu_model': None, 'flavor': flavor_info,
                          'pci_requests': '[]', 'numa_topology': None},
                'user_id': CONF.powervm.powervm_user_id,
                'info_cache': {'network_info': network_info},
                'hostname': lpar_instance['lpar_name'],
                'display_description': lpar_instance['lpar_name'],
                'key_data': None,
                'power_state': state,
                'progress': 0,
                'project_id': CONF.powervm.powervm_project_id,
                'metadata': {},
                'ramdisk_id': u'',
                'access_ip_v6': None,
                'access_ip_v4': None,
                'kernel_id': u'',
                'display_name': lpar_instance['lpar_name'],
                'system_metadata': {'image_disk_format': u'raw',
                        'image_min_ram': u'0',
                        'image_min_disk': u'10',
                        'image_base_image_ref': None,
                        'image_container_format': u'bare'},
                'task_state': None,
                'shutdown_terminate': False,
                'root_gb': disk_size,
                'ephemeral_gb': 0,
                'locked': False,
                'launch_index': 0,
                'memory_mb': lpar_instance['desired_mem'],
                'vcpus': lpar_instance['desired_procs'],
                'image_ref': None,
                'config_drive': u''}
        return inst

    def instance_exists(self, instance_name):
        lpar_instance = self._operator.get_lpar(instance_name)
        return True if lpar_instance else False

    def choose_instance_name(self, instance):
        name = instance['name']
        if self.instance_exists(instance['name']):
            name = instance['name']
        elif self.instance_exists(instance['display_name']):
            name = instance['display_name']
        return name

    def _get_instance(self, instance_name):
        """Check whether or not the LPAR instance exists and return it."""
        lpar_instance = self._operator.get_lpar(instance_name)

        if lpar_instance is None:
            LOG.error(_LE("LPAR instance '%s' not found") % instance_name)
            raise exception.PowerVMLPARInstanceNotFound(
                                                instance_name=instance_name)
        return lpar_instance

    def list_instances(self):
        """Return the names of all the instances known to the virtualization
        layer, as a list.
        """
        lpar_instances = self._operator.list_lpar_instances()
        return lpar_instances

    def get_available_resource(self):
        """Retrieve resource info.

        :returns: dictionary containing resource info
        """
        data = self.get_host_stats()
        # Memory data is in MB already.
        memory_mb_used = data['host_memory_total'] - data['host_memory_free']

        # Convert to GB
        local_gb = data['disk_total'] / 1024
        local_gb_used = data['disk_used'] / 1024

        dic = {'vcpus': data['vcpus'],
               'memory_mb': data['host_memory_total'],
               'local_gb': local_gb,
               'vcpus_used': data['vcpus_used'],
               'memory_mb_used': memory_mb_used,
               'local_gb_used': local_gb_used,
               'hypervisor_type': data['hypervisor_type'],
               'hypervisor_version': data['hypervisor_version'],
               'hypervisor_hostname': self._operator.get_hostname(),
               'cpu_info': ','.join(data['cpu_info']),
               'disk_available_least': data['disk_total'],
               'supported_instances': data['supported_instances']}
        dic['numa_topology'] = None
        return dic

    def get_host_stats(self, refresh=False):
        """Return currently known host stats."""
        if refresh or not self._host_stats:
            self._update_host_stats()
        return self._host_stats

    def _update_host_stats(self):
        memory_info = self._operator.get_memory_info()
        cpu_info = self._operator.get_cpu_info()

        # Note: disk avail information is not accurate. The value
        # is a sum of all Volume Groups and the result cannot
        # represent the real possibility. Example: consider two
        # VGs both 10G, the avail disk will be 20G however,
        # a 15G image does not fit in any VG. This can be improved
        # later on.
        disk_info = self._operator.get_disk_info()

        data = {}
        data['vcpus'] = cpu_info['total_procs']
        data['vcpus_used'] = cpu_info['total_procs'] - cpu_info['avail_procs']
        data['cpu_info'] = constants.POWERVM_CPU_INFO
        data['disk_total'] = disk_info['disk_total']
        data['disk_used'] = disk_info['disk_used']
        data['disk_available'] = disk_info['disk_avail']
        data['host_memory_total'] = memory_info['total_mem']
        data['host_memory_free'] = memory_info['avail_mem']
        data['hypervisor_type'] = constants.POWERVM_HYPERVISOR_TYPE
        data['hypervisor_version'] = constants.POWERVM_HYPERVISOR_VERSION
        data['hypervisor_hostname'] = self._operator.get_hostname()
        data['supported_instances'] = constants.POWERVM_SUPPORTED_INSTANCES
        data['extres'] = ''

        self._host_stats = data

    def get_host_uptime(self, host):
        """Returns the result of calling "uptime" on the target host."""
        return self._operator.get_host_uptime(host)

    def spawn(self, context, instance, image_meta,
              network_info, block_device_info):
        def _create_image(context, instance, image_meta, block_device_info):
            """Fetch image from glance and copy it to the remote system."""
            try:
                if hasattr(image_meta, 'id'):
                    image_id = image_meta.id
                    image_name = image_meta.name
                    if image_meta.disk_format == 'iso':
                        is_iso = True
                    else:
                        is_iso = False
                else:
                    image_id = None
                lpar_id = self._operator.get_lpar(
                    instance['display_name'])['lpar_id']
                vhost = self._operator.get_vhost_by_instance_id(lpar_id)
                if block_device_info is not None:
                    block_device_mapping = \
                        block_device_info.get('block_device_mapping') or []
                if image_id:
                    if is_iso:
                        size_gb = max(instance.root_gb,
                                      constants.POWERVM_MIN_ROOT_GB)
                        size = size_gb * 1024 * 1024 * 1024
                        disk_name = self._disk_adapter.create_volume(size)
                        self._disk_adapter.create_iso_from_isofile(context,
                            instance, image_name, image_id)
                        self._operator.attach_vopt_to_vhost(image_name, vhost)
                        self._operator.attach_disk_to_vhost(disk_name, vhost)
                    else:
                        root_volume = \
                            self._disk_adapter.create_volume_from_image(
                                context, instance, image_id)
                        self._disk_adapter.attach_volume_to_host(root_volume)
                        self._operator.attach_disk_to_vhost(
                            root_volume['device_name'], vhost)
                if block_device_mapping:
                    for disk in block_device_mapping:
                        driver_volume_type = \
                            disk['connection_info']['driver_volume_type']
                        if driver_volume_type == 'iscsi':
                            volume_data = disk['connection_info']['data']
                            self._disk_adapter.discovery_device(volume_data)
                            conn_info = self._get_volume_conn_info(volume_data)
                            dev_info = self.get_devname_by_conn_info(conn_info)
                            pvid = self._operator.get_hdisk_pvid(
                                dev_info['device_name'])
                            if pvid == 'none':
                                self._operator.chdev_hdisk_pvid(
                                    dev_info['device_name'])
                            self._operator.attach_disk_to_vhost(
                                dev_info['device_name'], vhost)
                        else:
                            volume_data = disk['connection_info']['data']
                            volume_name = volume_data['volume_name'][0:15]
                            self._operator.attach_disk_to_vhost(
                                volume_name, vhost)

            except Exception as e:
                LOG.exception(_LE("PowerVM image creation failed: %s") %
                        six.text_type(e))
                raise exception.PowerVMImageCreationFailed()

        spawn_start = time.time()

        try:
            try:
                host_stats = self.get_host_stats(refresh=True)
                lpar_inst = self._create_lpar_instance(instance,
                            network_info, host_stats)
                # TODO(mjfork) capture the error and handle the error when the
                #             MAC prefix already exists on the
                #             system (1 in 2^28)
                self._operator.create_lpar(lpar_inst)
                LOG.debug("Creating LPAR instance '%s'" %
                          instance['display_name'])
                meta = instance.metadata
                mgr = CONF.powervm.powervm_mgr
                lpar_id = self._operator.get_lpar(lpar_inst['name'])['lpar_id']
                meta['powervm_mgr'] = mgr
                meta['lpar_id'] = lpar_id
                instance.metadata.update(meta)
                instance.save()

            except processutils.ProcessExecutionError:
                LOG.exception(_LE("LPAR instance '%s' creation failed") %
                        instance['display_name'])
                raise exception.PowerVMLPARCreationFailed(
                    instance_name=instance['display_name'])

            _create_image(context, instance, image_meta, block_device_info)
            LOG.debug("Activating the LPAR instance '%s'"
                      % instance['display_name'])
            self._operator.start_lpar(instance['display_name'])

            # TODO(mrodden): probably do this a better way
            #                that actually relies on the time module
            #                and nonblocking threading
            # Wait for boot
            timeout_count = range(10)
            while timeout_count:
                state = self.get_info(instance).state
                if state == power_state.RUNNING:
                    LOG.info(_LI("Instance spawned successfully."),
                             instance=instance)
                    break
                timeout_count.pop()
                if len(timeout_count) == 0:
                    LOG.error(_("Instance '%s' failed to boot") %
                              instance['display_name'])
                    self._cleanup(instance['display_name'])
                    break
                time.sleep(1)

        except exception.PowerVMImageCreationFailed:
            with excutils.save_and_reraise_exception():
                # log errors in cleanup
                try:
                    self._cleanup(instance['display_name'])
                except Exception:
                    LOG.exception(_LE('Error while attempting to '
                                    'clean up failed instance launch.'))

        spawn_time = time.time() - spawn_start
        LOG.info(_LI("Instance spawned in %s seconds") % spawn_time,
                 instance=instance)

    def destroy(self, instance_name, block_device_info=None,
                destroy_disks=True):
        """Destroy (shutdown and delete) the specified instance.

        :param instance_name: Instance name.
        """
        try:
            self._cleanup(instance_name, block_device_info, destroy_disks)
        except exception.PowerVMLPARInstanceNotFound:
            LOG.warn(_LW("During destroy, LPAR instance '%s' was not found on "
                       "PowerVM system.") % instance_name)

    def capture_image(self, context, instance, image_id, image_meta,
                      update_task_state):
        """Capture the root disk for a snapshot

        :param context: nova context for this operation
        :param instance: instance information to capture the image from
        :param image_id: uuid of pre-created snapshot image
        :param image_meta: metadata to upload with captured image
        :param update_task_state: Function reference that allows for updates
                                  to the instance task state.
        """
        instance_name = self.choose_instance_name(instance)
        lpar = self._operator.get_lpar(instance_name)
        previous_state = lpar['state']

        # stop the instance if it is running
        if previous_state == 'Running':
            LOG.debug("Stopping instance %s for snapshot." %
                      instance['display_name'])
            # wait up to 2 minutes for shutdown
            self.power_off(instance_name, timeout=120)

        # get disk_name
        vhost = self._operator.get_vhost_by_instance_id(lpar['lpar_id'])
        disk_name = self._operator.get_disk_name_by_vhost(vhost)[0]

        # do capture and upload
        self._disk_adapter.create_image_from_volume(
                disk_name, context, image_id, image_meta, update_task_state)

        # restart instance if it was running before
        if previous_state == 'Running':
            self.power_on(instance_name)

    def _cleanup(self, instance_name, block_device_info=None,
                 destroy_disks=True):
        lpar_id = self._get_instance(instance_name)['lpar_id']
        try:
            vhost = self._operator.get_vhost_by_instance_id(lpar_id)
            disk_name = self._operator.get_disk_name_by_vhost(vhost)[0]
            vtopt_names = self._operator.get_vtopt_name_by_vhost(vhost)

            LOG.debug("Shutting down the instance '%s'" % instance_name)
            self._operator.stop_lpar(instance_name)

            volume_names = []
            if block_device_info is not None:
                block_device_mapping = \
                    block_device_info.get('block_device_mapping') or []
            else:
                block_device_mapping = []
            if block_device_mapping:
                for disk in block_device_mapping:
                    driver_volume_type = \
                        disk['connection_info']['driver_volume_type']
                    volume_data = disk['connection_info']['data']
                    volume_name = 'volume-' + volume_data['volume_id']
                    if driver_volume_type == 'iscsi':
                        self._disk_adapter.cancel_discovery_device(
                            volume_name[0:15])
                        conn_info = self._get_volume_conn_info(volume_data)
                        volume_info = self.get_devname_by_conn_info(conn_info)
                    elif driver_volume_type == 'lvmpower':
                        volume_info = {'device_name': volume_name[0:15]}
                    volume_names.append(volume_info['device_name'])
                    self._disk_adapter.detach_volume_from_vhost(volume_info)

            # dperaza: LPAR should be deleted first so that vhost is
            # cleanly removed and detached from disk device.
            LOG.debug("Deleting the LPAR instance '%s'" % instance_name)
            self._operator.remove_lpar(instance_name)

            if vtopt_names:
                for vtopt in vtopt_names:
                    self._operator.remove_vtd(vtopt)

            if disk_name and disk_name not in volume_names and destroy_disks:
                # TODO(mrodden): we should also detach from the instance
                # before we start deleting things...
                volume_info = {'device_name': disk_name}
                # Volume info dictionary might need more info that is lost when
                # volume is detached from host so that it can be deleted
                self._disk_adapter.detach_volume_from_host(volume_info)
                self._disk_adapter.delete_volume(volume_info)
        except Exception:
            LOG.exception(_LE("PowerVM instance cleanup failed"))
            raise exception.PowerVMLPARInstanceCleanupFailed(
                                                  instance_name=instance_name)

    def power_off(self, instance_name,
                  timeout=constants.POWERVM_LPAR_OPERATION_TIMEOUT):
        self._operator.stop_lpar(instance_name, timeout)

    def power_on(self, instance_name):
        self._operator.start_lpar(instance_name)

    def macs_for_instance(self, instance):
        return self._operator.macs_for_instance(instance)

    def _create_lpar_instance(self, instance, network_info, host_stats=None):
        # inst_name = instance['name']
        # Use display_name instead of name
        inst_name = instance['display_name']

        # CPU/Memory min and max can be configurable. Lets assume
        # some default values for now.

        # Memory
        mem = instance['memory_mb']
        if host_stats and mem > host_stats['host_memory_free']:
            LOG.error(_('Not enough free memory in the host'))
            raise exception.PowerVMInsufficientFreeMemory(
                                    instance_name=instance['display_name'])
        mem_min = min(mem, constants.POWERVM_MIN_MEM)
        mem_max = mem + constants.POWERVM_MAX_MEM

        # CPU
        cpus = instance['vcpus']
        if host_stats:
            avail_cpus = host_stats['vcpus'] - host_stats['vcpus_used']
            if cpus > avail_cpus:
                LOG.error(_('Insufficient available CPU on PowerVM'))
                raise exception.PowerVMInsufficientCPU(
                                    instance_name=instance['display_name'])
        cpus_min = min(cpus, constants.POWERVM_MIN_CPUS)
        cpus_max = cpus + constants.POWERVM_MAX_CPUS
        cpus_units_min = decimal.Decimal(cpus_min) / decimal.Decimal(10)
        cpus_units = decimal.Decimal(cpus) / decimal.Decimal(10)

        # Network
        # To ensure the MAC address on the guest matches the
        # generated value, pull the first 10 characters off the
        # MAC address for the mac_base_value parameter and then
        # get the integer value of the final 2 characters as the
        # slot_id parameter
        virtual_eth_adapters = ""
        for vif in network_info:
            mac = vif['address']
            mac_base_value = (mac[:-2]).replace(':', '')
            slot_id = int(mac[-2:], 16)
            network_type = vif['network'].get_meta('network_type', None)
            if network_type == 'vlan':
                eth_id = vif['network'].get_meta('sg_id')
            else:
                eth_id = self._operator.get_virtual_eth_adapter_id()
            if virtual_eth_adapters:
                virtual_eth_adapters = ('\\"%(virtual_eth_adapters)s, \
                                %(slot_id)s/0/%(eth_id)s//0/0\\"' %
                                {'virtual_eth_adapters': virtual_eth_adapters,
                                'slot_id': slot_id, 'eth_id': eth_id})
            else:
                virtual_eth_adapters = ('%(slot_id)s/0/%(eth_id)s//0/0' %
                                {'slot_id': slot_id, 'eth_id': eth_id})
        # LPAR configuration data
        # max_virtual_slots is hardcoded to 64 since we generate a MAC
        # address that must be placed in slots 32 - 64
        lpar_inst = LPAR.LPAR(
                        name=inst_name, lpar_env='aixlinux',
                        min_mem=mem_min, desired_mem=mem,
                        max_mem=mem_max, proc_mode='shared',
                        sharing_mode='uncap', min_procs=cpus_min,
                        desired_procs=cpus, max_procs=cpus_max,
                        min_proc_units=cpus_units_min,
                        desired_proc_units=cpus_units,
                        max_proc_units=cpus_max,
                        virtual_eth_mac_base_value=mac_base_value,
                        max_virtual_slots=64,
                        virtual_eth_adapters=virtual_eth_adapters)
        return lpar_inst

    def _check_host_resources(self, instance, vcpus, mem, host_stats):
        """Checks resources on host for resize, migrate, and spawn
        :param vcpus: CPUs to be used
        :param mem: memory requested by instance
        :param disk: size of disk to be expanded or created
        """
        if mem > host_stats['host_memory_free']:
            LOG.exception(_LE('Not enough free memory in the host'))
            raise exception.PowerVMInsufficientFreeMemory(
                                    instance_name=instance['display_name'])

        avail_cpus = host_stats['vcpus'] - host_stats['vcpus_used']
        if vcpus > avail_cpus:
            LOG.exception(_LE('Insufficient available CPU on PowerVM'))
            raise exception.PowerVMInsufficientCPU(
                                    instance_name=instance['display_name'])

    def migrate_disk(self, device_name, src_host, dest, image_path,
            instance_name=None):
        """Migrates SVC or Logical Volume based disks

        :param device_name: disk device name in /dev/
        :param dest: IP or DNS name of destination host/VIOS
        :param image_path: path on source and destination to directory
                           for storing image files
        :param instance_name: name of instance being migrated
        :returns: disk_info dictionary object describing root volume
                  information used for locating/mounting the volume
        """
        dest_file_path = self._disk_adapter.migrate_volume(
                device_name, src_host, dest, image_path, instance_name)
        disk_info = {}
        disk_info['root_disk_file'] = dest_file_path
        return disk_info

    def deploy_from_migrated_file(self, lpar, file_path, size,
                                  power_on=True):
        """Deploy the logical volume and attach to new lpar.

        :param lpar: lar instance
        :param file_path: logical volume path
        :param size: new size of the logical volume
        """
        need_decompress = file_path.endswith('.gz')

        try:
            # deploy lpar from file
            self._deploy_from_vios_file(lpar, file_path, size,
                                        decompress=need_decompress,
                                        power_on=power_on)
        finally:
            # cleanup migrated file
            self._operator._remove_file(file_path)

    def _deploy_from_vios_file(self, lpar, file_path, size,
                               decompress=True, power_on=True):
        self._operator.create_lpar(lpar)
        lpar = self._operator.get_lpar(lpar['name'])
        instance_id = lpar['lpar_id']
        vhost = self._operator.get_vhost_by_instance_id(instance_id)

        # Create logical volume on IVM
        diskName = self._disk_adapter._create_logical_volume(size)
        # Attach the disk to LPAR
        self._operator.attach_disk_to_vhost(diskName, vhost)

        # Copy file to device
        self._disk_adapter._copy_file_to_device(file_path, diskName,
                                                decompress)

        if power_on:
            self._operator.start_lpar(lpar['name'])

    def _get_volume_conn_info(self, volume_data):
        iqn = None
        lun = None
        conn_info = []
        if not volume_data:
            return None
        try:
            if 'target_iqn' in volume_data:
                iqn = volume_data['target_iqn']
            if 'target_lun' in volume_data:
                lun = hex(volume_data['target_lun'])
            if iqn and lun is not None:
                conn_info.append((iqn, lun))
            else:
                return None

        except Exception as e:
            LOG.error(_("Failed to convert volume_data to conn: %s") % e)
            return None
        return conn_info

    def get_devname_by_conn_info(self, conn_info):
        dev_info = {}
        disk = []
        outputs = []
        hdisks = self._operator.get_all_hdisk()
        if conn_info:
            for iqn, lun in conn_info:
                output = self._operator.get_hdisk_by_iqn(hdisks, iqn, lun)
                if len(output) == 0:
                    continue
                for o in output:
                    devname = o['name']

                    if disk.count(devname) == 0:
                        disk.append(devname)

        if len(disk) == 1:
            dev_info = {'device_name': disk[0]}
        elif len(disk) > 1:
            raise exception.\
                PowerVMInvalidLUNPathInfoMultiple(conn_info, outputs)
        else:
            raise exception.\
                PowerVMInvalidLUNPathInfoNone(conn_info, outputs)

        return dev_info

    def get_volume_connector(self, instance):
        """Return volume connector information."""
        initiator = self._disk_adapter.get_initiator_name()
        connector = {'ip': CONF.powervm.powervm_mgr,
                     'initiator': initiator,
                     'host': CONF.powervm.powervm_mgr}
        connector['instance'] = instance['display_name']
        return connector

    def attach_volume(self, connection_info, instance):
        volume_data = connection_info['data']
        driver_type = connection_info.get('driver_volume_type')
        volume_name = 'volume-' + volume_data['volume_id']
        lpar_id = self._operator.get_lpar(instance['display_name'])['lpar_id']
        vhost = self._operator.get_vhost_by_instance_id(lpar_id)
        if driver_type == 'lvmpower':
            self._operator.attach_disk_to_vhost(
                            volume_name[0:15], vhost)
        elif driver_type == 'iscsi':
            self._disk_adapter.discovery_device(volume_data)
            conn_info = self._get_volume_conn_info(volume_data)
            dev_info = self.get_devname_by_conn_info(conn_info)
            pvid = self._operator.get_hdisk_pvid(dev_info['device_name'])
            if pvid == 'none':
                self._operator.chdev_hdisk_pvid(dev_info['device_name'])
            self._operator.attach_disk_to_vhost(dev_info['device_name'], vhost)

    def detach_volume(self, connection_info, instance):
        driver_type = connection_info.get('driver_volume_type')
        volume_data = connection_info['data']
        volume_name = 'volume-' + volume_data['volume_id']
        if driver_type == 'iscsi':
            self._disk_adapter.cancel_discovery_device(volume_name[0:15])
            conn_info = self._get_volume_conn_info(volume_data)
            volume_info = self.get_devname_by_conn_info(conn_info)
        elif driver_type == 'lvmpower':
            volume_info = {'device_name': volume_name[0:15]}
        self._disk_adapter.detach_volume_from_vhost(volume_info)

    def change_iso(self, context, instance, iso_meta):
        lpar_id = self._operator.get_lpar(
            instance['display_name'])['lpar_id']
        vhost = self._operator.get_vhost_by_instance_id(lpar_id)
        if iso_meta:
            image_id = iso_meta.get('id')
            image_name = iso_meta.get('name')
            self._disk_adapter.create_iso_from_isofile(context,
                instance, image_name, image_id)
            self._operator.attach_vopt_to_vhost(image_name, vhost)
        else:
            vtopt_names = self._operator.get_vtopt_name_by_vhost(vhost)
            if vtopt_names:
                self._operator.remove_vtd(vtopt_names[-1])

    def list_phy_cdroms(self, instance):
        phy_cdroms = self._operator.list_phy_cdroms()
        return phy_cdroms

    def attach_phy_cdrom(self, instance, cdrom):
        lpar_id = self._operator.get_lpar(
            instance['display_name'])['lpar_id']
        vhost = self._operator.get_vhost_by_instance_id(lpar_id)
        self._operator.attach_disk_to_vhost(cdrom, vhost)

    def detach_phy_cdrom(self, instance, cdrom):
        lpar_id = self._operator.get_lpar(
            instance['display_name'])['lpar_id']
        vhost = self._operator.get_vhost_by_instance_id(lpar_id)
        cdrom_mapping = self._operator.get_cdrom_mapping_by_vhost(vhost)
        if cdrom in cdrom_mapping:
            self._operator.remove_vtd(cdrom_mapping[cdrom])


class BaseOperator(object):
    """Base operator for IVM and HMC managed systems."""

    def __init__(self, connection):
        """Constructor.

        :param connection: common.Connection object with the
                           information to connect to the remote
                           ssh.
        """
        self._connection = None
        self.connection_data = connection

    def _set_connection(self):
        # create a new connection or verify an existing connection
        # and re-establish if the existing connection is dead
        self._connection = common.check_connection(self._connection,
                                                   self.connection_data)

    def _poll_for_lpar_status(self, instance_name, status, operation,
                            timeout=constants.POWERVM_LPAR_OPERATION_TIMEOUT):
        """Polls until the LPAR with the given name reaches the given status.

        :param instance_name: LPAR instance name
        :param status: Poll until the given LPAR status is reached
        :param operation: The operation being performed, e.g. 'stop_lpar'
        :param timeout: The number of seconds to wait.
        :raises: PowerVMLPARInstanceNotFound
        :raises: PowerVMLPAROperationTimeout
        :raises: InvalidParameterValue
        """
        # make sure it's a valid status
        if (status == constants.POWERVM_NOSTATE or
                status not in constants.POWERVM_POWER_STATE):
            msg = _("Invalid LPAR state: %s") % status
            raise n_exc.InvalidParameterValue(err=msg)

        # raise the given timeout exception if the loop call doesn't complete
        # in the specified timeout
        timeout_exception = exception.PowerVMLPAROperationTimeout(
                                                operation=operation,
                                                instance_name=instance_name)
        with eventlet_timeout.Timeout(timeout, timeout_exception):
            def _wait_for_lpar_status(instance_name, status):
                """Called at an interval until the status is reached."""
                lpar_obj = self.get_lpar(instance_name)
                if lpar_obj['state'] == status:
                    raise loopingcall.LoopingCallDone()

            timer = loopingcall.FixedIntervalLoopingCall(_wait_for_lpar_status,
                                                         instance_name, status)
            timer.start(interval=1).wait()

    def get_lpar(self, instance_name, resource_type='lpar'):
        """Return a LPAR object by its instance name.

        :param instance_name: LPAR instance name
        :param resource_type: the type of resources to list
        :returns: LPAR object
        """
        cmd = self.command.lssyscfg('-r %s --filter "lpar_names=%s"'
                                    % (resource_type, instance_name))
        output = self.run_vios_command(cmd)
        if not output:
            return None
        cmd1 = self.command.lssyscfg('-r prof --filter "lpar_names=%s"'
                                    % instance_name)
        output1 = self.run_vios_command(cmd1)
        confdata = output[0] + ',' + output1[0]
        lpar = LPAR.load_from_conf_data(confdata)
        return lpar

    def list_lpar_instances(self):
        """List all existent LPAR instances names.

        :returns: list -- list with instances names.
        """
        lpar_names = self.run_vios_command(self.command.lssyscfg(
                    '-r lpar -F name'))
        if not lpar_names:
            return []
        return lpar_names

    def create_lpar(self, lpar):
        """Receives a LPAR data object and creates a LPAR instance.

        :param lpar: LPAR object
        """
        conf_data = lpar.to_string()
        self.run_vios_command(self.command.mksyscfg('-r lpar -i "%s"' %
                                                    conf_data))

    def start_lpar(self, instance_name,
                   timeout=constants.POWERVM_LPAR_OPERATION_TIMEOUT):
        """Start a LPAR instance.

        :param instance_name: LPAR instance name
        :param timeout: value in seconds for specifying
                        how long to wait for the LPAR to start
        """
        self.run_vios_command(self.command.chsysstate('-r lpar -o on -n %s'
                                                 % instance_name))
        # poll instance until running or raise exception
        self._poll_for_lpar_status(instance_name, constants.POWERVM_RUNNING,
                                   'start_lpar', timeout)

    def stop_lpar(self, instance_name,
                  timeout=constants.POWERVM_LPAR_OPERATION_TIMEOUT):
        """Stop a running LPAR.

        :param instance_name: LPAR instance name
        :param timeout: value in seconds for specifying
                        how long to wait for the LPAR to stop
        """
        cmd = self.command.chsysstate('-r lpar -o shutdown --immed -n %s' %
                                      instance_name)
        self.run_vios_command(cmd)

        # poll instance until stopped or raise exception
        self._poll_for_lpar_status(instance_name, constants.POWERVM_SHUTDOWN,
                                   'stop_lpar', timeout)

    def remove_lpar(self, instance_name):
        """Removes a LPAR.

        :param instance_name: LPAR instance name
        """
        self.run_vios_command(self.command.rmsyscfg('-r lpar -n %s'
                                               % instance_name))

    def get_vhost_by_instance_id(self, instance_id):
        """Return the vhost name by the instance id.

        :param instance_id: LPAR instance id
        :returns: string -- vhost name or None in case none is found
        """
        instance_hex_id = '%#010x' % int(instance_id)
        cmd = self.command.lsmap('-all -field clientid svsa -fmt :')
        output = self.run_vios_command(cmd)
        vhosts = dict(item.split(':') for item in list(output))

        if instance_hex_id in vhosts:
            return vhosts[instance_hex_id]

        return None

    def get_virtual_eth_adapter_id(self):
        """Virtual ethernet adapter id.

        Searches for the shared ethernet adapter and returns
        its id.

        :returns: id of the virtual ethernet adapter.
        """
        cmd = self.command.lsmap('-all -net -field sea -fmt :')
        output = self.run_vios_command(cmd)
        sea = output[0]
        cmd = self.command.lsdev('-dev %s -attr pvid' % sea)
        output = self.run_vios_command(cmd)
        # Returned output looks like this: ['value', '', '1']
        if output:
            return output[2]

        return None

    def get_hostname(self):
        """Returns the managed system hostname.

        :returns: string -- hostname
        """
        output = self.run_vios_command(self.command.hostname())
        hostname = output[0]
        if not hasattr(self, '_hostname'):
            self._hostname = hostname
        elif hostname != self._hostname:
            LOG.error(_('Hostname has changed from %(old)s to %(new)s. '
                        'A restart is required to take effect.'
                        ) % {'old': self._hostname, 'new': hostname})
        return self._hostname

    def get_disk_name_by_vhost(self, vhost):
        """Return the disk names attached to a specific vhost

        :param vhost: vhost name
        :returns: list of disk names attached to the vhost,
                  ordered by time of attachment
                  (first disk should be boot disk)
                  Returns None if no disks are found.
        """
        command = self.command.lsmap(
                '-vadapter %s -type disk lv -field LUN backing -fmt :' % vhost)
        output = self.run_vios_command(command)
        if output:
            # assemble dictionary of [ lun => diskname ]
            lun_to_disk = {}
            remaining_str = output[0].strip()
            while remaining_str:
                values = remaining_str.split(':', 2)
                lun_to_disk[values[0]] = values[1]
                if len(values) > 2:
                    remaining_str = values[2]
                else:
                    remaining_str = ''

            lun_numbers = lun_to_disk.keys()
            lun_numbers.sort()

            # assemble list of disknames ordered by lun number
            disk_names = []
            for lun_num in lun_numbers:
                disk_names.append(lun_to_disk[lun_num])

            return disk_names

        return [None]

    def remove_vtd(self, vtopt):
        cmd = self.command.rmvdev('-vtd %s' % vtopt)
        self.run_vios_command(cmd)

    def get_vtopt_name_by_vhost(self, vhost):
        command = self.command.lsmap(
                '-vadapter %s -type file_opt -field LUN vtd -fmt :' % vhost)
        output = self.run_vios_command(command)
        if output:
            lun_to_vtd = {}
            remaining_str = output[0].strip()
            while remaining_str:
                values = remaining_str.split(':', 2)
                lun_to_vtd[values[0]] = values[1]
                if len(values) > 2:
                    remaining_str = values[2]
                else:
                    remaining_str = ''

            lun_numbers = lun_to_vtd.keys()
            lun_numbers.sort()

            # assemble list of disknames ordered by lun number
            vtopt_names = []
            for lun_num in lun_numbers:
                vtopt_names.append(lun_to_vtd[lun_num])

            return vtopt_names

        return None

    def get_cdrom_mapping_by_vhost(self, vhost):
        cmd = self.command.lsmap(
            '-vadapter %s -type optical -field backing vtd -fmt ,' % vhost)
        output = self.run_vios_command(cmd)
        cdrom_mapping = dict(item.split(',') for item in list(output))
        return cdrom_mapping

    def attach_disk_to_vhost(self, disk, vhost):
        """Attach disk name to a specific vhost.

        :param disk: the disk name
        :param vhost: the vhost name
        """
        cmd = self.command.mkvdev('-vdev %s -vadapter %s') % (disk, vhost)
        self.run_vios_command(cmd)

    def attach_vopt_to_vhost(self, iso_name, vhost):
        # Create a vopt for vhost
        cmd = self.command.mkvdev('-fbo -vadapter %s' % vhost)
        vopt_info = self.run_vios_command(cmd)[0]
        vopt = vopt_info.split()[0]
        loadopt = ('ioscli loadopt -f -vtd %s -disk %s' % (vopt, iso_name))
        self.run_vios_command(loadopt)

    def get_memory_info(self):
        """Get memory info.

        :returns: tuple - memory info (total_mem, avail_mem)
        """
        cmd = self.command.lshwres(
            '-r mem --level sys -F configurable_sys_mem,curr_avail_sys_mem')
        output = self.run_vios_command(cmd)
        total_mem, avail_mem = output[0].split(',')
        return {'total_mem': int(total_mem),
                'avail_mem': int(avail_mem)}

    def get_host_uptime(self, host):
        """Get host uptime.
        :returns: string - amount of time since last system startup
        """
        # The output of the command is like this:
        # "02:54PM  up 24 days,  5:41, 1 user, load average: 0.06, 0.03, 0.02"
        cmd = self.command.sysstat('-short %s' % self.connection_data.username)
        return self.run_vios_command(cmd)[0]

    def get_cpu_info(self):
        """Get CPU info.

        :returns: tuple - cpu info (total_procs, avail_procs)
        """
        cmd = self.command.lshwres(
            '-r proc --level sys -F '
            'configurable_sys_proc_units,curr_avail_sys_proc_units')
        output = self.run_vios_command(cmd)
        total_procs, avail_procs = output[0].split(',')
        return {'total_procs': float(total_procs) * 10,
                'avail_procs': float(avail_procs) * 10}

    def get_disk_info(self):
        """Get the disk usage information.

        :returns: tuple - disk info (disk_total, disk_used, disk_avail)
        """
        vgs = self.run_vios_command(self.command.lsvg())
        (disk_total, disk_used, disk_avail) = [0, 0, 0]
        for vg in vgs:
            cmd = self.command.lsvg('%s -field totalpps usedpps freepps -fmt :'
                                    % vg)
            output = self.run_vios_command(cmd)
            # Output example:
            # 1271 (10168 megabytes):0 (0 megabytes):1271 (10168 megabytes)
            (d_total, d_used, d_avail) = re.findall(r'(\d+) megabytes',
                                                    output[0])
            disk_total += int(d_total)
            disk_used += int(d_used)
            disk_avail += int(d_avail)

        return {'disk_total': disk_total,
                'disk_used': disk_used,
                'disk_avail': disk_avail}

    def run_vios_command(self, cmd, check_exit_code=True):
        """Run a remote command using an active ssh connection.

        :param command: String with the command to run.
        """
        self._set_connection()
        stdout, stderr = processutils.ssh_execute(
            self._connection, cmd, check_exit_code=check_exit_code)
        error_text = stderr.strip()
        if error_text:
            LOG.warn(_LW("Found error stream for command \"%(cmd)s\": "
                        "%(error_text)s"),
                      {'cmd': cmd, 'error_text': error_text})

        return stdout.strip().splitlines()

    def run_vios_command_as_root(self, command, check_exit_code=True):
        """Run a remote command as root using an active ssh connection.

        :param command: List of commands.
        """
        self._set_connection()
        stdout, stderr = common.ssh_command_as_root(
            self._connection, command, check_exit_code=check_exit_code)

        error_text = stderr.read()
        if error_text:
            LOG.warn(_LW("Found error stream for command \"%(command)s\":"
                        " %(error_text)s"),
                      {'command': command, 'error_text': error_text})

        return stdout.read().splitlines()

    def macs_for_instance(self, instance):
        pass

    def update_lpar(self, lpar_info):
        """Resizing an LPAR

        :param lpar_info: dictionary of LPAR information
        """
        configuration_data = ('name=%s,min_mem=%s,desired_mem=%s,'
                              'max_mem=%s,min_procs=%s,desired_procs=%s,'
                              'max_procs=%s,min_proc_units=%s,'
                              'desired_proc_units=%s,max_proc_units=%s' %
                              (lpar_info['name'], lpar_info['min_mem'],
                               lpar_info['desired_mem'],
                               lpar_info['max_mem'],
                               lpar_info['min_procs'],
                               lpar_info['desired_procs'],
                               lpar_info['max_procs'],
                               lpar_info['min_proc_units'],
                               lpar_info['desired_proc_units'],
                               lpar_info['max_proc_units']))

        self.run_vios_command(self.command.chsyscfg('-r prof -i "%s"' %
                                               configuration_data))

    def get_logical_vol_size(self, diskname):
        """Finds and calculates the logical volume size in GB

        :param diskname: name of the logical volume
        :returns: size of logical volume in GB
        """
        configuration_data = ("ioscli lslv %s -fmt : -field pps ppsize" %
                                diskname)
        output = self.run_vios_command(configuration_data)
        pps, ppsize = output[0].split(':')
        ppsize = re.findall(r'\d+', ppsize)
        ppsize = int(ppsize[0])
        pps = int(pps)
        lv_size = ((pps * ppsize) / 1024)

        return lv_size

    def get_hdisk_size(self, diskname):
        disk_data = ("ioscli lspv -size %s" % diskname)
        output = self.run_vios_command(disk_data)
        size_mb = int(output[0])
        disk_size = size_mb / 1024
        return disk_size

    def rename_lpar(self, instance_name, new_name):
        """Rename LPAR given by instance_name to new_name

        Note: For IVM based deployments, the name is
              limited to 31 characters and will be trimmed
              to meet this requirement

        :param instance_name: name of LPAR to be renamed
        :param new_name: desired new name of LPAR
        :returns: new name of renamed LPAR trimmed to 31 characters
                  if necessary
        """

        # grab first 31 characters of new name
        new_name_trimmed = new_name[:31]

        cmd = ''.join(['chsyscfg -r lpar -i ',
                       '"',
                       'name=%s,' % instance_name,
                       'new_name=%s' % new_name_trimmed,
                       '"'])

        self.run_vios_command(cmd)

        return new_name_trimmed

    def _remove_file(self, file_path):
        """Removes a file on the VIOS partition

        :param file_path: absolute path to file to be removed
        """
        command = 'rm -f %s' % file_path
        self.run_vios_command_as_root(command)

    def set_lpar_mac_base_value(self, instance_name, mac):
        """Set LPAR's property virtual_eth_mac_base_value

        :param instance_name: name of the instance to be set
        :param mac: mac of virtual ethernet
        """
        # NOTE(ldbragst) We only use the base mac value because the last
        # byte is the slot id of the virtual NIC, which doesn't change.
        mac_base_value = mac[:-2].replace(':', '')
        cmd = ' '.join(['chsyscfg -r lpar -i',
                        '"name=%s,' % instance_name,
                        'virtual_eth_mac_base_value=%s"' % mac_base_value])
        self.run_vios_command(cmd)

    def get_lparname_by_lparid(self, lpar_id):
        cmd = self.command.lssyscfg('-r lpar --filter "lpar_ids=%s" -F name'
                                    % lpar_id)
        lpar_name = self.run_vios_command(cmd)[0]
        return lpar_name

    def list_phy_cdroms(self):
        cdroms_list = []
        cmd = self.command.lsmap('-type optical -all -field '
                                 'backing clientid -fmt ,')
        output = self.run_vios_command(cmd)
        used_cdroms = {}
        for item in list(output):
            item_list = item.split(',')
            used_cdroms[item_list[1]] = item_list[0]

        cmd = self.command.lsdev('-field name description physloc -fmt ,'
                                 ' -type optical -state available')
        cdroms = self.run_vios_command(cmd)
        if cdroms:
            for cdrom in cdroms:
                cdrom_dict = {}
                cdrom_info = cdrom.split(',')
                cdrom_dict['host'] = self.get_hostname()
                cdrom_dict['name'] = cdrom_info[0]
                cdrom_dict['description'] = cdrom_info[1]
                cdrom_dict['physloc'] = cdrom_info[2]
                if cdrom_info[0] in used_cdroms:
                    cdrom_dict['status'] = 'used'
                    lparid = int(used_cdroms[cdrom_info[0]], 16)
                    lpar_name = self.get_lparname_by_lparid(lparid)
                    cdrom_dict['used_by'] = lpar_name
                else:
                    cdrom_dict['status'] = 'available'
                    cdrom_dict['used_by'] = None
                cdroms_list.append(cdrom_dict)
        return cdroms_list

    def get_all_hdisk(self):
        disks = []
        command = self.command.lsdev('-type disk -field name')
        output = self.run_vios_command(command)
        if output:
            for o in output:
                if o.startswith('hdisk'):
                    disks.append(o)
            return disks
        return None

    def get_hdisk_by_iqn(self, hdisks, iqn, lun='0x0'):
        if iqn is None:
            return None
        if hdisks is None:
            return None
        attrs = ['target_name', 'port_num', 'lun_id', 'host_addr', 'pvid']
        hdiskattr = {}
        hdiskattrs = {}
        hdisklist = []
        for hdisk in hdisks:
            cmd = self.command.lsdev('-dev %s -attr target_name' % hdisk)
            try:
                nameoutput = self.run_vios_command(cmd)
            except Exception:
                continue
            cmd = self.command.lsdev('-dev %s -attr lun_id' % hdisk)
            lunoutput = self.run_vios_command(cmd)
            if nameoutput[2] == str(iqn) and lunoutput[2] == str(lun):
                for attr in attrs:
                    cmd = self.command.lsdev('-dev %s -attr %s' %
                        (hdisk, attr))
                    attroutput = self.run_vios_command(cmd)
                    if attroutput is not None:
                        hdiskattr[attr] = attroutput[2]
                hdiskattrs['name'] = hdisk
                hdiskattrs['attr'] = hdiskattr
                hdisklist.append(hdiskattrs)
        return hdisklist

    def get_hdisk_pvid(self, hdisk):
        if hdisk is None:
            return None
        cmd = self.command.lsdev('-dev %s -attr pvid' % hdisk)
        output = self.run_vios_command(cmd)
        if len(output) > 1:
            return output[2]
        return None

    def chdev_hdisk_pvid(self, hdisk, pv=False):
        if hdisk is None:
            return None
        if not pv:
            cmd = ('ioscli chdev -dev %s -attr pv=yes' % hdisk)
        else:
            cmd = ('isocli chdev -dev %s -attr pv=no' % hdisk)
        self.run_vios_command(cmd)


class IVMOperator(BaseOperator):
    """Integrated Virtualization Manager (IVM) Operator.

    Runs specific commands on an IVM managed system.
    """

    def __init__(self, ivm_connection):
        self.command = command.IVMCommand()
        BaseOperator.__init__(self, ivm_connection)

    def macs_for_instance(self, instance):
        """Generates set of valid MAC addresses for an IVM instance."""
        # NOTE(vish): We would prefer to use 0xfe here to ensure that linux
        #             bridge mac addresses don't change, but it appears to
        #             conflict with libvirt, so we use the next highest octet
        #             that has the unicast and locally administered bits set
        #             properly: 0xfa.
        #             Discussion: https://bugs.launchpad.net/nova/+bug/921838
        # NOTE(mjfork): For IVM-based PowerVM, we cannot directly set a MAC
        #               address on an LPAR, but rather need to construct one
        #               that can be used.  Retain the 0xfa as noted above,
        #               but ensure the final 2 hex values represent a value
        #               between 32 and 64 so we can assign as the slot id on
        #               the system. For future reference, the last octect
        #               should not exceed FF (255) since it would spill over
        #               into the higher-order octect.
        #
        #               FA:xx:xx:xx:xx:[32-64]

        macs = set()
        mac_base = [0xfa,
               random.randint(0x00, 0xff),
               random.randint(0x00, 0xff),
               random.randint(0x00, 0xff),
               random.randint(0x00, 0xff),
               random.randint(0x00, 0x00)]
        for n in range(32, 64):
            mac_base[5] = n
            macs.add(':'.join(map(lambda x: "%02x" % x, mac_base)))

        return macs


class PowerHMCOperator(PowerVMOperator):
    def __init__(self):
        self._operator = get_powervm_operator()
        self._host_stats = {}
        self._update_host_stats()
        self._parse_managed_system()

    def _parse_managed_system(self):
        self.managed_systems = self.parse_managed_system(
            CONF.powervm.managed_systems)

    def parse_managed_system(self, managed_systems):
        managed_sys = {}
        for sys in managed_systems:
            managed_system, vios_ip, vios_user, vios_pass = sys.split(':')
            vios_info = (vios_ip, vios_user, vios_pass)
            managed_sys.setdefault(managed_system, []).append(vios_info)
        return managed_sys

    def get_info(self, instance):
        """Get the current status of an LPAR instance.

        Returns a dict containing:

        :state:           the running state, one of the power_state codes
        :max_mem:         (int) the maximum memory in KBytes allowed
        :mem:             (int) the memory in KBytes used by the domain
        :num_cpu:         (int) the number of virtual CPUs for the domain
        :cpu_time:        (int) the CPU time used in nanoseconds

        :raises: PowerVMLPARInstanceNotFound
        """
        instance_name = instance['display_name']
        managed_system = instance['node']
        lpar_instance = self._get_instance(instance_name, managed_system)

        state = constants.POWERVM_POWER_STATE.get(
                lpar_instance['state'], power_state.NOSTATE)
        return hardware.InstanceInfo(
            state=state,
            max_mem_kb=lpar_instance['max_mem'],
            mem_kb=lpar_instance['desired_mem'],
            num_cpu=lpar_instance['max_procs'],
            cpu_time_ns=lpar_instance['uptime'])

    def get_available_resource(self, managed_system):
        data = self._get_one_host_stats(managed_system)
        # Memory data is in MB already.
        memory_mb_used = data['host_memory_total'] - data['host_memory_free']

        # Convert to GB
        local_gb = data['disk_total'] / 1024
        local_gb_used = data['disk_used'] / 1024

        dic = {'vcpus': data['vcpus'],
               'memory_mb': data['host_memory_total'],
               'local_gb': local_gb,
               'vcpus_used': data['vcpus_used'],
               'memory_mb_used': memory_mb_used,
               'local_gb_used': local_gb_used,
               'hypervisor_type': data['hypervisor_type'],
               'hypervisor_version': data['hypervisor_version'],
               'hypervisor_hostname': data['hypervisor_hostname'],
               'cpu_info': ','.join(data['cpu_info']),
               'disk_available_least': data['disk_total'],
               'supported_instances': data['supported_instances']}
        dic['numa_topology'] = None
        return dic

    def get_available_nodes(self, refresh=False):
        return self._operator.get_available_nodes()

    def get_host_stats(self, refresh=False):
        """Return currently known host stats."""
        if refresh or not self._host_stats:
            self._update_host_stats()
        return self._host_stats

    def _get_one_host_stats(self, managed_system):
        vios_lpar_id = self._operator.get_vios_lpar_id(managed_system)
        memory_info = self._operator.get_memory_info(managed_system)
        cpu_info = self._operator.get_cpu_info(managed_system)

        # Note: disk avail information is not accurate. The value
        # is a sum of all Volume Groups and the result cannot
        # represent the real possibility. Example: consider two
        # VGs both 10G, the avail disk will be 20G however,
        # a 15G image does not fit in any VG. This can be improved
        # later on.
        disk_info = self._operator.get_disk_info(managed_system,
                                                 vios_lpar_id)

        data = {}
        data['vcpus'] = cpu_info['total_procs']
        data['vcpus_used'] = cpu_info['total_procs'] - cpu_info['avail_procs']
        data['cpu_info'] = constants.POWERVM_CPU_INFO
        data['disk_total'] = disk_info['disk_total']
        data['disk_used'] = disk_info['disk_used']
        data['disk_available'] = disk_info['disk_avail']
        data['host_memory_total'] = memory_info['total_mem']
        data['host_memory_free'] = memory_info['avail_mem']
        data['hypervisor_type'] = constants.POWERVM_HYPERVISOR_TYPE
        data['hypervisor_version'] = constants.POWERVM_HYPERVISOR_VERSION
        data['hypervisor_hostname'] = managed_system
        data['supported_instances'] = constants.POWERVM_SUPPORTED_INSTANCES
        data['extres'] = ''
        return data

    def get_volume_connector(self, instance):
        """Return volume connector information."""
        managed_system = instance['node']
        disk_adapter = self._get_disk_adapter(managed_system)
        initiator = disk_adapter.get_initiator_name()
        connector = {'ip': CONF.powervm.powervm_mgr,
                     'initiator': initiator,
                     'host': CONF.powervm.powervm_mgr}
        connector['instance'] = instance['display_name']
        return connector

    def _update_host_stats(self):
        stats_list = []
        nodes = self.get_available_nodes()
        for node in nodes:
            data = self._get_one_host_stats(node)
            stats_list.append(data)

        self.host_stats = stats_list

    def get_disk_size(self, lpar_instance, managed_system, vios_pid):
        vhost = \
            self._operator.get_vhost_by_instance_id(managed_system, vios_pid,
                                                    lpar_instance['lpar_id'])
        if vhost:
            disk_name = self._operator.get_disk_name_by_vhost(managed_system,
                                                              vios_pid,
                                                              vhost)[0]
            disk_size = 0
            try:
                disk_size = self._operator.get_logical_vol_size(managed_system,
                                                                vios_pid,
                                                                disk_name)
            except Exception:
                disk_size = 0
            if disk_size == 0:
                try:
                    disk_size = self._operator.get_hdisk_size(managed_system,
                                                              vios_pid,
                                                              disk_name)
                except Exception:
                    disk_size = 0
        else:
            disk_size = 0
        return disk_size

    def get_instance_flavor_info(self, instance_name, flavor_name, node=None):
        "Get flavor info for instance exists in hmc managed_system"
        lpar_instance = self._get_instance(instance_name, node)
        vios_pid = self._operator.get_vios_lpar_id(node)
        if lpar_instance['lpar_env'] == 'vioserver':
            return None
        disk_size = self.get_disk_size(lpar_instance, node, vios_pid)
        flavorid = uuid.uuid4()
        flavorid = six.text_type(flavorid)
        flavor_info = {'root_gb': disk_size, 'name': flavor_name,
                       'ephemeral_gb': 0,
                       'memory_mb': lpar_instance['desired_mem'],
                       'vcpus': lpar_instance['desired_procs'], 'swap': 0,
                       'rxtx_factor': 1.0, 'is_public': True,
                       'flavorid': flavorid}
        return flavor_info

    def get_instance_info(self, instance_name, host, node, flavor):
        lpar_instance = self._get_instance(instance_name, node)
        vios_pid = self._operator.get_vios_lpar_id(node)
        disk_size = self.get_disk_size(lpar_instance, node, vios_pid)
        hypervisor_name = node
        return self._get_instance_info(lpar_instance, disk_size,
                                       hypervisor_name, host, flavor)

    def _get_instance(self, instance_name, managed_system):
        """Check whether or not the LPAR instance exists and return it."""
        lpar_instance = self._operator.get_lpar(instance_name,
                                                managed_system)

        if lpar_instance is None:
            LOG.error(_LE("LPAR instance '%s' not found") % instance_name)
            raise exception.PowerVMLPARInstanceNotFound(
                                                instance_name=instance_name)
        return lpar_instance

    def list_instances(self, managed_system):
        """Return the names of all the instances known to the virtualization
        layer, as a list.
        """
        lpar_instances = self._operator.list_lpar_instances(managed_system)
        return lpar_instances

    def _create_lpar_instance(self, instance, network_info, host_stats=None):
        # inst_name = instance['name']
        # Use display_name instead of name
        inst_name = instance['display_name']

        # CPU/Memory min and max can be configurable. Lets assume
        # some default values for now.

        # Memory
        mem = instance['memory_mb']
        if host_stats and mem > host_stats['host_memory_free']:
            LOG.error(_('Not enough free memory in the host'))
            raise exception.PowerVMInsufficientFreeMemory(
                                    instance_name=instance['display_name'])
        mem_min = min(mem, constants.POWERVM_MIN_MEM)
        mem_max = mem + constants.POWERVM_MAX_MEM

        # CPU
        cpus = instance['vcpus']
        if host_stats:
            avail_cpus = host_stats['vcpus'] - host_stats['vcpus_used']
            if cpus > avail_cpus:
                LOG.error(_('Insufficient available CPU on PowerVM'))
                raise exception.PowerVMInsufficientCPU(
                                    instance_name=instance['display_name'])
        cpus_min = min(cpus, constants.POWERVM_MIN_CPUS)
        cpus_max = cpus + constants.POWERVM_MAX_CPUS
        cpus_units_min = decimal.Decimal(cpus_min) / decimal.Decimal(10)
        cpus_units = decimal.Decimal(cpus) / decimal.Decimal(10)

        # Network
        # To ensure the MAC address on the guest matches the
        # generated value, pull the first 10 characters off the
        # MAC address for the mac_base_value parameter and then
        # get the integer value of the final 2 characters as the
        # slot_id parameter
        is_lower = self._operator.check_power_version(instance['node'])
        virtual_eth_adapters = ""
        for vif in network_info:
            mac = vif['address']
            mac_addr = mac.replace(':', '')
            slot_id = int(mac[-2:], 16)
            network_type = vif['network'].get_meta('network_type', None)
            if network_type == 'vlan':
                eth_id = vif['network'].get_meta('sg_id')
            else:
                eth_id = self._operator.get_virtual_eth_adapter_id(instance)
            vswitch = self._operator.get_vswitch_by_vlanid(instance, eth_id)
            if not vswitch:
                vswitch = ''
            if is_lower:
                if virtual_eth_adapters:
                    virtual_eth_adapters = ('\\"%(virtual_eth_adapters)s, \
                                %(slot_id)s/0/%(eth_id)s//0/0\\"' %
                                {'virtual_eth_adapters': virtual_eth_adapters,
                                'slot_id': slot_id, 'eth_id': eth_id})
                else:
                    virtual_eth_adapters = ('%(slot_id)s/0/%(eth_id)s//0/0' %
                                {'slot_id': slot_id, 'eth_id': eth_id})
            else:
                if virtual_eth_adapters:
                    virtual_eth_adapters = ('\\"%(virtual_eth_adapters)s, \
                        %(slotid)s/0/%(eth_id)s//0/0/%(vs)s/%(mac_add)s//\\"' %
                        {'virtual_eth_adapters': virtual_eth_adapters,
                         'slotid': slot_id, 'eth_id': eth_id,
                         'vs': vswitch, 'mac_add': mac_addr})
                else:
                    virtual_eth_adapters = (
                        '%(slot_id)s/0/%(eth_id)s//0/0/%(vs)s/%(mac_addr)s//' %
                        {'slot_id': slot_id, 'eth_id': eth_id,
                         'vs': vswitch, 'mac_addr': mac_addr})
        # LPAR configuration data
        # max_virtual_slots is hardcoded to 64 since we generate a MAC
        # address that must be placed in slots 32 - 64
        lpar_inst = LPAR.LPAR(
                        name=inst_name, lpar_env='aixlinux',
                        min_mem=mem_min, desired_mem=mem,
                        max_mem=mem_max, proc_mode='shared',
                        sharing_mode='uncap', min_procs=cpus_min,
                        uncap_weight=128,
                        desired_procs=cpus, max_procs=cpus_max,
                        min_proc_units=cpus_units_min,
                        desired_proc_units=cpus_units,
                        max_proc_units=cpus_max,
                        max_virtual_slots=64,
                        profile_name=inst_name,
                        virtual_eth_adapters=virtual_eth_adapters)
        return lpar_inst

    def _get_disk_adapter(self, managed_system):
        vios_info = self.managed_systems[managed_system]
        for ip, user, password in vios_info:
            disk_adapter = get_vios_disk_adapter(ip, user, password)
        return disk_adapter

    def spawn(self, context, instance, image_meta,
              network_info, block_device_info):
        def _create_image(context, instance, image_meta, block_device_info):
            """Fetch image from glance and copy it to the remote system."""
            try:
                if hasattr(image_meta, 'id'):
                    image_id = image_meta.id
                    image_name = image_meta.name
                    if image_meta.disk_format == 'iso':
                        is_iso = True
                    else:
                        is_iso = False
                else:
                    image_id = None
                managed_system = instance['node']
                vios_pid = self._operator.get_vios_lpar_id(managed_system)
                disk_adapter = self._get_disk_adapter(managed_system)
                lpar_id = self._operator.get_lpar(instance['display_name'],
                                                  managed_system)['lpar_id']
                vhost = self._operator.get_vhost_by_instance_id(managed_system,
                                                                vios_pid,
                                                                lpar_id)
                if block_device_info is not None:
                    block_device_mapping = \
                        block_device_info.get('block_device_mapping') or []
                if image_id:
                    if is_iso:
                        size_gb = max(instance.root_gb,
                            constants.POWERVM_MIN_ROOT_GB)
                        size = size_gb * 1024 * 1024 * 1024
                        disk_name = disk_adapter.create_volume(size)
                        disk_adapter.create_iso_from_isofile(context,
                            instance, image_name, image_id)
                        self._operator.attach_vopt_to_vhost(managed_system,
                            vios_pid, image_name, vhost)
                        self._operator.attach_disk_to_vhost(managed_system,
                            vios_pid, disk_name, vhost)
                    else:
                        root_volume = disk_adapter.create_volume_from_image(
                            context, instance, image_id)
                        disk_adapter.attach_volume_to_host(root_volume)
                        self._operator.attach_disk_to_vhost(managed_system,
                            vios_pid, root_volume['device_name'], vhost)
                if block_device_mapping:
                    for disk in block_device_mapping:
                        driver_volume_type = \
                            disk['connection_info']['driver_volume_type']
                        if driver_volume_type == 'iscsi':
                            volume_data = disk['connection_info']['data']
                            disk_adapter.discovery_device(volume_data)
                            conn_info = self._get_volume_conn_info(volume_data)
                            dev_info = self.get_devname_by_conn_info(
                                managed_system, vios_pid, conn_info)
                            pvid = self._operator.get_hdisk_pvid(
                                managed_system,
                                vios_pid,
                                dev_info['device_name'])
                            if pvid == 'none':
                                self._operator.chdev_hdisk_pvid(managed_system,
                                                vios_pid,
                                                dev_info['device_name'])
                            self._operator.attach_disk_to_vhost(managed_system,
                                vios_pid, dev_info['device_name'], vhost)
                        else:
                            volume_data = disk['connection_info']['data']
                            volume_name = volume_data['volume_name'][0:15]
                            self._operator.attach_disk_to_vhost(managed_system,
                                vios_pid, volume_name, vhost)

                # Config drive
                if configdrive.required_by(instance):
                    LOG.info(_LI('Using config drive'), instance=instance)
                    extra_md = {}

                    inst_md = instance_metadata.InstanceMetadata(instance,
                        extra_md=extra_md, network_info=network_info)
                    with configdrive.ConfigDriveBuilder(
                            instance_md=inst_md) as cdb:
                        configdrive_path = \
                            disk_adapter.get_disk_config_path(instance.uuid)
                        LOG.info(_LI('Creating config drive at %(path)s'),
                                 {'path': configdrive_path}, instance=instance)
                        try:
                            cdb.make_drive(configdrive_path)
                        except processutils.ProcessExecutionError as e:
                            with excutils.save_and_reraise_exception():
                                LOG.error(_LE('Creating config drive failed '
                                              'with error: %s'),
                                          e, instance=instance)
                    iso_cd = disk_adapter.copy_cd_for_instance(
                        configdrive_path)
                    cd_opt_name = instance.uuid[0:8] + '.disk.config'
                    disk_adapter.create_cd_opt(cd_opt_name, iso_cd)
                    self._operator.attach_vopt_to_vhost(managed_system,
                            vios_pid, cd_opt_name, vhost)

            except Exception as e:
                LOG.exception(_LE("PowerVM image creation failed: %s") %
                        six.text_type(e))
                raise exception.PowerVMImageCreationFailed()

        spawn_start = time.time()

        try:
            try:
                managed_system = instance['node']
                host_stats = self._get_one_host_stats(managed_system)
                lpar_inst = self._create_lpar_instance(instance,
                            network_info, host_stats)
                # TODO(mjfork) capture the error and handle the error when the
                #             MAC prefix already exists on the
                #             system (1 in 2^28)
                self._operator.create_lpar(lpar_inst, managed_system)
                LOG.debug("Creating LPAR instance '%s'" %
                          instance['display_name'])
                meta = instance.metadata
                mgr = CONF.powervm.powervm_mgr
                lpar_id = self._operator.get_lpar(lpar_inst['name'],
                                                  managed_system)['lpar_id']
                meta['powervm_mgr'] = mgr
                meta['managed_system'] = managed_system
                meta['lpar_id'] = lpar_id
                instance.metadata.update(meta)
                instance.save()
            except processutils.ProcessExecutionError:
                LOG.exception(_LE("LPAR instance '%s' creation failed") %
                        instance['display_name'])
                raise exception.PowerVMLPARCreationFailed(
                    instance_name=instance['display_name'])

            _create_image(context, instance, image_meta, block_device_info)
            LOG.debug("Activating the LPAR instance '%s'"
                      % instance['display_name'])
            self._operator.start_lpar(instance['display_name'], managed_system)

            # TODO(mrodden): probably do this a better way
            #                that actually relies on the time module
            #                and nonblocking threading
            # Wait for boot
            timeout_count = range(10)
            while timeout_count:
                state = self.get_info(instance).state
                if state == power_state.RUNNING:
                    LOG.info(_LI("Instance spawned successfully."),
                             instance=instance)
                    break
                timeout_count.pop()
                if len(timeout_count) == 0:
                    LOG.error(_("Instance '%s' failed to boot") %
                              instance['display_name'])
                    self._cleanup(instance['display_name'], managed_system)
                    break
                time.sleep(1)

        except exception.PowerVMImageCreationFailed:
            with excutils.save_and_reraise_exception():
                # log errors in cleanup
                try:
                    self._cleanup(instance['display_name'], managed_system)
                except Exception:
                    LOG.exception(_LE('Error while attempting to '
                                    'clean up failed instance launch.'))

        spawn_time = time.time() - spawn_start
        LOG.info(_LI("Instance spawned in %s seconds") % spawn_time,
                 instance=instance)

    def destroy(self, instance_name, managed_system,
                block_device_info=None, destroy_disks=True):
        """Destroy (shutdown and delete) the specified instance."""
        try:
            self._cleanup(instance_name, managed_system,
                          block_device_info, destroy_disks)
        except exception.PowerVMLPARInstanceNotFound:
            LOG.warn(_LW("During destroy, LPAR instance '%s' was not found on "
                       "PowerVM system.") % instance_name)

    def _cleanup(self, instance_name, managed_system,
                 block_device_info=None, destroy_disks=True):
        vios_pid = self._operator.get_vios_lpar_id(managed_system)
        disk_adapter = self._get_disk_adapter(managed_system)
        lpar = self._operator.get_lpar(instance_name, managed_system)
        if lpar:
            lpar_id = lpar['lpar_id']
            lpar_state = lpar['state']
        else:
            raise exception.PowerVMLPARInstanceNotFound(instance_name)
        try:
            vhost = self._operator.get_vhost_by_instance_id(managed_system,
                                                            vios_pid,
                                                            lpar_id)
            if vhost:
                disk_name = self._operator.get_disk_name_by_vhost(
                                                            managed_system,
                                                            vios_pid,
                                                            vhost)[0]
                vtopt_names = self._operator.get_vtopt_name_by_vhost(
                    managed_system, vios_pid, vhost)
            else:
                disk_name = None
                vtopt_names = None

            LOG.debug("Shutting down the instance '%s'" % instance_name)
            if lpar_state != 'Not Activated':
                self._operator.stop_lpar(instance_name, managed_system)

            volume_names = []
            if block_device_info is not None:
                block_device_mapping = \
                    block_device_info.get('block_device_mapping') or []
            else:
                block_device_mapping = []
            if block_device_mapping:
                for disk in block_device_mapping:
                    driver_volume_type = \
                        disk['connection_info']['driver_volume_type']
                    volume_data = disk['connection_info']['data']
                    volume_name = 'volume-' + volume_data['volume_id']
                    if driver_volume_type == 'iscsi':
                        disk_adapter.cancel_discovery_device(volume_name[0:15])
                        conn_info = self._get_volume_conn_info(volume_data)
                        volume_info = self.get_devname_by_conn_info(
                            managed_system, vios_pid, conn_info)
                    elif driver_volume_type == 'lvmpower':
                        volume_info = {'device_name': volume_name[0:15]}
                    volume_names.append(volume_info['device_name'])
                    disk_adapter.detach_volume_from_vhost(volume_info)

            if vtopt_names:
                for vtopt in vtopt_names:
                    if vtopt != '':
                        self._operator.remove_vtd(managed_system,
                                                  vios_pid, vtopt)

            if disk_name and disk_name not in volume_names and destroy_disks:
                # TODO(mrodden): we should also detach from the instance
                # before we start deleting things...
                volume_info = {'device_name': disk_name}
                # Volume info dictionary might need more info that is lost when
                # volume is detached from host so that it can be deleted
                disk_adapter.detach_volume_from_host(volume_info)
                disk_adapter.delete_volume(volume_info)
            if destroy_disks and vhost:
                self._operator.remove_vscsi_server(instance_name,
                     managed_system, vios_pid, vhost)

            LOG.debug("Deleting the LPAR instance '%s'" % instance_name)
            self._operator.remove_lpar(instance_name, managed_system, vios_pid)

        except Exception:
            LOG.exception(_LE("PowerVM instance cleanup failed"))
            raise exception.PowerVMLPARInstanceCleanupFailed(
                                                instance_name=instance_name)

    def power_off(self, instance,
                  timeout=constants.POWERVM_LPAR_OPERATION_TIMEOUT):
        managed_system = instance['node']
        instance_name = instance['display_name']
        lpar = self._operator.get_lpar(instance_name, managed_system)
        if lpar['state'] != constants.POWERVM_SHUTDOWN:
            self._operator.stop_lpar(instance_name, managed_system, timeout)

    def power_on(self, instance):
        managed_system = instance['node']
        instance_name = instance['display_name']
        self._operator.start_lpar(instance_name, managed_system)

    def suspend(self, instance):
        managed_system = instance['node']
        instance_name = instance['display_name']
        self._operator.suspend_lpar(instance_name, managed_system)

    def resume(self, instance):
        managed_system = instance['node']
        instance_name = instance['display_name']
        self._operator.resume_lpar(instance_name, managed_system)

    def instance_exists(self, instance_name, managed_system):
        lpar_instance = self._operator.get_lpar(instance_name, managed_system)
        return True if lpar_instance else False

    def capture_image(self, context, instance, image_id, image_meta,
                      update_task_state):
        """Capture the root disk for a snapshot

        :param context: nova context for this operation
        :param instance: instance information to capture the image from
        :param image_id: uuid of pre-created snapshot image
        :param image_meta: metadata to upload with captured image
        :param update_task_state: Function reference that allows for updates
                                  to the instance task state.
        """
        instance_name = instance['display_name']
        managed_system = instance['node']
        vios_pid = self._operator.get_vios_lpar_id(managed_system)
        disk_adapter = self._get_disk_adapter(managed_system)
        lpar = self._operator.get_lpar(instance_name, managed_system)
        previous_state = lpar['state']

        # stop the instance if it is running
        if previous_state == 'Running':
            LOG.debug("Stopping instance %s for snapshot." %
                      instance['display_name'])
            # wait up to 2 minutes for shutdown
            self.power_off(instance, timeout=120)

        # get disk_name
        vhost = self._operator.get_vhost_by_instance_id(managed_system,
                                                        vios_pid,
                                                        lpar['lpar_id'])

        if vhost:
            disk_name = self._operator.get_disk_name_by_vhost(
                                                        managed_system,
                                                        vios_pid,
                                                        vhost)[0]
        else:
            disk_name = None

        if disk_name:
            # do capture and upload
            disk_adapter.create_image_from_volume(
                disk_name, context, image_id, image_meta, update_task_state)
        else:
            raise n_exc.ImageNotFound(image_id=image_id)

        # restart instance if it was running before
        if previous_state == 'Running':
            self.power_on(instance)

    def migrate_disk(self, device_name, src_host, dest, image_path,
            instance):
        """Migrates SVC or Logical Volume based disks

        :param device_name: disk device name in /dev/
        :param dest: IP or DNS name of destination host/VIOS
        :param image_path: path on source and destination to directory
                           for storing image files
        :param instance_name: name of instance being migrated
        :returns: disk_info dictionary object describing root volume
                  information used for locating/mounting the volume
        """
        instance_name = instance['display_name']
        managed_system = instance['node']
        disk_adapter = self._get_disk_adapter(managed_system)
        dest_file_path = disk_adapter.migrate_volume(
                device_name, src_host, dest, image_path, instance_name)
        disk_info = {}
        disk_info['root_disk_file'] = dest_file_path
        return disk_info

    def deploy_from_migrated_file(self, managed_system, lpar, file_path, size,
                                  power_on=True):
        """Deploy the logical volume and attach to new lpar.

        :param lpar: lar instance
        :param file_path: logical volume path
        :param size: new size of the logical volume
        """
        need_decompress = file_path.endswith('.gz')
        vios_pid = self._operator.get_vios_lpar_id(managed_system)
        disk_adapter = self._get_disk_adapter(managed_system)

        try:
            # deploy lpar from file
            self._deploy_from_vios_file(managed_system, vios_pid, lpar,
                                        file_path, size,
                                        decompress=need_decompress,
                                        power_on=power_on)
        finally:
            # cleanup migrated file
            disk_adapter.remove_file(file_path)

    def _deploy_from_vios_file(self, managed_system, vios_pid,
                               lpar, file_path, size,
                               decompress=True, power_on=True):
        self._operator.create_lpar(lpar, managed_system)
        lpar = self._operator.get_lpar(lpar['name'], managed_system)
        instance_id = lpar['lpar_id']
        vhost = self._operator.get_vhost_by_instance_id(managed_system,
                                                        vios_pid, instance_id)

        # Create logical volume on VIOS
        disk_adapter = self._get_disk_adapter(managed_system)
        diskName = disk_adapter._create_logical_volume(size)
        # Attach the disk to LPAR
        self._operator.attach_disk_to_vhost(managed_system, vios_pid,
                                            diskName, vhost)

        # Copy file to device
        disk_adapter._copy_file_to_device(file_path, diskName,
                                          decompress)

        if power_on:
            self._operator.start_lpar(lpar['name'], managed_system)

    def get_devname_by_conn_info(self, managed_system, vios_pid, conn_info):
        dev_info = {}
        disk = []
        outputs = []
        hdisks = self._operator.get_all_hdisk(managed_system, vios_pid)
        if conn_info:
            for iqn, lun in conn_info:
                output = self._operator.get_hdisk_by_iqn(managed_system,
                    vios_pid, hdisks, iqn, lun)
                if len(output) == 0:
                    continue
                for o in output:
                    devname = o['name']

                    if disk.count(devname) == 0:
                        disk.append(devname)

        if len(disk) == 1:
            dev_info = {'device_name': disk[0]}
        elif len(disk) > 1:
            raise exception.\
                PowerVMInvalidLUNPathInfoMultiple(conn_info, outputs)
        else:
            raise exception.\
                PowerVMInvalidLUNPathInfoNone(conn_info, outputs)

        return dev_info

    def attach_volume(self, connection_info, instance):
        volume_data = connection_info['data']
        driver_type = connection_info.get('driver_volume_type')
        volume_name = 'volume-' + volume_data['volume_id']
        managed_system = instance['node']
        disk_adapter = self._get_disk_adapter(managed_system)
        vios_pid = self._operator.get_vios_lpar_id(managed_system)
        lpar_id = self._operator.get_lpar(instance['display_name'],
                                          managed_system)['lpar_id']
        if driver_type == 'lvmpower':
            vhost = self._operator.get_vhost_by_instance_id(managed_system,
                                                            vios_pid,
                                                            lpar_id)
            self._operator.attach_disk_to_vhost(managed_system, vios_pid,
                            volume_name[0:15], vhost)
        elif driver_type == 'iscsi':
            disk_adapter.discovery_device(volume_data)
            conn_info = self._get_volume_conn_info(volume_data)
            dev_info = self.get_devname_by_conn_info(managed_system, vios_pid,
                                                     conn_info)
            pvid = self._operator.get_hdisk_pvid(managed_system, vios_pid,
                                                 dev_info['device_name'])
            if pvid == 'none':
                self._operator.chdev_hdisk_pvid(managed_system, vios_pid,
                                                dev_info['device_name'])
            vhost = self._operator.get_vhost_by_instance_id(managed_system,
                                                            vios_pid,
                                                            lpar_id)
            self._operator.attach_disk_to_vhost(managed_system, vios_pid,
                            dev_info['device_name'], vhost)

    def detach_volume(self, connection_info, instance):
        managed_system = instance['node']
        driver_type = connection_info.get('driver_volume_type')
        disk_adapter = self._get_disk_adapter(managed_system)
        vios_pid = self._operator.get_vios_lpar_id(managed_system)
        volume_data = connection_info['data']
        volume_name = 'volume-' + volume_data['volume_id']
        if driver_type == 'iscsi':
            disk_adapter.cancel_discovery_device(volume_name[0:15])
            conn_info = self._get_volume_conn_info(volume_data)
            volume_info = self.get_devname_by_conn_info(managed_system,
                                                        vios_pid, conn_info)
        elif driver_type == 'lvmpower':
            volume_info = {'device_name': volume_name[0:15]}
        disk_adapter.detach_volume_from_vhost(volume_info)

    def change_iso(self, context, instance, iso_meta):
        managed_system = instance['node']
        disk_adapter = self._get_disk_adapter(managed_system)
        vios_pid = self._operator.get_vios_lpar_id(managed_system)
        lpar_id = self._operator.get_lpar(instance['display_name'],
                                          managed_system)['lpar_id']
        vhost = self._operator.get_vhost_by_instance_id(managed_system,
                                                        vios_pid, lpar_id)
        if iso_meta:
            image_id = iso_meta.get('id')
            image_name = iso_meta.get('name')
            disk_adapter.create_iso_from_isofile(context,
                instance, image_name, image_id)
            self._operator.attach_vopt_to_vhost(managed_system, vios_pid,
                                                image_name, vhost)
        else:
            vtopt_names = self._operator.get_vtopt_name_by_vhost(
                managed_system, vios_pid, vhost)
            if vtopt_names:
                self._operator.remove_vtd(managed_system, vios_pid,
                                          vtopt_names[-1])

    def list_phy_cdroms(self, instance):
        managed_system = instance['node']
        vios_pid = self._operator.get_vios_lpar_id(managed_system)
        phy_cdroms = self._operator.list_phy_cdroms(managed_system, vios_pid)
        return phy_cdroms

    def attach_phy_cdrom(self, instance, cdrom):
        managed_system = instance['node']
        vios_pid = self._operator.get_vios_lpar_id(managed_system)
        lpar_id = self._operator.get_lpar(instance['display_name'],
                                          managed_system)['lpar_id']
        vhost = self._operator.get_vhost_by_instance_id(managed_system,
                                                        vios_pid, lpar_id)
        self._operator.attach_disk_to_vhost(managed_system, vios_pid,
                                            cdrom, vhost)

    def detach_phy_cdrom(self, instance, cdrom):
        managed_system = instance['node']
        vios_pid = self._operator.get_vios_lpar_id(managed_system)
        lpar_id = self._operator.get_lpar(instance['display_name'],
                                          managed_system)['lpar_id']
        vhost = self._operator.get_vhost_by_instance_id(managed_system,
                                                        vios_pid, lpar_id)
        cdrom_mapping = self._operator.get_cdrom_mapping_by_vhost(
            managed_system, vios_pid, vhost)
        if cdrom in cdrom_mapping:
            self._operator.remove_vtd(managed_system, vios_pid,
                                      cdrom_mapping[cdrom])


class HMCBaseOperator(BaseOperator):
    """Base operator for IVM and HMC managed systems."""

    def __init__(self, connection):
        super(HMCBaseOperator, self).__init__(connection)

    def get_available_nodes(self):
        cmd = self.command.lssyscfg('-r sys -F name')
        output = self.run_hmc_command(cmd)
        return output

    def _poll_for_lpar_status(self, instance_name, managed_system,
                            status, operation,
                            timeout=constants.POWERVM_LPAR_OPERATION_TIMEOUT):
        """Polls until the LPAR with the given name reaches the given status.

        :param instance_name: LPAR instance name
        :param status: Poll until the given LPAR status is reached
        :param operation: The operation being performed, e.g. 'stop_lpar'
        :param timeout: The number of seconds to wait.
        :raises: PowerVMLPARInstanceNotFound
        :raises: PowerVMLPAROperationTimeout
        :raises: InvalidParameterValue
        """
        # make sure it's a valid status
        if (status == constants.POWERVM_NOSTATE or
                status not in constants.POWERVM_POWER_STATE):
            msg = _("Invalid LPAR state: %s") % status
            raise n_exc.InvalidParameterValue(err=msg)

        # raise the given timeout exception if the loop call doesn't complete
        # in the specified timeout
        timeout_exception = exception.PowerVMLPAROperationTimeout(
                                                operation=operation,
                                                instance_name=instance_name)
        with eventlet_timeout.Timeout(timeout, timeout_exception):
            def _wait_for_lpar_status(instance_name, managed_system, status):
                """Called at an interval until the status is reached."""
                lpar_obj = self.get_lpar(instance_name, managed_system)
                if lpar_obj['state'] == status:
                    raise loopingcall.LoopingCallDone()

            timer = loopingcall.FixedIntervalLoopingCall(_wait_for_lpar_status,
                                                    instance_name,
                                                    managed_system, status)
            timer.start(interval=1).wait()

    def get_lpar(self, instance_name, managed_system, resource_type='lpar'):
        """Return a LPAR object by its instance name.

        :param instance_name: LPAR instance name
        :param resource_type: the type of resources to list
        :returns: LPAR object
        """
        cmd = self.command.lssyscfg('-m %s -r %s --filter "lpar_names=%s"'
                                    % (managed_system, resource_type,
                                       instance_name))
        try:
            output = self.run_hmc_command(cmd)
        except Exception:
            return None
        cmd1 = self.command.lssyscfg('-m %s -r prof --filter "lpar_names=%s"'
                                    % (managed_system, instance_name))
        output1 = self.run_hmc_command(cmd1)
        confdata = output[0] + ',' + output1[0]
        lpar = LPAR.load_from_conf_data(confdata)
        return lpar

    def list_lpar_instances(self, managed_system):
        """List all existent LPAR instances names.

        :returns: list -- list with instances names.
        """
        lpar_names = self.run_hmc_command(self.command.lssyscfg(
                    '-m %s -r lpar -F name' % managed_system))
        if not lpar_names:
            return []
        return lpar_names

    def _get_max_virt_slot(self, managed_system, vios_lpar_id):
        """Helper to get the max virtual slots number from the VIOS LPAR."""
        cmd = self.command.lshwres('-m %s -r virtualio --rsubtype slot '
            '--level lpar --filter lpar_ids=%s -F curr_max_virtual_slots' %
            (managed_system, vios_lpar_id))
        max_virt_slots, = self.run_hmc_command(cmd)
        return int(max_virt_slots)

    def _get_min_virt_slot(self, managed_system):
        """Helper to get the minimal virtual slot number from the VIOS LPAR."""
        cmd = self.command.lshwres('-m %s -r virtualio --rsubtype slot '
            '--level sys -F min_vios_virtual_slot_num' % managed_system)
        min_virt_slots, = self.run_hmc_command(cmd)
        return int(min_virt_slots)

    def _find_avail_virt_slot(self, managed_system, vios_lpar_id):
        slots_in_use = map(int, self.list_vios_virt_slots(managed_system,
                                                          vios_lpar_id))
        avail_slot = None
        min_virt_slot = self._get_min_virt_slot(managed_system)
        max_virt_slot = self._get_max_virt_slot(managed_system, vios_lpar_id)
        for slot in range(min_virt_slot, max_virt_slot + 1):
            if slot not in slots_in_use:
                avail_slot = slot
                break
        return avail_slot

    def list_vios_virt_slots(self, managed_system, vios_lpar_id):
        cmd = self.command.lshwres('-m %s -r virtualio --rsubtype slot '
            '--level slot --filter lpar_ids=%s -F slot_num' %
            (managed_system, vios_lpar_id))
        slots_in_use = self.run_hmc_command(cmd)
        return slots_in_use

    def _create_vscsi_server_adapter(self, managed_system, vios_lpar_id,
                                      lpar_id, slot_num):
        cmd = self.command.chhwres('-m %s -r virtualio -o a --id %s '
            '--rsubtype scsi -s %s '
            '-a "adapter_type=server,remote_lpar_id=%s,remote_slot_num=2"' %
            (managed_system, vios_lpar_id, slot_num, lpar_id))
        self.run_hmc_command(cmd)

    @lockutils.synchronized('create_lpar', external=True)
    def create_lpar(self, lpar, managed_system):
        """Receives a LPAR data object and creates a LPAR instance.

        :param lpar: LPAR object
        """
        vios_lpar_id = self.get_vios_lpar_id(managed_system)
        slot_num = self._find_avail_virt_slot(managed_system, vios_lpar_id)
        lpar.__setitem__('virtual_scsi_adapters',
            '2/client/%s//%s/1' % (vios_lpar_id, slot_num))
        conf_data = lpar.to_string()
        cmd = self.command.mksyscfg('-m %s -r lpar -i "%s"' %
                                    (managed_system, conf_data))
        self.run_hmc_command(cmd)
        lpar_id = self.get_lpar(lpar['name'], managed_system)['lpar_id']
        self._create_vscsi_server_adapter(managed_system, vios_lpar_id,
                                          lpar_id, slot_num)

    def start_lpar(self, instance_name, managed_system,
                   timeout=constants.POWERVM_LPAR_OPERATION_TIMEOUT):
        """Start a LPAR instance."""
        prof = instance_name
        cmd = self.command.lssyscfg(
            '-m %s -r lpar -F default_profile,curr_profile '
            '--filter lpar_names=%s' %
            (managed_system, instance_name))
        output = self.run_hmc_command(cmd)
        profs = output[0].split(',')
        if profs[1]:
            prof = profs[1]
        elif profs[0]:
            prof = profs[0]
        cmd = self.command.chsysstate('-m %s -r lpar -o on -n %s -f %s'
                                      % (managed_system, instance_name, prof))
        self.run_hmc_command(cmd)
        # poll instance until running or raise exception
        self._poll_for_lpar_status(instance_name, managed_system,
                                   constants.POWERVM_RUNNING,
                                   'start_lpar', timeout)

    def stop_lpar(self, instance_name, managed_system,
                  timeout=constants.POWERVM_LPAR_OPERATION_TIMEOUT):
        """Stop a running LPAR."""
        cmd = self.command.chsysstate('-m %s -r lpar -o shutdown '
                                      '--immed -n %s' %
                                      (managed_system, instance_name))
        self.run_hmc_command(cmd)

        # poll instance until stopped or raise exception
        self._poll_for_lpar_status(instance_name, managed_system,
                                   constants.POWERVM_SHUTDOWN,
                                   'stop_lpar', timeout)

    def suspend_lpar(self, instance_name, managed_system):
        """Suspend a running LPAR."""
        cmd = self.command.chlparstate('-m %s -o suspend -p %s' %
                                       (managed_system, instance_name))
        self.run_hmc_command(cmd)

    def resume_lpar(self, instance_name, managed_system):
        """Resume a suspend LPAR."""
        cmd = self.command.chlparstate('-m %s -o resume -p %s' %
                                       (managed_system, instance_name))
        self.run_hmc_command(cmd)

    def get_vscsi_server_slot_num(self, instance_name, managed_system):
        lpar = self.get_lpar(instance_name, managed_system)
        virtual_scsi_adapters = \
            lpar.__getitem__('virtual_scsi_adapters')
        if virtual_scsi_adapters != 'none':
            vscsi_client_adapter, = virtual_scsi_adapters.split(',')
            # Example: 2/client/2/vio-name/13/1. We want the remote slot
            # id which is '13'.
            slot_num = vscsi_client_adapter.split('/')[4]
            return slot_num
        return None

    def _remove_virtual_terminal(self, instance_name, managed_system):
        cmd = 'rmvterm -m %s -p %s' % (managed_system, instance_name)
        self.run_hmc_command(cmd)

    def _remove_vscsi_server_adapter(self, managed_system,
                                     vios_pid, slot_num, vhost):
        cmd = self.command.viosvrcmd('-m %s --id %s -c '
                                      '\'rmdev -dev %s -recursive\'' %
                                      (managed_system, vios_pid, vhost))
        self.run_hmc_command(cmd)
        cmd1 = self.command.chhwres('-r virtualio -m %s -o r --id %s '
            '--rsubtype scsi -s %s' %
            (managed_system, vios_pid, slot_num))
        self.run_hmc_command(cmd1)

    def remove_lpar(self, instance_name, managed_system, vios_pid):
        """Removes a LPAR.

        :param instance_name: LPAR instance name
        """
        self._remove_virtual_terminal(instance_name, managed_system)
        cmd = self.command.rmsyscfg('-m %s -r lpar -n %s' %
                                    (managed_system, instance_name))
        self.run_hmc_command(cmd)

    def remove_vscsi_server(self, instance_name, managed_system,
                            vios_pid, vhost):
        slot_num = self.get_vscsi_server_slot_num(instance_name,
                                                  managed_system)
        self._remove_vscsi_server_adapter(managed_system, vios_pid,
                                          slot_num, vhost)

    def get_vhost_by_instance_id(self, managed_system, vios_pid, instance_id):
        """Return the vhost name by the instance id.

        :param instance_id: LPAR instance id
        :returns: string -- vhost name or None in case none is found
        """
        instance_hex_id = '%#010x' % int(instance_id)
        cmd = self.command.viosvrcmd('-m %s --id %s -c '
            '\'lsmap -all -field clientid svsa -fmt :\'' %
            (managed_system, vios_pid))
        try:
            output = self.run_hmc_command(cmd)
            vhosts = dict(item.split(':') for item in list(output))
        except Exception:
            return None

        if instance_hex_id in vhosts:
            return vhosts[instance_hex_id]

        return None

    def get_virtual_eth_adapter_id(self, instance):
        """Virtual ethernet adapter id.

        Searches for the shared ethernet adapter and returns
        its id.

        :returns: id of the virtual ethernet adapter.
        """
        managed_system = instance['node']
        vios_pid = self.get_vios_lpar_id(managed_system)
        cmd = self.command.viosvrcmd('-m %s --id %s -c \'lsmap '
                                     '-all -net -field sea -fmt :\'' %
                                     (managed_system, vios_pid))
        output = self.run_hmc_command(cmd)
        sea = output[0]
        cmd = self.command.viosvrcmd('-m %s --id %s -c \'lsdev -dev %s '
                                     '-attr pvid\'' %
                                     (managed_system, vios_pid, sea))
        output = self.run_hmc_command(cmd)
        # Returned output looks like this: ['value', '', '1']
        if output:
            return output[2]

        return None

    def get_vswitch_by_vlanid(self, instance, vlan_id):
        managed_system = instance['node']
        cmd = self.command.lshwres('-r virtualio -m %s --rsubtype vswitch'
                                   ' --filter vlans=%s -F vswitch' %
                                   (managed_system, vlan_id))
        output = self.run_hmc_command(cmd)
        if output:
            for vswitch in output:
                if "Default" in vswitch:
                    continue
                else:
                    return vswitch

        return None

    def get_disk_name_by_vhost(self, managed_system, vios_pid, vhost):
        """Return the disk names attached to a specific vhost

        :param vhost: vhost name
        :returns: list of disk names attached to the vhost,
                  ordered by time of attachment
                  (first disk should be boot disk)
                  Returns None if no disks are found.
        """
        cmd = self.command.viosvrcmd('-m %s --id %s -c \'lsmap -vadapter %s '
                                     '-type disk lv -field LUN backing '
                                     '-fmt :\'' %
                                     (managed_system, vios_pid, vhost))
        output = self.run_hmc_command(cmd)
        if output:
            # assemble dictionary of [ lun => diskname ]
            lun_to_disk = {}
            remaining_str = output[0].strip()
            while remaining_str:
                values = remaining_str.split(':', 2)
                lun_to_disk[values[0]] = values[1]
                if len(values) > 2:
                    remaining_str = values[2]
                else:
                    remaining_str = ''

            lun_numbers = lun_to_disk.keys()
            lun_numbers.sort()

            # assemble list of disknames ordered by lun number
            disk_names = []
            for lun_num in lun_numbers:
                disk_names.append(lun_to_disk[lun_num])

            return disk_names

        return [None]

    def remove_vtd(self, managed_system, vios_pid, vtopt):
        if vtopt and vtopt != '':
            cmd = self.command.viosvrcmd('-m %s --id %s -c '
                '\'rmvdev -vtd %s\'' %
                (managed_system, vios_pid, vtopt))
            self.run_hmc_command(cmd)

    def get_vtopt_name_by_vhost(self, managed_system, vios_pid, vhost):
        command = self.command.viosvrcmd('-m %s --id %s -c \'lsmap '
            '-vadapter %s -type file_opt -field LUN vtd -fmt :\'' %
            (managed_system, vios_pid, vhost))
        output = self.run_vios_command(command)
        if output:
            lun_to_vtd = {}
            remaining_str = output[0].strip()
            while remaining_str:
                values = remaining_str.split(':', 2)
                lun_to_vtd[values[0]] = values[1]
                if len(values) > 2:
                    remaining_str = values[2]
                else:
                    remaining_str = ''

            lun_numbers = lun_to_vtd.keys()
            lun_numbers.sort()

            # assemble list of disknames ordered by lun number
            vtopt_names = []
            for lun_num in lun_numbers:
                vtopt_names.append(lun_to_vtd[lun_num])

            return vtopt_names

        return None

    def attach_disk_to_vhost(self, managed_system, vios_pid, disk, vhost):
        """Attach disk name to a specific vhost.

        :param disk: the disk name
        :param vhost: the vhost name
        """
        cmd = self.command.viosvrcmd('-m %s --id %s -c '
            '\'mkvdev -vdev %s -vadapter %s\'' %
            (managed_system, vios_pid, disk, vhost))
        self.run_hmc_command(cmd)

    def attach_vopt_to_vhost(self, managed_system, vios_pid, iso_name, vhost):
        # Create a vopt for vhost
        cmd = self.command.viosvrcmd('-m %s --id %s -c '
            '\'mkvdev -fbo -vadapter %s\'' %
            (managed_system, vios_pid, vhost))
        vopt_info = self.run_hmc_command(cmd)[0]
        vopt = vopt_info.split()[0]
        loadopt = self.command.viosvrcmd('-m %s --id %s -c '
            '\'loadopt -f -vtd %s -disk %s\'' %
            (managed_system, vios_pid, vopt, iso_name))
        self.run_hmc_command(loadopt)

    def get_hostname(self, managed_system, vios_lpar_id):
        """Returns the managed system hostname.

        :returns: string -- hostname
        """
        output = self.run_hmc_command(
            self.command.viosvrcmd('-m %s --id %s -c hostname' %
                                   (managed_system, vios_lpar_id)))
        hostname = output[0]
        if not hasattr(self, '_hostname'):
            self._hostname = hostname
        elif hostname != self._hostname:
            LOG.error(_('Hostname has changed from %(old)s to %(new)s. '
                        'A restart is required to take effect.'
                        ) % {'old': self._hostname, 'new': hostname})
        return self._hostname

    def get_vios_lpar_id(self, managed_system):
        cmd = self.command.lssyscfg(
            '-m %s -r sys -F service_lpar_id' % managed_system)
        output = self.run_hmc_command(cmd)
        lpar_id = output[0]
        return lpar_id

    def get_memory_info(self, managed_system):
        """Get memory info.

        :returns: tuple - memory info (total_mem, avail_mem)
        """
        cmd = self.command.lshwres(
            '-m %s -r mem --level sys -F '
            'configurable_sys_mem,curr_avail_sys_mem' %
            managed_system)
        output = self.run_hmc_command(cmd)
        total_mem, avail_mem = output[0].split(',')
        return {'total_mem': int(total_mem),
                'avail_mem': int(avail_mem)}

    def get_cpu_info(self, managed_system):
        """Get CPU info.

        :returns: tuple - cpu info (total_procs, avail_procs)
        """
        cmd = self.command.lshwres(
            '-m %s -r proc --level sys -F '
            'configurable_sys_proc_units,curr_avail_sys_proc_units' %
            managed_system)
        output = self.run_hmc_command(cmd)
        total_procs, avail_procs = output[0].split(',')
        return {'total_procs': float(total_procs) * 10,
                'avail_procs': float(avail_procs) * 10}

    def get_disk_info(self, managed_system, vios_lpar_id):
        """Get the disk usage information.

        :returns: tuple - disk info (disk_total, disk_used, disk_avail)
        """
        vgs = self.run_hmc_command(
            self.command.viosvrcmd('-m %s --id %s -c lsvg' %
                                   (managed_system, vios_lpar_id)))
        (disk_total, disk_used, disk_avail) = [0, 0, 0]
        for vg in vgs:
            cmd = self.command.viosvrcmd(
                '-m %s --id %s -c \'lsvg %s -field '
                'totalpps usedpps freepps -fmt :\''
                % (managed_system, vios_lpar_id, vg))
            output = self.run_hmc_command(cmd)
            # Output example:
            # 1271 (10168 megabytes):0 (0 megabytes):1271 (10168 megabytes)
            (d_total, d_used, d_avail) = re.findall(r'(\d+) megabytes',
                                                    output[0])
            disk_total += int(d_total)
            disk_used += int(d_used)
            disk_avail += int(d_avail)

        return {'disk_total': disk_total,
                'disk_used': disk_used,
                'disk_avail': disk_avail}

    def run_hmc_command(self, cmd, check_exit_code=True):
        """Run a remote command using an active ssh connection.

        :param command: String with the command to run.
        """
        return self.run_vios_command(cmd, check_exit_code=True)

    def macs_for_instance(self, instance):
        pass

    def get_logical_vol_size(self, managed_system, vios_pid, diskname):
        """Finds and calculates the logical volume size in GB """
        cmd = self.command.viosvrcmd('-m %s --id %s '
                                     '-c \'lslv %s -fmt : -field pps ppsize\''
                                     % (managed_system, vios_pid, diskname))
        output = self.run_hmc_command(cmd)
        pps, ppsize = output[0].split(':')
        ppsize = re.findall(r'\d+', ppsize)
        ppsize = int(ppsize[0])
        pps = int(pps)
        lv_size = ((pps * ppsize) / 1024)

        return lv_size

    def get_hdisk_size(self, managed_system, vios_pid, diskname):
        cmd = self.command.viosvrcmd('-m %s --id %s '
                                     '-c \'lspv -size %s\'' %
                                     (managed_system, vios_pid, diskname))
        output = self.run_hmc_command(cmd)
        size_mb = int(output[0])
        disk_size = size_mb / 1024
        return disk_size

    def rename_lpar(self, instance_name, managed_system, new_name):
        """Rename LPAR given by instance_name to new_name"""

        new_name_trimmed = new_name[:31]

        cmd = ''.join(['chsyscfg -m %s -r lpar -i ' % managed_system,
                       '"',
                       'name=%s,' % instance_name,
                       'new_name=%s' % new_name_trimmed,
                       '"'])

        self.run_hmc_command(cmd)

        return new_name_trimmed

    def _remove_file(self, managed_system, vios_pid, file_path):
        """Removes a file on the VIOS partition

        :param file_path: absolute path to file to be removed
        """
        command = self.command.viosvrcmd('-m %s --id %s -c \'rm -f %s\'' %
                                         (managed_system, vios_pid, file_path))
        self.run_hmc_command(command)

    def get_all_hdisk(self, managed_system, vios_pid):
        disks = []
        command = self.command.viosvrcmd('-m %s --id %s -c'
            ' \'lsdev -type disk -field name\'' %
            (managed_system, vios_pid))
        output = self.run_hmc_command(command)
        if output:
            for o in output:
                if o.startswith('hdisk'):
                    disks.append(o)
            return disks
        return None

    def get_hdisk_by_iqn(self, managed_system, vios_pid,
                         hdisks, iqn, lun='0x0'):
        if iqn is None:
            return None
        if hdisks is None:
            return None
        attrs = ['target_name', 'port_num', 'lun_id', 'host_addr', 'pvid']
        hdiskattr = {}
        hdiskattrs = {}
        hdisklist = []
        for hdisk in hdisks:
            cmd = self.command.viosvrcmd('-m %s --id %s -c'
                ' \'lsdev -dev %s -attr target_name\'' %
                (managed_system, vios_pid, hdisk))
            try:
                nameoutput = self.run_hmc_command(cmd)
            except Exception:
                continue
            cmd = self.command.viosvrcmd('-m %s --id %s -c'
                ' \'lsdev -dev %s -attr lun_id\'' %
                (managed_system, vios_pid, hdisk))
            lunoutput = self.run_hmc_command(cmd)
            if nameoutput[2] == str(iqn) and lunoutput[2] == str(lun):
                for attr in attrs:
                    cmd = self.command.viosvrcmd('-m %s --id %s -c'
                        ' \'lsdev -dev %s -attr %s\'' %
                        (managed_system, vios_pid, hdisk, attr))
                    attroutput = self.run_hmc_command(cmd)
                    if attroutput is not None:
                        hdiskattr[attr] = attroutput[2]
                hdiskattrs['name'] = hdisk
                hdiskattrs['attr'] = hdiskattr
                hdisklist.append(hdiskattrs)
        return hdisklist

    def get_hdisk_pvid(self, managed_system, vios_pid, hdisk):
        if hdisk is None:
            return None
        cmd = self.command.viosvrcmd('-m %s --id %s -c'
            ' \'lsdev -dev %s -attr pvid\'' %
            (managed_system, vios_pid, hdisk))
        output = self.run_hmc_command(cmd)
        if len(output) > 1:
            return output[2]
        return None

    def chdev_hdisk_pvid(self, managed_system, vios_pid, hdisk, pv=False):
        if hdisk is None:
            return None
        if not pv:
            cmd = self.command.viosvrcmd('-m %s --id %s -c'
                ' \'chdev -dev %s -attr pv=yes\'' %
                (managed_system, vios_pid, hdisk))
        else:
            cmd = self.command.viosvrcmd('-m %s --id %s -c'
                ' \'chdev -dev %s -attr pv=no\'' %
                (managed_system, vios_pid, hdisk))
        self.run_hmc_command(cmd)

    def check_power_version(self, managed_system):
        lower_versions = ['POWER5', 'POWER6']
        cmd = self.command.lssyscfg('-m %s -r sys'
            ' -F lpar_proc_compat_modes' % managed_system)
        version = self.run_hmc_command(cmd)[0]
        for lower_version in lower_versions:
            if lower_version in version:
                return True
        return False

    def get_lparname_by_lparid(self, managed_system, lpar_id):
        cmd = self.command.lssyscfg('-m %s -r lpar '
                                    '--filter "lpar_ids=%s" -F name' %
                                    (managed_system, lpar_id))
        lpar_name = self.run_hmc_command(cmd)[0]
        return lpar_name

    def list_phy_cdroms(self, managed_system, vios_pid):
        cdroms_list = []
        cmd = self.command.viosvrcmd('-m %s --id %s -c'
            ' \'lsmap -type optical -all '
            '-field backing clientid -fmt :\'' % (managed_system, vios_pid))
        output = self.run_hmc_command(cmd)
        used_cdroms = {}
        for item in list(output):
            item_list = item.split(':')
            used_cdroms[item_list[1]] = item_list[0]

        cmd = self.command.viosvrcmd('-m %s --id %s -c'
            ' \'lsdev -field name description physloc -fmt ,'
            ' -type optical -state available\'' % (managed_system, vios_pid))
        cdroms = self.run_hmc_command(cmd)
        if cdroms:
            for cdrom in cdroms:
                cdrom_dict = {}
                cdrom_info = cdrom.split(',')
                cdrom_dict['host'] = managed_system
                cdrom_dict['name'] = cdrom_info[0]
                cdrom_dict['description'] = cdrom_info[1]
                cdrom_dict['physloc'] = cdrom_info[2]
                if cdrom_info[0] in used_cdroms:
                    cdrom_dict['status'] = 'used'
                    lparid = int(used_cdroms[cdrom_info[0]], 16)
                    lpar_name = self.get_lparname_by_lparid(managed_system,
                                                            lparid)
                    cdrom_dict['used_by'] = lpar_name
                else:
                    cdrom_dict['status'] = 'available'
                    cdrom_dict['used_by'] = None
                cdroms_list.append(cdrom_dict)
        return cdroms_list

    def get_cdrom_mapping_by_vhost(self, managed_system, vios_pid, vhost):
        cmd = self.command.viosvrcmd('-m %s --id %s -c'
            ' \'lsmap -vadapter %s -type optical '
            '-field backing vtd -fmt ,\'' % (managed_system, vios_pid, vhost))
        output = self.run_hmc_command(cmd)
        cdrom_mapping = dict(item.split(',') for item in list(output))
        return cdrom_mapping


class HMCOperator(HMCBaseOperator):
    """Hardware Management Console (HMC) Operator.

    Runs specific commands on an HMC managed system.
    """

    def __init__(self, hmc_connection):
        self.command = command.HMCCommand()
        HMCBaseOperator.__init__(self, hmc_connection)

    def macs_for_instance(self, instance):
        """Generates set of valid MAC addresses for an IVM instance."""
        # NOTE(vish): We would prefer to use 0xfe here to ensure that linux
        #             bridge mac addresses don't change, but it appears to
        #             conflict with libvirt, so we use the next highest octet
        #             that has the unicast and locally administered bits set
        #             properly: 0xfa.
        #             Discussion: https://bugs.launchpad.net/nova/+bug/921838
        # NOTE(mjfork): For IVM-based PowerVM, we cannot directly set a MAC
        #               address on an LPAR, but rather need to construct one
        #               that can be used.  Retain the 0xfa as noted above,
        #               but ensure the final 2 hex values represent a value
        #               between 32 and 64 so we can assign as the slot id on
        #               the system. For future reference, the last octect
        #               should not exceed FF (255) since it would spill over
        #               into the higher-order octect.
        #
        #               FA:xx:xx:xx:xx:[32-64]

        macs = set()
        mac_base = [0xfa,
               random.randint(0x00, 0xff),
               random.randint(0x00, 0xff),
               random.randint(0x00, 0xff),
               random.randint(0x00, 0xff),
               random.randint(0x00, 0x00)]
        for n in range(32, 64):
            mac_base[5] = n
            macs.add(':'.join(map(lambda x: "%02x" % x, mac_base)))

        return macs
