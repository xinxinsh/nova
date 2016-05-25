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


class ExtImageFile(base.NovaPersistentObject, base.NovaObject):
    # Version 1.0: Initial version
    VERSION = '1.0'
    fields = {
        'id': fields.StringField(),
        'host': fields.StringField(nullable=False),
        'parent': fields.StringField(nullable=False),
        'root': fields.StringField(nullable=False),
        'active': fields.BooleanField(default=False),
        'format': fields.StringField(nullable=False),
    }

    @staticmethod
    def _from_db_object(context, ext_image_file, db_ext_image_file,
                        expected_attrs=None):
        if expected_attrs is None:
            expected_attrs = []

        for field in ext_image_file.fields:
            ext_image_file[field] = db_ext_image_file[field]

        ext_image_file._context = context
        ext_image_file.obj_reset_changes()
        return ext_image_file

    @obj_base.remotable_classmethod
    def get_by_id(cls, context, id):
        db_image_file = db.ext_image_file_get_by_id(context, id)
        return cls._from_db_object(context, cls(context), db_image_file)

    @obj_base.remotable
    def get_by_root(self, context):
        """read_deleted=yes"""
        db_image_files = db.ext_image_file_get_by_root(context, self.root)
        image_files = []
        for db_image_file in db_image_files:
            image_file = self._from_db_object(context, ExtImageFile(context),
                                              db_image_file)
            image_files.append(image_file)
        return image_files

    @obj_base.remotable
    def get_all_parents(self, context):
        db_parents = db.ext_image_file_get_all_parents(context, self.root)

        parents = []
        for db_image_file in db_parents:
            if db_image_file['id'] == self.id:
                image_file = self
            else:
                image_file = self._from_db_object(context,
                                                  ExtImageFile(context),
                                                  db_image_file)
            parents.append(image_file)
        return parents

    @obj_base.remotable
    def get_chain_depth(self, context):
        db_parents = db.ext_image_file_get_all_parents(context, self.root)
        depth = 1

        parent = self.parent
        if self.id == parent:
            return depth

        map_id = {}
        for db_image_file in db_parents:
            value = dict(id=db_image_file['id'],
                         parent=db_image_file['parent'],
                         root=db_image_file['root'])
            map_id[db_image_file['id']] = value

        while True:
            if parent in map_id:
                depth += 1
                _this = map_id[parent]
                parent = _this['parent']
                if parent != _this['id']:
                    continue
                # Make sure the parent of root image is itself
                if parent != _this['root']:
                    raise exception.ExtImageFileDepthFailure(
                            error='The Parent of root image is error')
                return depth
            else:
                raise exception.ExtImageFileDepthFailure(
                    error='Parent does not exist')

    @obj_base.remotable
    def create(self, context):
        updates = self.obj_get_changes()
        db_image_file = db.ext_image_file_create(context, updates)
        self._from_db_object(context, self, db_image_file)

    @obj_base.remotable
    def save(self, context):
        updates = self.obj_get_changes()
        if 'id' in updates:
            raise exception.ObjectActionError(action='save',
                                              reason='id is not mutable')
        db.ext_image_file_update(context, self.id, updates)
        self.obj_reset_changes()

    @base.remotable
    def destroy(self, context):
        if not self.obj_attr_is_set('id'):
            raise exception.ObjectActionError(action='destroy',
                                              reason='already destroyed')
        db.ext_image_file_destroy(context, self.id)
        delattr(self, base.get_attrname('id'))
