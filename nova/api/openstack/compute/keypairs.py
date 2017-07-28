# Copyright 2011 OpenStack Foundation
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

"""Keypair management extension."""

import webob
import webob.exc

from nova.api.openstack import common
from nova.api.openstack.compute.schemas import keypairs
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova.api import validation
from nova import compute
from nova.compute import api as compute_api
from nova import exception
from nova.i18n import _
from nova.objects import keypair as keypair_obj


ALIAS = 'os-keypairs'
authorize = extensions.os_compute_authorizer(ALIAS)
soft_authorize = extensions.os_compute_soft_authorizer(ALIAS)


class KeypairController(wsgi.Controller):

    """Keypair API controller for the OpenStack API."""
    def __init__(self):
        self.api = compute_api.KeypairAPI()
        self.compute_api = compute.API(skip_policy_check=True)

    def _filter_keypair(self, keypair, **attrs):
        # TODO(claudiub): After v2 and v2.1 is no longer supported,
        # keypair.type can be added to the clean dict below
        clean = {
            'name': keypair.name,
            'public_key': keypair.public_key,
            'fingerprint': keypair.fingerprint,
            }
        for attr in attrs:
            clean[attr] = keypair[attr]
        return clean

    @wsgi.Controller.api_version("2.10")
    @wsgi.response(201)
    @extensions.expected_errors((400, 403, 409))
    @validation.schema(keypairs.create_v210)
    def create(self, req, body):
        """Create or import keypair.

        A policy check restricts users from creating keys for other users

        params: keypair object with:
            name (required) - string
            public_key (optional) - string
            type (optional) - string
            user_id (optional) - string
        """
        # handle optional user-id for admin only
        user_id = body['keypair'].get('user_id')
        return self._create(req, body, type=True, user_id=user_id)

    @wsgi.Controller.api_version("2.2", "2.9")  # noqa
    @wsgi.response(201)
    @extensions.expected_errors((400, 403, 409))
    @validation.schema(keypairs.create_v22)
    def create(self, req, body):
        """Create or import keypair.

        Sending name will generate a key and return private_key
        and fingerprint.

        Keypair will have the type ssh or x509, specified by type.

        You can send a public_key to add an existing ssh/x509 key.

        params: keypair object with:
            name (required) - string
            public_key (optional) - string
            type (optional) - string
        """
        return self._create(req, body, type=True)

    @wsgi.Controller.api_version("2.1", "2.1")  # noqa
    @extensions.expected_errors((400, 403, 409))
    @validation.schema(keypairs.create_v20, "2.0", "2.0")
    @validation.schema(keypairs.create, "2.1", "2.1")
    def create(self, req, body):
        """Create or import keypair.

        Sending name will generate a key and return private_key
        and fingerprint.

        You can send a public_key to add an existing ssh key.

        params: keypair object with:
            name (required) - string
            public_key (optional) - string
        """
        return self._create(req, body)

    def _create(self, req, body, user_id=None, **keypair_filters):
        context = req.environ['nova.context']
        params = body['keypair']
        name = common.normalize_name(params['name'])
        key_type = params.get('type', keypair_obj.KEYPAIR_TYPE_SSH)
        user_id = user_id or context.user_id
        authorize(context, action='create',
                           target={'user_id': user_id,
                                   'project_id': context.project_id})

        action_result = False
        try:
            if 'public_key' in params:
                keypair = self.api.import_key_pair(context,
                                              user_id, name,
                                              params['public_key'], key_type)
                keypair = self._filter_keypair(keypair, user_id=True,
                                               **keypair_filters)
            else:
                keypair, private_key = self.api.create_key_pair(
                    context, user_id, name, key_type)
                keypair = self._filter_keypair(keypair, user_id=True,
                                               **keypair_filters)
                keypair['private_key'] = private_key

            action_result = True
            self.compute_api.operation_log_about_instance(context,
                                                          'Succeeded')
            return {'keypair': keypair}

        except exception.KeypairLimitExceeded:
            msg = _("Quota exceeded, too many key pairs.")
            raise webob.exc.HTTPForbidden(explanation=msg)
        except exception.InvalidKeypair as exc:
            raise webob.exc.HTTPBadRequest(explanation=exc.format_message())
        except exception.KeyPairExists as exc:
            raise webob.exc.HTTPConflict(explanation=exc.format_message())
        finally:
            if not action_result:
                self.compute_api.operation_log_about_instance(context,
                                                              'Failed')

    @wsgi.Controller.api_version("2.1", "2.1")
    @wsgi.response(202)
    @extensions.expected_errors(404)
    def delete(self, req, id):
        self._delete(req, id)

    @wsgi.Controller.api_version("2.2", "2.9")    # noqa
    @wsgi.response(204)
    @extensions.expected_errors(404)
    def delete(self, req, id):
        self._delete(req, id)

    @wsgi.Controller.api_version("2.10")    # noqa
    @wsgi.response(204)
    @extensions.expected_errors(404)
    def delete(self, req, id):
        # handle optional user-id for admin only
        user_id = self._get_user_id(req)
        self._delete(req, id, user_id=user_id)

    def _delete(self, req, id, user_id=None):
        """Delete a keypair with a given name."""
        context = req.environ['nova.context']
        # handle optional user-id for admin only
        user_id = user_id or context.user_id
        authorize(context, action='delete',
                  target={'user_id': user_id,
                          'project_id': context.project_id})
        try:
            self.api.delete_key_pair(context, user_id, id)
        except exception.KeypairNotFound as exc:
            self.compute_api.operation_log_about_instance(context, 'Failed')
            raise webob.exc.HTTPNotFound(explanation=exc.format_message())

        self.compute_api.operation_log_about_instance(context, 'Succeeded')

    def _get_user_id(self, req):
        if 'user_id' in req.GET.keys():
            user_id = req.GET.getall('user_id')[0]
            return user_id

    @wsgi.Controller.api_version("2.10")
    @extensions.expected_errors(404)
    def show(self, req, id):
        # handle optional user-id for admin only
        user_id = self._get_user_id(req)
        return self._show(req, id, type=True, user_id=user_id)

    @wsgi.Controller.api_version("2.2", "2.9")  # noqa
    @extensions.expected_errors(404)
    def show(self, req, id):
        return self._show(req, id, type=True)

    @wsgi.Controller.api_version("2.1", "2.1")  # noqa
    @extensions.expected_errors(404)
    def show(self, req, id):
        return self._show(req, id)

    def _show(self, req, id, user_id=None, **keypair_filters):
        """Return data for the given key name."""
        context = req.environ['nova.context']
        user_id = user_id or context.user_id
        authorize(context, action='show',
                  target={'user_id': user_id,
                          'project_id': context.project_id})

        try:
            # The return object needs to be a dict in order to pop the 'type'
            # field, if the api_version < 2.2.
            keypair = self.api.get_key_pair(context, user_id, id)
            keypair = self._filter_keypair(keypair, created_at=True,
                                           deleted=True, deleted_at=True,
                                           id=True, user_id=True,
                                           updated_at=True, **keypair_filters)
        except exception.KeypairNotFound as exc:
            raise webob.exc.HTTPNotFound(explanation=exc.format_message())
        # TODO(oomichi): It is necessary to filter a response of keypair with
        # _filter_keypair() when v2.1+microversions for implementing consistent
        # behaviors in this keypair resource.
        return {'keypair': keypair}

    @wsgi.Controller.api_version("2.10")
    @extensions.expected_errors(())
    def index(self, req):
        # handle optional user-id for admin only
        user_id = self._get_user_id(req)
        return self._index(req, type=True, user_id=user_id)

    @wsgi.Controller.api_version("2.2", "2.9")  # noqa
    @extensions.expected_errors(())
    def index(self, req):
        return self._index(req, type=True)

    @wsgi.Controller.api_version("2.1", "2.1")  # noqa
    @extensions.expected_errors(())
    def index(self, req):
        return self._index(req)

    def _index(self, req, user_id=None, **keypair_filters):
        """List of keypairs for a user."""
        context = req.environ['nova.context']
        user_id = user_id or context.user_id
        authorize(context, action='index',
                           target={'user_id': user_id,
                                   'project_id': context.project_id})
        key_pairs = self.api.get_key_pairs(context, user_id)
        rval = []
        for key_pair in key_pairs:
            rval.append({'keypair': self._filter_keypair(key_pair,
                                                         **keypair_filters)})

        return {'keypairs': rval}


