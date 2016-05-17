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
from nova.compute import power_state
from nova.compute import vm_states
from nova import exception
from nova.i18n import _
from oslo_log import log as logging
import webob
from webob import exc

LOG = logging.getLogger(__name__)

ALIAS = "os-ssh_key_action"
authorize = extensions.os_compute_authorizer(ALIAS)


class SshKeyActionController(wsgi.Controller):
    def __init__(self, *args, **kwargs):
        super(SshKeyActionController, self).__init__(*args, **kwargs)
        self.compute_api = compute.API()

    @wsgi.response(200)
    @extensions.expected_errors((404, 409))
    @wsgi.action('clearSshPublicKey')
    def _clearSshPublicKey(self, req, id, body):
        """Delete the public ssh key of the given instance"""

        context = req.environ['nova.context']
        authorize(context)

        instance = common.get_instance(self.compute_api,
                                       context,
                                       id)

        if(instance['vm_state'] != vm_states.ACTIVE
           or instance['power_state'] != power_state.RUNNING
           or instance['task_state'] is not None):
            raise exception.InstanceNotReady(instance_id=instance['uuid'])

        cmd = {
            "execute": "guest-system-cmd",
            "arguments": {
                "cmd": "/bin/rm -f /root/.ssh/authorized_keys"
            }
        }
        async = False
        timeout = 120

        try:
            self.compute_api.call_qga(context, instance, cmd, async,
                                      timeout)
        except exception.QgaExecuteFailure as e:
            raise exc.HTTPBadRequest(explanation=e.format_message())

        return webob.Response(status_int=200)

    @wsgi.response(200)
    @extensions.expected_errors((404, 409))
    @wsgi.action('replaceSshPublicKey')
    def _replaceSshPublicKey(self, req, id, body):
        """binding the public ssh key to the given instance"""

        context = req.environ['nova.context']
        authorize(context)

        if not self.is_valid_body(body, 'replaceSshPublicKey'):
            msg = (_("Missing required element '%s' in request body") %
                   'replaceSshPublicKey')
            raise exc.HTTPBadRequest(explanation=msg)
        SshPublicKey = body.get("replaceSshPublicKey", {})

        public_key_content = SshPublicKey.get("key")
        if not public_key_content:
            msg = _("'key' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        instance = common.get_instance(self.compute_api,
                                       context,
                                       id)

        if(instance['vm_state'] != vm_states.ACTIVE
           or instance['power_state'] != power_state.RUNNING
           or instance['task_state'] is not None):
            raise exception.InstanceNotReady(instance_id=instance['uuid'])

        cmd = {
            "execute": "guest-inject-files",
            "arguments": {
                "files": [
                    {
                        "path": "/root/.ssh/authorized_keys",
                        "content": public_key_content
                    }
                ]
            }
        }
        async = False
        timeout = 120

        try:
            self.compute_api.call_qga(context, instance, cmd, async,
                                      timeout)
        except exception.QgaExecuteFailure as e:
            raise exc.HTTPBadRequest(explanation=e.format_message())

        return webob.Response(status_int=200)


class SshKeyAction(extensions.V21APIExtensionBase):
    """Enables Ssh key action to vms."""

    name = "SshKeyAction"
    alias = ALIAS
    version = 1

    def get_controller_extensions(self):
        controller = SshKeyActionController()
        extension = extensions.ControllerExtension(self, 'servers', controller)
        return [extension]

    def get_resources(self):
        return []
