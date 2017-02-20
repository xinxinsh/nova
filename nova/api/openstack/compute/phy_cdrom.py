#   Copyright 2013 OpenStack Foundation
#
#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#   WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#   License for the specific language governing permissions and limitations
#   under the License.


from nova.api.openstack import common
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova import compute
from nova import exception
from nova.i18n import _
from nova import objects
from oslo_log import log as logging
import webob
from webob import exc

LOG = logging.getLogger(__name__)

ALIAS = "os-phy_cdrom"
authorize = extensions.os_compute_authorizer(ALIAS)


class PhyCdromController(wsgi.Controller):
    def __init__(self, *args, **kwargs):
        super(PhyCdromController, self).__init__(*args, **kwargs)
        self.compute_api = compute.API()
        self.host_api = compute.HostAPI()

    def _get_hosts(self, context):
        """Returns a list of hosts"""

        hosts = []
        try:
            filters = {'disabled': False, 'binary': 'nova-compute'}
            services = self.host_api.service_get_all(context, filters)

            for s in services:
                hosts.append(s['host'])
        except exception.NotFound as e:
            raise exc.HTTPNotFound(explanation=e.format_message())

        return hosts

    def _get_instance(self, context, instance_uuid):
        try:
            attrs = ['system_metadata', 'metadata']
            return objects.Instance.get_by_uuid(context, instance_uuid,
                                                expected_attrs=attrs)
        except exception.InstanceNotFound as e:
            raise webob.exc.HTTPNotFound(explanation=e.format_message())

    def index(self, req):
        """Returns a cdrom_list of all host"""

        context = req.environ['nova.context']
        authorize(context)

        instance_id = req.GET.get('instance_id')
        host_cdrom_list = []
        try:
            if not instance_id:
                hosts = self._get_hosts(context)
                for host in hosts:
                    cdrom_list = self.compute_api.list_phy_cdroms(context,
                                                                  host)
                    host_cdrom_list.extend(cdrom_list)
            else:
                instance = self._get_instance(context, instance_id)
                cdrom_list = self.compute_api.list_phy_cdroms(context,
                                                              instance.host)
                host_cdrom_list.extend(cdrom_list)
        except exception.NotFound as e:
            raise exc.HTTPNotFound(explanation=e.format_message())

        return {'host_cdrom_list': host_cdrom_list}

    def attach_cdrom(self, req, body):
        """Attach the cdrom to the local instance"""

        context = req.environ['nova.context']
        authorize(context)

        if not self.is_valid_body(body, 'attached_cdrom'):
            msg = (_("Missing required element '%s' in request body") %
                   'attached_cdrom')
            raise exc.HTTPBadRequest(explanation=msg)
        attached_cdrom = body.get("attached_cdrom", {})

        instance_id = attached_cdrom.get("instance_id")
        if not instance_id:
            msg = _("'instance_id' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        cdrom = attached_cdrom.get("cdrom")
        if not cdrom:
            msg = _("'cdrom' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        instance = common.get_instance(self.compute_api,
                                       context,
                                       instance_id)

        self.compute_api.attach_phy_cdrom(context, instance, cdrom)

        return webob.Response(status_int=200)

    def detach_cdrom(self, req, body):
        """detach the cdrom to the local instance"""

        context = req.environ['nova.context']
        authorize(context)

        if not self.is_valid_body(body, 'detached_cdrom'):
            msg = (_("Missing required element '%s' in request body") %
                   'detached_cdrom')
            raise exc.HTTPBadRequest(explanation=msg)
        detached_cdrom = body.get("detached_cdrom", {})

        instance_id = detached_cdrom.get("instance_id")
        if not instance_id:
            msg = _("'instance_id' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        cdrom = detached_cdrom.get("cdrom")
        if not cdrom:
            msg = _("'cdrom' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        instance = common.get_instance(self.compute_api,
                                       context,
                                       instance_id)

        self.compute_api.detach_phy_cdrom(context, instance, cdrom)

        return webob.Response(status_int=200)


class PhyCdrom(extensions.V21APIExtensionBase):
    """Cdrom actions between hosts and instances"""

    name = "PhyCdrom"
    alias = ALIAS
    version = 1

    def get_resources(self):
        actions = {'attach_cdrom': 'POST',
                   'detach_cdrom': 'POST'}
        resources = extensions.ResourceExtension(ALIAS,
                                                 PhyCdromController(),
                                                 collection_actions=actions)

        return [resources]

    def get_controller_extensions(self):
        return []
