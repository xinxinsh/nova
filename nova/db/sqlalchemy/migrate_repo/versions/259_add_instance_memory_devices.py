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

from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table


def upgrade(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine

    columns = [
        (('created_at', DateTime), {}),
        (('updated_at', DateTime), {}),
        (('deleted_at', DateTime), {}),
        (('deleted', Integer), {}),
        (('id', Integer), dict(primary_key=True, nullable=False,
                               autoincrement=True)),
        (('model', String(length=255)), dict(nullable=False)),
        (('name', String(length=255)), dict(nullable=False)),
        (('instance_uuid', String(length=36)), dict(nullable=False)),
        (('target_size', String(length=255)), dict(nullable=False)),
        (('target_node', String(length=255)), dict(nullable=False)),
        (('source_pagesize', String(length=255)), dict(nullable=True)),
        (('source_nodemask', String(length=255)), dict(nullable=True)),
    ]
    for prefix in ('', 'shadow_'):
        basename = prefix + 'instance_memory_devices'
        if migrate_engine.has_table(basename):
            continue
        _columns = tuple([Column(*args, **kwargs)
                          for args, kwargs in columns])
        table = Table(basename, meta, *_columns, mysql_engine='InnoDB',
                      mysql_charset='utf8')
        table.create()

        # Index
        instance_uuid_index = Index(basename + '_instance_uuid_deleted',
                                    table.c.instance_uuid, table.c.deleted)
        instance_uuid_index.create(migrate_engine)


def downgrade(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine

    for prefix in ('', 'shadow_'):
        table_name = prefix + 'instance_memory_devices'
        if migrate_engine.has_table(table_name):
            instance_memory_devices = Table(table_name, meta, autoload=True)
            instance_memory_devices.drop()
