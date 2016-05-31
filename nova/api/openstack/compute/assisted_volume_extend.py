# Copyright 2016 Chinac Corp.
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

"""The Assisted volume extend extension."""

from oslo_log import log as logging
from webob import exc

from nova.api.openstack.compute.schemas import assisted_volume_extend
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova.api import validation
from nova import compute
from nova import exception
from nova.i18n import _LI


LOG = logging.getLogger(__name__)
ALIAS = 'os-assisted-volume-extend'
authorize = extensions.os_compute_authorizer(ALIAS)


class AssistedVolumeExtendController(wsgi.Controller):
    """The Assisted volume extend API controller for the OpenStack API."""

    def __init__(self):
        self.compute_api = compute.API(skip_policy_check=True)
        super(AssistedVolumeExtendController, self).__init__()

    @extensions.expected_errors(400)
    @validation.schema(assisted_volume_extend.volume_extend)
    def create(self, req, body):
        """Online extend a volume."""
        context = req.environ['nova.context']
        authorize(context, action='create')

        volume = body['volume']
        extend_info = volume['extend_info']
        volume_id = volume['volume_id']

        LOG.info(_LI("Online extend for volume %s"), volume_id,
                  context=context)
        try:
            return self.compute_api.volume_online_extend(context, volume_id,
                                                         extend_info)
        except (exception.VolumeBDMNotFound,
                exception.InvalidVolume) as error:
            raise exc.HTTPBadRequest(explanation=error.format_message())


class AssistedVolumeExtend(extensions.V21APIExtensionBase):
    """Online extend a volume."""

    name = "AssistedVolumeExtend"
    alias = ALIAS
    version = 1

    def get_resources(self):
        res = [extensions.ResourceExtension(ALIAS,
                                    AssistedVolumeExtendController())]
        return res

    def get_controller_extensions(self):
        """It's an abstract function V21APIExtensionBase and the extension
        will not be loaded without it.
        """
        return []