class Controller(wsgi.Controller):

    def _add_key_name(self, req, servers):
        for server in servers:
            db_server = req.get_db_instance(server['id'])
            # server['id'] is guaranteed to be in the cache due to
            # the core API adding it in its 'show'/'detail' methods.
            server['key_name'] = db_server['key_name']

    def _show(self, req, resp_obj):
        if 'server' in resp_obj.obj:
            server = resp_obj.obj['server']
            self._add_key_name(req, [server])

    @wsgi.extends
    def show(self, req, resp_obj, id):
        context = req.environ['nova.context']
        if soft_authorize(context):
            self._show(req, resp_obj)

    @wsgi.extends
    def detail(self, req, resp_obj):
        context = req.environ['nova.context']
        if 'servers' in resp_obj.obj and soft_authorize(context):
            servers = resp_obj.obj['servers']
            self._add_key_name(req, servers)


class Keypairs(extensions.V21APIExtensionBase):
    """Keypair Support."""

    name = "Keypairs"
    alias = ALIAS
    version = 1

    def get_resources(self):
        resources = [
            extensions.ResourceExtension(ALIAS,
                                         KeypairController())]
        return resources

    def get_controller_extensions(self):
        controller = Controller()
        extension = extensions.ControllerExtension(self, 'servers', controller)
        return [extension]

    # use nova.api.extensions.server.extensions entry point to modify
    # server create kwargs
    # NOTE(gmann): This function is not supposed to use 'body_deprecated_param'
    # parameter as this is placed to handle scheduler_hint extension for V2.1.
    def server_create(self, server_dict, create_kwargs, body_deprecated_param):
        # NOTE(alex_xu): The v2.1 API compat mode, we strip the spaces for
        # keypair create. But we didn't strip spaces at here for
        # backward-compatible some users already created keypair and name with
        # leading/trailing spaces by legacy v2 API.
        create_kwargs['key_name'] = server_dict.get('key_name')

    def get_server_create_schema(self, version):
        if version == '2.0':
            return keypairs.server_create_v20
        else:
            return keypairs.server_create
