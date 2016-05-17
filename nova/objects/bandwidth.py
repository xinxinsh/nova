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

from nova import db
from nova import exception
from nova.objects import base
from nova.objects import base as obj_base
from nova.objects import fields


class Bandwidth(base.NovaPersistentObject, base.NovaObject):
    # Version 1.0: Initial version
    VERSION = '1.0'
    fields = {
        'port_id': fields.StringField(),
        'inbound_kilo_bytes': fields.IntegerField(nullable=True),
        'outbound_kilo_bytes': fields.IntegerField(nullable=True),
    }

    @staticmethod
    def _from_db_object(context, bandwidth, db_bandwidth, expected_attrs=None):
        if expected_attrs is None:
            expected_attrs = []

        for field in bandwidth.fields:
            bandwidth[field] = db_bandwidth[field]

        bandwidth._context = context
        bandwidth.obj_reset_changes()
        return bandwidth

    @obj_base.remotable_classmethod
    def get_by_port_id(cls, context, port_id):
        db_bandwidth = db.bandwidth_get_by_port_id(context, port_id)
        return cls._from_db_object(context, cls(), db_bandwidth)

    @obj_base.remotable
    def create(self, context):
        updates = self.obj_get_changes()
        db_bandwidth = db.bandwidth_create(context, updates)
        self._from_db_object(context, self, db_bandwidth)

    @obj_base.remotable
    def save(self, context):
        updates = self.obj_get_changes()
        if 'port_id' in updates:
            raise exception.ObjectActionError(action='save',
                                              reason='port_id is not mutable')
        db.bandwidth_update(context, self.port_id, updates)
        self.obj_reset_changes()
