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

"""The qga extension."""


import base64
from nova.api.openstack import common
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova import compute
from nova import exception
from nova.i18n import _
from oslo_log import log as logging
import webob
from webob import exc

LOG = logging.getLogger(__name__)

ALIAS = "qga"
authorize = extensions.os_compute_authorizer(ALIAS)


class QgaController(wsgi.Controller):
    def __init__(self, *args, **kwargs):
        super(QgaController, self).__init__(*args, **kwargs)
        self.compute_api = compute.API()

    @wsgi.action('qga')
    def _qga(self, req, id, body):
        context = req.environ['nova.context']
        authorize(context)

        try:
            qga = body['qga']
            cmd = qga['cmd']
            async = qga.get('async', True)
            timeout = qga.get('timeout')
        except Exception as e:
            raise exc.HTTPBadRequest(explanation=e)

        instance = common.get_instance(self.compute_api, context, id)

        try:
            r = self.compute_api.call_qga(context, instance, cmd, async,
                                          timeout)
        except exception.QgaExecuteFailure as e:
            raise exc.HTTPBadRequest(explanation=e.format_message())

        return dict(result=r)

    @wsgi.action('qgaIsLive')
    def _qga_is_live(self, req, id, body):
        context = req.environ['nova.context']
        authorize(context)

        instance = common.get_instance(self.compute_api, context, id)

        try:
            r = self.compute_api.get_qga_is_live(context, instance)
        except exception.QgaExecuteFailure as e:
            raise exc.HTTPBadRequest(explanation=e.format_message())

        return r

    @wsgi.action('setupConfigDriver')
    def _setup_config_driver(self, req, id, body):
        context = req.environ['nova.context']
        authorize(context)

        personality = body['setupConfigDriver'].get('personality')
        if not personality:
            expl = 'Missing personality'
            raise exc.HTTPBadRequest(explanation=expl)

        injected_files = self._get_injected_files(personality)

        instance = common.get_instance(self.compute_api, context, id)
        try:
            result = self.compute_api.setup_config_driver(context, instance,
                                                          injected_files)
        except exception.NovaException as e:
            raise exc.HTTPBadRequest(explanation=e.format_message())

        return {"result": result}

    @wsgi.action('ensureDetachDiskConfig')
    def _ensure_detach_disk_config(self, req, id, body):
        context = req.environ["nova.context"]
        authorize(context, 'ensureDetachDiskConfig')

        instance = common.get_instance(self.compute_api, context, id)
        try:
            self.compute_api.ensure_detach_disk_config(context, instance)
        except exception.NovaException as e:
            raise exc.HTTPBadRequest(explanation=e.format_message())

        return webob.Response(status_int=200)

    def _get_injected_files(self, personality):
        """Create a list of injected files from the personality attribute.

        At this time, injected_files must be formatted as a list of
        (file_path, file_content) pairs for compatibility with the
        underlying compute service.
        """
        injected_files = []

        for item in personality:
            try:
                path = item['path']
                contents = item['contents']
            except KeyError as key:
                expl = _('Bad personality format: missing %s') % key
                raise exc.HTTPBadRequest(explanation=expl)
            except TypeError:
                expl = _('Bad personality format')
                raise exc.HTTPBadRequest(explanation=expl)
            if self._decode_base64(contents) is None:
                expl = _('Personality content for %s cannot be decoded') % path
                raise exc.HTTPBadRequest(explanation=expl)
            injected_files.append((path, contents))
        return injected_files

    def _decode_base64(self, data):
        try:
            return base64.b64decode(data)
        except TypeError:
            return None


class Qga(extensions.V21APIExtensionBase):
    """Manage qga."""

    name = "Qga"
    alias = ALIAS
    version = 1

    def get_controller_extensions(self):
        controller = QgaController()
        extension = extensions.ControllerExtension(self, 'servers', controller)
        return [extension]

    def get_resources(self):
        return []
