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
from nova.objects import fields


@base.NovaObjectRegistry.register
class Bandwidth(base.NovaPersistentObject, base.NovaObject,
                base.NovaObjectDictCompat):
    # Version 1.0: Initial version
    VERSION = '1.0'

    fields = {
        'port_id': fields.StringField(),
        'inbound_kilo_bytes': fields.IntegerField(),
        'outbound_kilo_bytes': fields.IntegerField(),
    }

    @staticmethod
    def _from_db_object(context, bandwidth, db_bandwidth):
        for field in bandwidth.fields:
            bandwidth[field] = db_bandwidth[field]

        bandwidth._context = context
        bandwidth.obj_reset_changes()
        return bandwidth

    @base.serialize_args
    @base.remotable_classmethod
    def get_by_port_id(cls, context, port_id):
        db_bandwidth = db.bandwidth_get_by_port_id(context, port_id)
        if db_bandwidth:
            return cls._from_db_object(context, cls(), db_bandwidth)
        else:
            return None

    @base.serialize_args
    @base.remotable
    def create(self):
        updates = self.obj_get_changes()
        db_bandwidth = db.bandwidth_create(self._context, updates)
        self._from_db_object(self._context, self, db_bandwidth)

    @base.remotable
    def save(self):
        updates = self.obj_get_changes()
        if 'port_id' in updates:
            raise exception.ObjectActionError(action='save',
                                              reason='port_id is not mutable')
        db.bandwidth_update(self._context, self.port_id, updates)
        self.obj_reset_changes()
