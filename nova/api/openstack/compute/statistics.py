# Copyright 2016 Chinac, Inc
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

from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova import compute
from nova import db
from oslo_log import log as logging


LOG = logging.getLogger(__name__)
ALIAS = "os-statistics"
authorize = extensions.os_compute_authorizer(ALIAS)


class StatisticsController(wsgi.Controller):

    def __init__(self):
        self.host_api = compute.HostAPI()
        self.compute_api = compute.API()
        super(StatisticsController, self).__init__()

    def index(self, req):
        context = req.environ['nova.context']
        authorize(context, action='index')
        elevated = context.elevated(read_deleted='no')

        instances = db.quota_usage_get_by_resource(elevated, 'instances')
        vcpu_used = db.quota_usage_get_by_resource(elevated, 'cores')
        memory_used = db.quota_usage_get_by_resource(elevated, 'ram')

        compute_nodes = db.compute_node_get_all(elevated)
        vcpu_total = sum([x['vcpus'] for x in compute_nodes])
        memory_total = sum([x['memory_mb'] for x in compute_nodes])

        data = {
            'instances': instances,
            'vcpus': {
                'used': vcpu_used,
                'total': vcpu_total
            },
            'memory': {
                'used': memory_used,
                'total': memory_total
            }
        }
        return data


class Statistics(extensions.V21APIExtensionBase):
    """Nova statistics information support."""

    name = "Statistics"
    alias = ALIAS
    version = 1

    def get_resources(self):
        controller = StatisticsController()
        resources = extensions.ResourceExtension(
            ALIAS,
            controller)
        return [resources]

    def get_controller_extensions(self):
        return []
