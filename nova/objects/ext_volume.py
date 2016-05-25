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
from nova import objects
from nova.objects import base
from nova.objects import base as obj_base
from nova.objects import fields


class ExtVolume(base.NovaPersistentObject, base.NovaObject):
    # Version 1.0: Initial version
    VERSION = '1.0'
    fields = {
        'id': fields.StringField(),
        'user_id': fields.StringField(),
        'project_id': fields.StringField(),
        'glance_image_id': fields.StringField(nullable=True),
        'image_file_id': fields.StringField(nullable=True),
        'qos_id': fields.StringField(nullable=True),
        'host': fields.StringField(),
        'size': fields.IntegerField(nullable=True),
        'availability_zone': fields.StringField(),
        'bootable': fields.BooleanField(default=False),
        'instance_uuid': fields.UUIDField(nullable=True),
        'mountpoint': fields.StringField(nullable=True),
        'attach_time': fields.DateTimeField(nullable=True),
        'status': fields.StringField(),
        'migration_status': fields.StringField(nullable=True),
        'attach_status': fields.StringField(),
        'scheduled_at': fields.DateTimeField(nullable=True),
        'launched_at': fields.DateTimeField(nullable=True),
        'terminated_at': fields.DateTimeField(nullable=True),
        'display_name': fields.StringField(nullable=True),
        'display_description': fields.StringField(nullable=True),
        'image_file': fields.ObjectField('ExtImageFile', nullable=True),
        'qos': fields.ObjectField('ExtVolumeQos', nullable=True),
    }

    @staticmethod
    def _from_db_object(context, ext_volume, db_volume, expected_attrs=None):
        if expected_attrs is None:
            expected_attrs = []
        for field in ext_volume.fields:
            if field in ('image_file', 'qos'):
                continue
            ext_volume[field] = db_volume[field]
        ext_volume.image_file = None
        if 'image_file' in expected_attrs and db_volume['image_file']:
            ext_volume.image_file = objects.ExtImageFile._from_db_object(
                context, objects.ExtImageFile(context),
                db_volume['image_file'])
        ext_volume.qos = None
        if 'qos' in expected_attrs and db_volume['qos']:
            ext_volume.qos = objects.ExtVolumeQos._from_db_object(
                context, objects.ExtVolumeQos(context), db_volume['qos'])

        ext_volume._context = context
        ext_volume.obj_reset_changes()

        return ext_volume

    @obj_base.remotable_classmethod
    def get_by_id(cls, context, id, expected_attrs=None):
        if expected_attrs is None:
            expected_attrs = []
        get_image_file = 'image_file' in expected_attrs
        get_qos_specs = 'qos' in expected_attrs
        db_volume = db.ext_volume_get_by_id(context, id, get_image_file,
                                            get_qos_specs)
        return cls._from_db_object(context, cls(context), db_volume,
                                   expected_attrs)

    @obj_base.remotable_classmethod
    def get_all(cls, context, filters=None, expected_attrs=None):
        if expected_attrs is None:
            expected_attrs = []
        get_image_file = 'image_file' in expected_attrs

        filters = filters or {}

        db_volumes = db.ext_volume_get_all(context, get_image_file, filters)

        ext_volumes = []
        for db_volume in db_volumes:
            volume_obj = cls._from_db_object(context, cls(context), db_volume,
                                             expected_attrs)
            ext_volumes.append(volume_obj)

        return ext_volumes

    @obj_base.remotable
    def create(self, context):
        updates = self.obj_get_changes()
        db_volume = db.ext_volume_create(context, updates)
        return self._from_db_object(context, self, db_volume)

    @obj_base.remotable
    def save(self, context):
        updates = self.obj_get_changes()
        if 'id' in updates:
            raise exception.ObjectActionError(action='save',
                                              reason='id is not mutable')
        db.ext_volume_update(context, self.id, updates)
        self.obj_reset_changes()

    @base.remotable
    def destroy(self, context):
        if not self.obj_attr_is_set('id'):
            raise exception.ObjectActionError(action='destroy',
                                              reason='already destroyed')
        db.ext_volume_destroy(context, self.id)
        delattr(self, base.get_attrname('id'))
