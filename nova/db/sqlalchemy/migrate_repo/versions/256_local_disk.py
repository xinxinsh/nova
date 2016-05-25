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
from sqlalchemy import MetaData
from sqlalchemy import Table
from sqlalchemy import Integer, DateTime, String, Boolean


def upgrade(engine):
    meta = MetaData(bind=engine)

    ext_image_files = Table('ext_image_files', meta,
        Column('created_at', DateTime),
        Column('updated_at', DateTime),
        Column('deleted_at', DateTime),
        Column('id', String(36), primary_key=True, nullable=False),
        Column('host', String(length=255)),
        Column('parent', String(length=36)),
        Column('root', String(length=36)),
        Column('active', Boolean),
        Column('format', String(16)),
        Column('deleted', String(36)),
        mysql_engine='InnoDB',
        mysql_charset='utf8',
    )

    ext_volumes = Table('ext_volumes', meta,
        Column('created_at', DateTime),
        Column('updated_at', DateTime),
        Column('deleted_at', DateTime),
        Column('deleted', String(36)),
        Column('id', String(36), primary_key=True, nullable=False),
        Column('image_file_id', String(36)),
        Column('qos_id', String(36)),
        Column('glance_image_id', String(255)),
        Column('snapshot_id', String(36)),
        Column('host', String(length=255)),
        Column('size', Integer),
        Column('user_id', String(length=255)),
        Column('project_id', String(length=255)),
        Column('availability_zone', String(length=255)),
        Column('instance_uuid', String(length=36)),
        Column('mountpoint', String(length=255)),
        Column('attach_time', DateTime),
        Column('bootable', Boolean),
        Column('status', String(length=255)),
        Column('attach_status', String(length=255)),
        Column('migration_status', String(length=255)),
        Column('scheduled_at', DateTime),
        Column('launched_at', DateTime),
        Column('terminated_at', DateTime),
        Column('display_name', String(length=255)),
        Column('display_description', String(length=255)),
        mysql_engine='InnoDB',
        mysql_charset='utf8'
    )

    ext_volume_snapshots = Table('ext_volume_snapshots', meta,
        Column('created_at', DateTime),
        Column('updated_at', DateTime),
        Column('deleted_at', DateTime),
        Column('deleted', String(36)),
        Column('id', String(36), primary_key=True, nullable=False),
        Column('image_file_id', String(36)),
        Column('volume_id', String(36)),
        Column('instance_snapshot_id', String(36)),
        Column('status', String(length=255)),
        Column('progress', String(length=255)),
        Column('user_id', String(length=255)),
        Column('project_id', String(length=255)),
        Column('volume_size', Integer),
        Column('display_name', String(length=255)),
        Column('display_description', String(length=255)),
        mysql_engine='InnoDB',
        mysql_charset='utf8'
    )

    ext_instance_snapshots = Table('ext_instance_snapshots', meta,
        Column('created_at', DateTime),
        Column('updated_at', DateTime),
        Column('deleted_at', DateTime),
        Column('deleted', String(36)),
        Column('id', String(36), primary_key=True, nullable=False),
        Column('status', String(length=255)),
        Column('progress', String(length=255)),
        Column('user_id', String(length=255)),
        Column('project_id', String(length=255)),
        Column('display_name', String(length=255)),
        Column('display_description', String(length=255)),
        mysql_engine='InnoDB',
        mysql_charset='utf8'
    )

    ext_qos = Table('ext_qos', meta,
        Column('created_at', DateTime),
        Column('updated_at', DateTime),
        Column('deleted_at', DateTime),
        Column('deleted', String(36)),
        Column('id', String(36), primary_key=True, nullable=False),
        Column('display_name', String(length=255)),
        Column('display_description', String(length=255)),
        Column('read_bytes_sec', Integer),
        Column('read_iops_sec', Integer),
        Column('write_bytes_sec', Integer),
        Column('write_iops_sec', Integer),
        Column('total_bytes_sec', Integer),
        Column('total_iops_sec', Integer),
        mysql_engine='InnoDB',
        mysql_charset='utf8'
    )

    tables = [ext_image_files, ext_volumes, ext_volume_snapshots,
              ext_instance_snapshots, ext_qos]
    for table in tables:
        try:
            table.create()
        except Exception:
            raise


def downgrade(engine):
    raise NotImplementedError('Downgrade from local disk is unsupported.')
