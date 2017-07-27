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
from oslo_log import log as logging

LOG = logging.getLogger(__name__)
ALIAS = "os-iso"
authorize = extensions.os_compute_authorizer(ALIAS)


class ISOActionController(wsgi.Controller):

    def __init__(self, *args, **kwargs):
        super(ISOActionController, self).__init__(*args, **kwargs)
        self.compute_api = compute.API()

    @wsgi.response(202)
    @extensions.expected_errors(404)
    @wsgi.action('ChangeISO')
    def change_cdrom(self, req, id, body):
        """change cdrom of instance."""
        context = req.environ['nova.context']
        authorize(context)

        iso = None
        if 'iso' in body['ChangeISO']:
            if not body['ChangeISO']['iso']:
                iso = None
            else:
                iso = body['ChangeISO']['iso']

        instance = common.get_instance(self.compute_api, context, id)
        try:
            self.compute_api.change_iso(context, instance, iso)
            self.compute_api.operation_log_about_instance(context,
                                                          'Succeeded')
        except exception.NovaException as e:
            self.compute_api.operation_log_about_instance(context, 'Failed')
            raise exc.HTTPBadRequest(explanation=e.format_message())
        return webob.Response(status_int=202)


# Note: The class name is as it has to be for this to be loaded as an
# extension--only first character capitalized.
class Iso(extensions.V21APIExtensionBase):
    """ISO support."""

    name = "Iso"
    alias = ALIAS
    version = 1

    def get_controller_extensions(self):
        controller = ISOActionController()
        extension = extensions.ControllerExtension(self, 'servers', controller)
        return [extension]

    def get_resources(self):
        return []
