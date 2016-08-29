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

import webob

from nova.api.openstack import common
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova import compute
from nova import exception
from nova.i18n import _
from nova import objects

from nova.openstack.common import log as logging

LOG = logging.getLogger(__name__)
authorize = extensions.extension_authorizer('compute', 'attach_mem')


def _translate_mem_attachment_view(model, alias_name, instance_uuid,
                                   target_size, target_node,
                                   source_pagesize, source_nodemask):
    """Maps keys for memory hotplugin attachment details view."""
    return {
        'model': model,
        'name': alias_name,
        'instance_uuid': instance_uuid,
        'target_size': target_size,
        'target_node': target_node,
    }


def _translate_mem_attachment(mem):
    """Maps keys for memory hotplugin attachment details view to instance"""
    return {
        'model': mem['model'],
        'name': mem['name'],
        'instance_uuid': mem['instance_uuid'],
        'target_size': mem['target_size'],
        'target_node': mem['target_node'],
    }


class MemAttachmentController(wsgi.Controller):
    """The memory hotplugin attachment API controller for the OpenStack API."""

    def __init__(self):
        self.compute_api = compute.API()
        super(MemAttachmentController, self).__init__()

    def _items(self, req, server_id, entity_maker):
        """Returns a list of attachments, transformed through entity_maker."""
        context = req.environ['nova.context']
        authorize(context)
        use_slave = False
        instance = common.get_instance(
            self.compute_api, context, server_id, want_objects=True)
        results = []
        mems = objects.InstanceMemoryDeviceList.get_by_instance_uuid(
            context, instance['uuid'], use_slave=use_slave)
        results = [entity_maker(mem) for mem in mems]
        return {'memAttachments': results}

    def index(self, req, server_id):
        """Returns a list of memory device attached to server  """
        return self._items(req, server_id,
                           entity_maker=_translate_mem_attachment)

    def create(self, req, server_id, body=None):
        """Attach a memory hotplugin to an instance."""
        context = req.environ['nova.context']
        authorize(context)

        target_size = None
        target_node = None
        source_pagesize = None
        source_nodemask = None
        if body:
            attachment = body['memAttachment']
            target_size = attachment.get('target_size', None)
            target_node = attachment.get('target_node', None)
            source_pagesize = attachment.get('source_pagesize', None)
            source_nodemask = attachment.get('source_nodemask', None)
        if target_size is not None:
            pass
        else:
            msg = _("Must not input mem_hotplugin target_size")
            raise webob.exc.HTTPBadRequest(explanation=msg)
        if target_node is not None:
            pass
        else:
            target_node = '0'

        try:
            instance = common.get_instance(self.compute_api,
                                           context, server_id,
                                           want_objects=True)
            LOG.audit(_("Attach memory"), instance=instance)
            alias_name = self.compute_api.attach_mem(context,
                                                     instance,
                                                     target_size,
                                                     target_node,
                                                     source_pagesize,
                                                     source_nodemask)
        except exception.InstanceNotFound:
            msg = _("Server not found")
            raise webob.exc.HTTPNotFound(explanation=msg)
        except exception.InstanceIsLocked as e:
            raise webob.exc.HTTPConflict(explanation=e.format_message())
        except NotImplementedError:
            msg = _("memory hotplugin driver does not support this function.")
            # raise webob.exc.HTTPNotImplemented(explanation=msg)
            raise webob.exc.raise_feature_not_supported(explanation=msg)
        except exception.MemAttachFailed as e:
            LOG.exception(e)
            msg = _("Failed to attach memory hotplugin")
            raise webob.exc.HTTPInternalServerError(explanation=msg)
        except exception.InstanceInvalidState as state_error:
            common.raise_http_conflict_for_instance_invalid_state(state_error,
                                                                  'attach_mem')
        try:
            mem_dev = _translate_mem_attachment_view('dimm', alias_name,
                                                     instance['uuid'],
                                                     target_size,
                                                     target_node,
                                                     source_pagesize,
                                                     source_nodemask)
            objects.InstanceMemoryDevice.create(context, mem_dev)
        except exception.InvalidInput as e:
            raise webob.exc.HTTPBadRequest(explanation=e.format_message())
        return {'memAttachment': mem_dev}

    def delete(self, req, server_id, id):
        """Detach a memory device from an instance"""
        context = req.environ['nova.context']
        authorize(context)
        mem_name = id
        try:
            instance = common.get_instance(self.compute_api,
                                           context, server_id,
                                           want_objects=True)
            mem_dev = objects.InstanceMemoryDevice.get_by_name_uuid(
                context, mem_name, instance['uuid'])
            LOG.audit(_("Detach memory %s"), mem_dev[
                      'name'], instance=instance)
            self.compute_api.detach_mem(context, instance, mem_dev=mem_dev)
            objects.InstanceMemoryDevice.destroy_by_name_uuid(
                context, mem_name, instance['uuid'])
        except exception.InstanceNotFound as e:
            msg = _("Server not found")
            raise webob.exc.HTTPNotFound(explanation=msg)
        except exception.MemNotFound as e:
            raise webob.exc.HTTPNotFound(explanation=e.format_message())
        except exception.InstanceIsLocked as e:
            raise webob.exc.HTTPConflict(explanation=e.format_message())
        except NotImplementedError:
            msg = _("Memory driver does not support this function.")
            # raise exc.HTTPNotImplemented(explanation=msg)
            raise webob.exc.raise_feature_not_supported(explanation=msg)
        except exception.MemDetachFailed as e:
            LOG.exception(e)
            msg = _("Failed to detach memory device")
            raise webob.exc.HTTPInternalServerError(explanation=msg)
        except exception.InstanceInvalidState as state_error:
            common.\
            raise_http_conflict_for_instance_invalid_state(state_error,
                                                           'detach_interface')
        return webob.Response(status_int=202)


class Mem_hotplugin(extensions.ExtensionDescriptor):
    """Attach memory hotplugin support. """

    name = "MemHotplugin"
    # Must follow the naming conversion for extension to work
    alias = "os-mem-hotplugin"
    namespace = (
        "http://docs.openstack.org/compute/ext/mem_hotplugin/api/v1.1")
    updated = "2016-02-02T00:00:00Z"

    def get_resources(self):
        resources = []
        extenion = extensions.ResourceExtension('os-mem',
                                                MemAttachmentController(),
                                                parent=dict(
                                                    member_name='server',
                                                    collection_name='servers'))
        resources.append(extenion)
        return resources
