# Copyright 2016 ChinaC Corporation

from sqlalchemy import Column
from sqlalchemy import MetaData
from sqlalchemy import Table
from sqlalchemy import Text


def upgrade(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine

    instances_table = Table('instances', meta, autoload=True)
    iso_column = Column('iso', Text, nullable=True)
    instances_table.create_column(iso_column)
