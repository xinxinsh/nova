# Copyright 2014 Chinac, Inc
# All Rights Reserved.
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


from nova.api.openstack import common
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova import compute
from nova.compute import power_state
from nova import exception
from nova.i18n import _
from nova.i18n import _LI
from nova.image import glance
from nova import objects
from nova import volume
from oslo_log import log as logging
import webob
from webob import exc

LOG = logging.getLogger(__name__)

ALIAS = "os-live_snapshot_instance"
authorize = extensions.os_compute_authorizer(ALIAS)


class LiveSnapshotInstanceController(wsgi.Controller):
    def __init__(self, *args, **kwargs):
        super(LiveSnapshotInstanceController, self).__init__(*args, **kwargs)
        self.compute_api = compute.API()
        self.volume_api = volume.API()

    def index(self, req):
        """Returns a snapshot list of the given instance"""

        context = req.environ['nova.context']
        authorize(context)

        id = req.GET.get('instance_id')
        if not id:
            msg = _("Missing 'instance_id'")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        image_service = glance.get_default_image_service()
        image_meta = image_service.detail(context)

        snapshot_list = []
        for meta in image_meta:
            if 'instance_uuid' not in meta['properties']:
                pass
            else:
                if(meta['properties']['instance_uuid'] == id
                   and meta['properties']['image_type'] ==
                        'snapshot_for_instance'):

                    snapshot = {}
                    snapshot['snapshot_id'] = meta['id']
                    snapshot['snapshot_name'] = meta['name']
                    snapshot['snapshot_type'] = \
                        meta['properties']['snapshot_type']
                    snapshot['snapshot_build_type'] = \
                        meta['properties']['snapshot_build_type']
                    snapshot['snapshot_status'] = meta['status']
                    snapshot['created_at'] = meta['created_at']
                    snapshot_list.append(snapshot)

        return {'snapshot_list': snapshot_list}

    def show(self, req, id):
        """Returns snapshot info of the given image."""

        context = req.environ['nova.context']
        authorize(context)

        try:
            snapshot_info = self.compute_api.get_snapshot_info(context, id)
        except exception.NotFound as e:
            raise exc.HTTPNotFound(explanation=e.format_message())

        return {'snapshot_info': snapshot_info}

    def delete(self, req, id):
        """Delete a snapshot of the given image"""

        context = req.environ['nova.context']
        authorize(context)

        LOG.info(_LI("Delete snapshot with id:%s"), id, context=context)

        msg = self.compute_api.delete_vm_snapshot(context, id)
        if msg != '':
            self.compute_api.operation_log_about_instance(context, 'Failed')
            raise exc.HTTPNotFound(explanation=msg)

        self.compute_api.operation_log_about_instance(context, 'Succeeded')
        return webob.Response(status_int=200)

    def create(self, req, body):
        """Snapshot the given instance."""

        context = req.environ['nova.context']
        authorize(context)

        if not self.is_valid_body(body, 'live_snapshot'):
            msg = (_("Missing required element '%s' in request body") %
                   'live_snapshot')
            raise exc.HTTPBadRequest(explanation=msg)
        live_snapshot = body.get("live_snapshot", {})

        id = live_snapshot.get("instance_id")
        if not id:
            msg = _("Missing 'instance_id'")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        display_name = live_snapshot.get("display_name")
        if not display_name:
            msg = _("Missing 'display_name'")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        # manual/automatic
        snapshot_build_type = live_snapshot.get("snapshot_build_type",
                                                "manual")

        # create system snapshot
        system_snapshot = live_snapshot.get("system_snapshot", True)
        if system_snapshot:
            system_snapshot = "True"
        else:
            system_snapshot = "False"

        # volume snapshot list
        volume_snapshot_list = live_snapshot.get("volume_snapshot_list", [])

        if system_snapshot == "False" and len(volume_snapshot_list) == 0:
            msg = _("No disk is selected")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        LOG.info(_LI("Create live_snapshot_instance of vm:%s"), id,
                 context=context)

        props = {}
        metadata = live_snapshot.get(
            'metadata', {'snapshot_build_type': snapshot_build_type,
                         'system_snapshot': system_snapshot})
        common.check_img_metadata_properties_quota(context, metadata)
        try:
            props.update(metadata)
        except ValueError:
            msg = _("Invalid metadata")
            raise exc.HTTPBadRequest(explanation=msg)

        instance = common.get_instance(self.compute_api,
                                       context,
                                       id)

        if(instance['vm_state'] not in ['active', 'paused', 'stopped']
           or instance['power_state'] not in [power_state.RUNNING,
                                              power_state.PAUSED,
                                              power_state.SHUTDOWN]
           or instance['task_state'] is not None):
            raise exception.InstanceNotReady(instance_id=instance['uuid'])

        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
            context, instance.uuid)

        try:
            # if instance['vm_state'] == 'stopped':
            #    memory_snapshot = False
            # else:
            #    memory_snapshot = True
            if self.compute_api.is_volume_backed_instance(context,
                                                          instance,
                                                          bdms):
                image_system = False
            else:
                image_system = True

            image = self.compute_api.snapshot_for_instance(
                context,
                instance,
                display_name,
                volume_snapshot_list,
                extra_properties=props,
                memory_snapshot=False,
                image_system=image_system)

        except exception.Invalid as err:
            self.compute_api.operation_log_about_instance(context, 'Failed')
            raise exc.HTTPBadRequest(explanation=err.format_message())

        snapshot = {}
        snapshot['snapshot_id'] = image['id']
        snapshot['snapshot_name'] = image['name']
        snapshot['snapshot_actual_status'] = \
            image['properties']['snapshot_actual_status']
        snapshot['snapshot_info'] = self.compute_api.get_snapshot_info(
            context,
            image['id'])

        self.compute_api.update_snapshot_info(context, instance, image['id'])

        return {'snapshot': snapshot}

    def rollback_to_memory_snapshot(self, req, body):
        """Make vm rollback to a memory snapshot."""

        context = req.environ['nova.context']
        authorize(context)

        if not self.is_valid_body(body, 'rollback_to_memory_snapshot'):
            msg = (_("Missing required element '%s' in request body") %
                   'rollback_to_memory_snapshot')
            raise exc.HTTPBadRequest(explanation=msg)
        snapshot = body.get("rollback_to_memory_snapshot", {})

        snapshot_id = snapshot.get("snapshot_id")
        if not snapshot_id:
            msg = _("'snapshot_id' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        try:
            image_service = glance.get_default_image_service()
            image_meta = image_service.show(context, snapshot_id)
        except exception.NotFound:
            msg = _("snapshot_id could not be found")
            raise exc.HTTPNotFound(explanation=msg)

        try:
            vm_uuid = image_meta['properties']['instance_uuid']
        except Exception:
            msg = _("vm_uuid could not be found in snapshot")
            raise exc.HTTPNotFound(explanation=msg)

        instance = common.get_instance(self.compute_api,
                                       context,
                                       vm_uuid)

        if(instance['vm_state'] != 'stopped'
           or instance['power_state'] != power_state.SHUTDOWN
           or instance['task_state'] is not None):
            raise exception.InstanceNotReady(instance_id=instance['uuid'])

        self.compute_api.rollback_to_memory_snapshot(context,
                                                     instance,
                                                     image_meta)

        return webob.Response(status_int=200)

    def rollback_to_snapshot(self, req, body):
        """Make vm rollback to a memory snapshot."""

        context = req.environ['nova.context']
        authorize(context)

        if not self.is_valid_body(body, 'rollback_to_snapshot'):
            msg = (_("Missing required element '%s' in request body") %
                   'rollback_to_snapshot')
            raise exc.HTTPBadRequest(explanation=msg)

        snapshot_body = body.get("rollback_to_snapshot", {})
        snapshot_id = snapshot_body.get("snapshot_id")
        if not snapshot_id:
            msg = _("'snapshot_id' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        disk_snapshot_list = snapshot_body.get("disk_snapshot_list", [])
        is_rollback_memory = snapshot_body.get("is_rollback_memory", False)
        auto_start = snapshot_body.get("auto_start", True)
        try:
            image_service = glance.get_default_image_service()
            image_meta = image_service.show(context, snapshot_id)
            vm_uuid = image_meta.get('properties', {}).get('instance_uuid')
        except exception.NotFound:
            msg = _("snapshot_id could not be found")
            raise exc.HTTPNotFound(explanation=msg)

        try:

            instance = common.get_instance(self.compute_api, context, vm_uuid)
        except Exception:
            error_msg = 'instance(%s) could not be found.' % vm_uuid
            raise exc.HTTPNotFound(explanation=error_msg)

        try:
            if (instance['vm_state'] != 'stopped' or
                        instance['power_state'] != 4 or
                        instance['task_state'] is not None):
                raise exception.InstanceNotReady(instance_id=instance['uuid'])

            block_device_mappings = image_meta['properties'].get(
                'block_device_mapping')
            snapshot = self.compute_api.get_snapshot_info(context, snapshot_id)
            disk_snapshot = snapshot.get('disk_snapshot', [])
            LOG.debug('disk_snapshot: %s', disk_snapshot)
            bdm_dict = {}
            for bdm in block_device_mappings:
                bdm_dict[bdm['snapshot_id']] = bdm['device_name']
            for disk in disk_snapshot:
                disk_snapshot_id = disk.get('disk_snapshot_id')
                disk['device_name'] = bdm_dict.get(disk_snapshot_id)

            rollback_snapshot_list = []
            boot_volume = None
            for disk in disk_snapshot:
                if disk['device_name'] and disk['device_name'][-1] == 'a':
                    boot_volume = disk
                elif disk.get('disk_snapshot_id') in disk_snapshot_list:
                    rollback_snapshot_list.append(disk)

            if boot_volume:
                rollback_snapshot_list.insert(0, boot_volume)

            LOG.debug('rollback_snapshot_list: %s' % rollback_snapshot_list)
            self.compute_api.rollback_to_snapshot(context,
                                                  instance,
                                                  image_meta,
                                                  is_rollback_memory,
                                                  auto_start,
                                                  rollback_snapshot_list
                                                  )
        except Exception as ex:
            raise exc.HTTPError(explanation=ex.message)

        return webob.Response(status_int=200)


# Note: The class name is as it has to be for this to be loaded as an
# extension--only first character capitalized.
class LiveSnapshotInstance(extensions.V21APIExtensionBase):
    """Live snapshot instance support."""

    name = "LiveSnapshotInstance"
    alias = ALIAS
    version = 1

    def get_resources(self):
        actions = {'rollback_to_memory_snapshot': 'POST',
                   'rollback_to_snapshot': 'POST'}
        resources = extensions.ResourceExtension(
            ALIAS,
            LiveSnapshotInstanceController(),
            collection_actions=actions)

        return [resources]

    def get_controller_extensions(self):
        return []
