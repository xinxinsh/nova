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
from sqlalchemy import Integer, DateTime, String


def upgrade(engine):
    meta = MetaData(bind=engine)

    bandwidth = Table('bandwidth', meta,
                      Column('created_at', DateTime),
                      Column('updated_at', DateTime),
                      Column('deleted_at', DateTime),
                      Column('port_id', String(length=255), primary_key=True,
                             nullable=False),
                      Column('inbound_kilo_bytes', Integer),
                      Column('outbound_kilo_bytes', Integer),
                      Column('deleted', Integer),
                      mysql_engine='InnoDB',
                      mysql_charset='utf8'
                      )

    try:
        bandwidth.create()
    except Exception:
        raise


def downgrade(engine):
    raise NotImplementedError('Downgrade from bandwidth is unsupported.')
