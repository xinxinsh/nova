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

import webob
from webob import exc

from nova.api.openstack import common
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova import compute
from nova import exception
from nova import servicegroup
from oslo_log import log as logging

LOG = logging.getLogger(__name__)
ALIAS = "cdrom"
authorize = extensions.os_compute_authorizer(ALIAS)


class CdromController(wsgi.Controller):
    def __init__(self):
        self.host_api = compute.HostAPI()
        self.servicegroup_api = servicegroup.API()
        self.compute_api = compute.API()
        super(CdromController, self).__init__()

    def index(self, req):
        """Return a list of iso file."""
        context = req.environ['nova.context']
        authorize(context)

        filters = {'disabled': False, 'binary': 'nova-compute'}
        services = self.host_api.service_get_all(context, filters)
        iso = []
        for s in services:
            if self.servicegroup_api.service_is_up(s):
                iso = self.compute_api.get_cdroms(context, s['host'])
                break
        self.compute_api.operation_log_about_instance(context, 'Succeeded')
        return dict(isoList=iso)


class CdromActionController(wsgi.Controller):
    def __init__(self, *args, **kwargs):
        super(CdromActionController, self).__init__(*args, **kwargs)
        self.compute_api = compute.API()

    @wsgi.response(202)
    @extensions.expected_errors(404)
    @wsgi.action('listMountedCdrom')
    def list_cdrom(self, req, id, body):
        """List mounted iso of instance."""
        context = req.environ['nova.context']
        authorize(context)

        instance = common.get_instance(self.compute_api, context, id)
        mounted = self.compute_api.list_mounted_cdrom(context, instance)
        self.compute_api.operation_log_about_instance(context, 'Succeeded')
        return dict(mountedIso=mounted)

    @wsgi.response(202)
    @extensions.expected_errors(404)
    @wsgi.action('changeCdrom')
    def change_cdrom(self, req, id, body):
        """change cdrom of instance."""
        context = req.environ['nova.context']
        authorize(context)

        iso_name = None
        if 'isoName' in body['changeCdrom']:
            if not body['changeCdrom']['isoName']:
                iso_name = None
            else:
                iso_name = body['changeCdrom']['isoName']

        instance = common.get_instance(self.compute_api, context, id)
        try:
            self.compute_api.change_cdrom(context, instance, iso_name)
            self.compute_api.operation_log_about_instance(context, 'Succeeded')
        except exception.NovaException as e:
            self.compute_api.operation_log_about_instance(context, 'Failed')
            raise exc.HTTPBadRequest(explanation=e.format_message())
        return webob.Response(status_int=202)


# Note: The class name is as it has to be for this to be loaded as an
# extension--only first character capitalized.
class Cdrom(extensions.V21APIExtensionBase):
    """Cdrom support."""

    name = "Cdrom"
    alias = "cdrom"
    version = 1

    def get_resources(self):
        resources = extensions.ResourceExtension(
            'os-cdrom', CdromController(), member_actions={'index': 'GET'})

        return [resources]

    def get_controller_extensions(self):
        controller = CdromActionController()
        extension = extensions.ControllerExtension(self, 'servers', controller)
        return [extension]
