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

"""The interface bandwidth extension."""

from nova.api.openstack import common
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova import compute
from nova.i18n import _
from oslo_log import log as logging
import webob
from webob import exc


LOG = logging.getLogger(__name__)

ALIAS = "bandwidth"
authorize = extensions.os_compute_authorizer(ALIAS)


class BandwidthController(wsgi.Controller):
    def __init__(self, *args, **kwargs):
        super(BandwidthController, self).__init__(*args, **kwargs)
        self.compute_api = compute.API()

    @wsgi.action('setInterfaceBandwidth')
    def _set_interface_bandwidth(self, req, id, body):
        """Set bandwidth of instance virtual interface."""
        context = req.environ['nova.context']
        authorize(context)

        # Validate the input entity
        if 'portId' not in body['setInterfaceBandwidth']:
            msg = _("Missing 'portId' argument for setInterfaceBandwidth")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        port_id = body['setInterfaceBandwidth']['portId']

        inbound_kilo_bytes = body['setInterfaceBandwidth'].get(
            'inboundKiloBytes', None)
        outbound_kilo_bytes = body['setInterfaceBandwidth'].get(
            'outboundKiloBytes', None)

        instance = common.get_instance(self.compute_api, context, id)

        self.compute_api.set_interface_bandwidth(
            context,
            instance,
            port_id,
            inbound_kilo_bytes,
            outbound_kilo_bytes)

        return webob.Response(status_int=202)


# Note: The class name is as it has to be for this to be loaded as an
# extension--only first character capitalized.
class Bandwidth(extensions.ExtensionDescriptor):
    """Interface bandwidth support."""

    name = "Bandwidth"
    alias = ALIAS
    version = 1

    def get_controller_extensions(self):
        controller = BandwidthController()
        extension = extensions.ControllerExtension(self, 'servers', controller)
        return [extension]

    def get_resources(self):
        return []
