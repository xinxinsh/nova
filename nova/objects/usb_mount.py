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
from nova.objects import fields


@base.NovaObjectRegistry.register
class UsbMount(base.NovaPersistentObject, base.NovaObject,
          base.NovaObjectDictCompat):
    # Version 1.0: Initial version
    VERSION = '1.0'

    fields = {
        'usb_vid': fields.StringField(),
        'usb_pid': fields.StringField(),
        'usb_port': fields.StringField(nullable=True),
        'src_host_name': fields.StringField(),
        'dst_host_name': fields.StringField(),
        'instance_id': fields.StringField(nullable=True),
        'mounted': fields.BooleanField(),
        'is_auto': fields.BooleanField(),
    }

    @staticmethod
    def _from_db_object(context, usb_mount, db_usb_mount):
        for field in usb_mount.fields:
            usb_mount[field] = db_usb_mount[field]

        usb_mount._context = context
        usb_mount.obj_reset_changes()
        return usb_mount

    @base.serialize_args
    @base.remotable_classmethod
    def get_by_vid_pid(cls, context, usb_vid, usb_pid):
        db_usb_mount = db.usb_mount_get_by_vid_pid(context, usb_vid, usb_pid)
        if db_usb_mount:
            return cls._from_db_object(context, cls(), db_usb_mount)
        else:
            return None

    @base.serialize_args
    @base.remotable
    def create(self):
        updates = self.obj_get_changes()
        db_usb_mount = db.usb_mount_create(self._context, updates)
        self._from_db_object(self._context, self, db_usb_mount)

    @base.remotable
    def save(self):
        updates = self.obj_get_changes()
        if 'usb_vid' in updates or 'usb_pid' in updates:
            raise exception.ObjectActionError(
                action='save',
                reason='usb_vid or usb_pid is not mutable')
        db.usb_mount_update(self._context, self.usb_vid, self.usb_pid, updates)
        self.obj_reset_changes()


@base.NovaObjectRegistry.register
class UsbMountList(base.ObjectListBase, base.NovaObject):
    # Version 1.0: Initial version
    VERSION = '1.0'

    fields = {
        'objects': fields.ListOfObjectsField('UsbMount'),
        }

    @base.remotable_classmethod
    def get_by_instance_id(cls, context, instance_id):
        db_usbs = db.usb_mount_get_by_instance_id(
            context, instance_id)
        return base.obj_make_list(context, cls(context), objects.UsbMount,
                                  db_usbs)
