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


class ExtVolumeSnapshot(base.NovaPersistentObject, base.NovaObject):
    # Version 1.0: Initial version
    VERSION = '1.0'
    fields = {
        'id': fields.StringField(),
        'user_id': fields.StringField(nullable=True),
        'project_id': fields.StringField(nullable=True),
        'volume_id': fields.StringField(nullable=True),
        'instance_snapshot_id': fields.StringField(nullable=True),
        'image_file_id': fields.StringField(nullable=True),
        'status': fields.StringField(nullable=True),
        'progress': fields.StringField(nullable=True),
        'volume_size': fields.IntegerField(nullable=True),
        'display_name': fields.StringField(nullable=True),
        'display_description': fields.StringField(nullable=True),
    }

    @staticmethod
    def _from_db_object(context, ext_volume_snapshot, db_ext_volume_snapshot,
                        expected_attrs=None):
        if expected_attrs is None:
            expected_attrs = []

        for field in ext_volume_snapshot.fields:
            ext_volume_snapshot[field] = db_ext_volume_snapshot[field]

        ext_volume_snapshot._context = context
        ext_volume_snapshot.obj_reset_changes()
        return ext_volume_snapshot

    @obj_base.remotable_classmethod
    def get_all(cls, context, filters=None):
        filters = filters or {}
        db_snapshots = db.ext_volume_snapshot_get_all(context, filters)
        ext_snapshots = []
        for db_snapshot in db_snapshots:
            snapshot_obj = cls._from_db_object(context, cls(context),
                                               db_snapshot)
            ext_snapshots.append(snapshot_obj)

        return ext_snapshots

    @obj_base.remotable_classmethod
    def get_by_id(cls, context, id):
        value = db.ext_volume_snapshot_get_by_id(context, id)
        return cls._from_db_object(context, cls(context), value)

    @obj_base.remotable
    def create(self, context):
        updates = self.obj_get_changes()
        value = db.ext_volume_snapshot_create(context, updates)
        return self._from_db_object(context, self, value)

    @obj_base.remotable
    def save(self, context):
        updates = self.obj_get_changes()
        if 'id' in updates:
            raise exception.ObjectActionError(action='save',
                                              reason='id is not mutable')
        db.ext_volume_snapshot_update(context, self.id, updates)
        self.obj_reset_changes()

    @base.remotable
    def destroy(self, context):
        if not self.obj_attr_is_set('id'):
            raise exception.ObjectActionError(action='destroy',
                                              reason='already destroyed')
        db.ext_volume_snapshot_destroy(context, self.id)
        delattr(self, base.get_attrname('id'))
