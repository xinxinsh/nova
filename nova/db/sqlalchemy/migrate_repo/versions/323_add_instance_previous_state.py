# Copyright 2016 ChinaC Corporation
#
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
from sqlalchemy import String
from sqlalchemy import Table


def upgrade(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine

    instances_table = Table('instances', meta, autoload=True)
    vm_previous_state_column = Column('vm_previous_state',
                                      String(length=255),
                                      nullable=True)
    instances_table.create_column(vm_previous_state_column)

    instances_table = Table('shadow_instances', meta, autoload=True)
    vm_previous_state_column = Column('vm_previous_state',
                                      String(length=255),
                                      nullable=True)
    instances_table.create_column(vm_previous_state_column)
