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
from nova import objects
from nova.objects import base
from nova.objects import fields


@base.NovaObjectRegistry.register
class InstanceMemoryDevice(base.NovaPersistentObject, base.NovaObject,
                           base.NovaObjectDictCompat):
    # Version 1.0: Initial version
    VERSION = '1.0'

    fields = {
        'id': fields.IntegerField(),
        'model': fields.StringField(),
        'name': fields.StringField(),
        'instance_uuid': fields.StringField(),
        'target_size': fields.StringField(),
        'target_node': fields.StringField(),
    }

    @staticmethod
    def _from_db_object(context, memorydevice, db_memorydevice):
        # NOTE(danms): These are identical right now
        for field in memorydevice.fields:
            memorydevice[field] = db_memorydevice[field]
        memorydevice._context = context
        memorydevice.obj_reset_changes()
        return memorydevice

    @base.remotable_classmethod
    def create(cls, context, mem_dev):
        db_memorydevice = db.instance_memory_devices_create(context, mem_dev)
        return cls._from_db_object(context, cls(), db_memorydevice)

    @base.remotable_classmethod
    def get_by_name_uuid(cls, context, name, instance_uuid):
        db_memorydevice = db.instance_memory_devices_get_by_name_uuid(
            context, name, instance_uuid)
        return cls._from_db_object(context, cls(), db_memorydevice)

    @base.remotable_classmethod
    def destroy_by_name_uuid(cls, context, name, instance_uuid):
        db.instance_memory_devices_destroy_by_name_uuid(
            context, name, instance_uuid)

    @base.remotable_classmethod
    def get(cls, context, memorydevice_id):
        db_memorydevice = db.instance_memory_devices_get(
            context, memorydevice_id)
        return cls._from_db_object(context, cls(), db_memorydevice)

    @base.remotable_classmethod
    def get_by_name(cls, context, project_id, group_name):
        db_memorydevice = db.instance_memory_devices_get_by_name(context,
                                                                 project_id,
                                                                 group_name)
        return cls._from_db_object(context, cls(), db_memorydevice)

    @base.remotable
    def in_use(self, context):
        return db.instance_memory_devices_in_use(context, self.id)

    @base.remotable
    def save(self, context):
        updates = self.obj_get_changes()
        if updates:
            db_memorydevice = db.instance_memory_devices_update(
                context, self.id, updates)
            self._from_db_object(context, self, db_memorydevice)
        self.obj_reset_changes()

    @base.remotable
    def refresh(self, context):
        self._from_db_object(context, self,
                             db.instance_memory_devices_get(context, self.id))


@base.NovaObjectRegistry.register
class InstanceMemoryDeviceList(base.ObjectListBase, base.NovaObject):
    # Version 1.0: Initial version
    VERSION = '1.0'

    fields = {
        'objects': fields.ListOfObjectsField('InstanceMemoryDevice'),
    }
    child_versions = {
        '1.0': '1.0',
    }

    def __init__(self, *args, **kwargs):
        super(InstanceMemoryDeviceList, self).__init__(*args, **kwargs)
        self.objects = []
        self.obj_reset_changes()

    @base.remotable_classmethod
    def get_all(cls, context):
        devices = db.instance_memory_devices_get_all(context)
        return base.obj_make_list(context, cls(context),
                                  objects.InstanceMemoryDevice, devices)

    @base.remotable_classmethod
    def get_by_project(cls, context, project_id):
        devices = db.instance_memory_devices_get_by_project(
            context, project_id)
        return base.obj_make_list(context, cls(context),
                                  objects.InstanceMemoryDevice, devices)

    @base.remotable_classmethod
    def get_by_instance_uuid(cls, context, instance_uuid):
        devices = db.instance_memory_devices_get_by_instance_uuid(
            context, instance_uuid)
        return base.obj_make_list(context, cls(context),
                                  objects.InstanceMemoryDevice, devices)


def make_memorydevice_list(instance_memory_devices):
    """A helper to make memory device objects from a list of names.

    Note that this does not make them save-able or have the rest of the
    attributes they would normally have, but provides a quick way to fill,
    for example, an instance object during create.
    """
    memorydevices = objects.InstanceMemoryDeviceList()
    memorydevices.objects = []
    for name in instance_memory_devices:
        memorydevice = objects.InstanceMemoryDevice()
        memorydevice.name = name
        memorydevices.objects.append(memorydevice)
    return memorydevices
