# Copyright 2013 Red Hat, Inc.
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

__all__ = [
    'init',
    'cleanup',
    'set_defaults',
    'add_extra_exmods',
    'clear_extra_exmods',
    'get_allowed_exmods',
    'RequestContextSerializer',
    'get_client',
    'get_server',
    'get_notifier',
    'TRANSPORT_ALIASES',
]

import functools

from nova.i18n import _
from oslo_config import cfg
from oslo_log import log as logging
import oslo_messaging as messaging
from oslo_serialization import jsonutils

import nova.context
import nova.exception


CONF = cfg.CONF
notification_opts = [
    cfg.StrOpt('notification_format',
               choices=['unversioned', 'versioned', 'both'],
               default='both',
               help='Specifies which notification format shall be used by '
                    'nova.'),
]

CONF.register_opts(notification_opts)

LOG = logging.getLogger(__name__)

TRANSPORT = None
LEGACY_NOTIFIER = None
NOTIFICATION_TRANSPORT = None
NOTIFIER = None

ALLOWED_EXMODS = [
    nova.exception.__name__,
]
EXTRA_EXMODS = []

# NOTE(markmc): The nova.openstack.common.rpc entries are for backwards compat
# with Havana rpc_backend configuration values. The nova.rpc entries are for
# compat with Essex values.
TRANSPORT_ALIASES = {
    'nova.openstack.common.rpc.impl_kombu': 'rabbit',
    'nova.openstack.common.rpc.impl_qpid': 'qpid',
    'nova.openstack.common.rpc.impl_zmq': 'zmq',
    'nova.rpc.impl_kombu': 'rabbit',
    'nova.rpc.impl_qpid': 'qpid',
    'nova.rpc.impl_zmq': 'zmq',
}


def init(conf):
    global TRANSPORT, NOTIFICATION_TRANSPORT, LEGACY_NOTIFIER, NOTIFIER
    exmods = get_allowed_exmods()
    TRANSPORT = messaging.get_transport(conf,
                                        allowed_remote_exmods=exmods,
                                        aliases=TRANSPORT_ALIASES)
    NOTIFICATION_TRANSPORT = messaging.get_notification_transport(
        conf, allowed_remote_exmods=exmods, aliases=TRANSPORT_ALIASES)
    serializer = RequestContextSerializer(JsonPayloadSerializer())
    if conf.notification_format == 'unversioned':
        LEGACY_NOTIFIER = messaging.Notifier(NOTIFICATION_TRANSPORT,
                                             serializer=serializer)
        NOTIFIER = messaging.Notifier(NOTIFICATION_TRANSPORT,
                                      serializer=serializer, driver='noop')
    elif conf.notification_format == 'both':
        LEGACY_NOTIFIER = messaging.Notifier(NOTIFICATION_TRANSPORT,
                                             serializer=serializer)
        NOTIFIER = messaging.Notifier(NOTIFICATION_TRANSPORT,
                                      serializer=serializer,
                                      topic='versioned_notifications')
    else:
        LEGACY_NOTIFIER = messaging.Notifier(NOTIFICATION_TRANSPORT,
                                             serializer=serializer,
                                             driver='noop')
        NOTIFIER = messaging.Notifier(NOTIFICATION_TRANSPORT,
                                      serializer=serializer,
                                      topic='versioned_notifications')


def cleanup():
    global TRANSPORT, NOTIFICATION_TRANSPORT, LEGACY_NOTIFIER, NOTIFIER
    assert TRANSPORT is not None
    assert NOTIFICATION_TRANSPORT is not None
    assert LEGACY_NOTIFIER is not None
    assert NOTIFIER is not None
    TRANSPORT.cleanup()
    NOTIFICATION_TRANSPORT.cleanup()
    TRANSPORT = NOTIFICATION_TRANSPORT = LEGACY_NOTIFIER = NOTIFIER = None


def set_defaults(control_exchange):
    messaging.set_transport_defaults(control_exchange)


def add_extra_exmods(*args):
    EXTRA_EXMODS.extend(args)


def clear_extra_exmods():
    del EXTRA_EXMODS[:]


def get_allowed_exmods():
    return ALLOWED_EXMODS + EXTRA_EXMODS


class JsonPayloadSerializer(messaging.NoOpSerializer):
    @staticmethod
    def serialize_entity(context, entity):
        return jsonutils.to_primitive(entity, convert_instances=True)


class RequestContextSerializer(messaging.Serializer):

    def __init__(self, base):
        self._base = base

    def serialize_entity(self, context, entity):
        if not self._base:
            return entity
        return self._base.serialize_entity(context, entity)

    def deserialize_entity(self, context, entity):
        if not self._base:
            return entity
        return self._base.deserialize_entity(context, entity)

    def serialize_context(self, context):
        return context.to_dict()

    def deserialize_context(self, context):
        return nova.context.RequestContext.from_dict(context)


def get_transport_url(url_str=None):
    return messaging.TransportURL.parse(CONF, url_str, TRANSPORT_ALIASES)


def get_client(target, version_cap=None, serializer=None):
    assert TRANSPORT is not None
    serializer = RequestContextSerializer(serializer)
    return messaging.RPCClient(TRANSPORT,
                               target,
                               version_cap=version_cap,
                               serializer=serializer)


def get_server(target, endpoints, serializer=None):
    assert TRANSPORT is not None
    serializer = RequestContextSerializer(serializer)
    return messaging.get_rpc_server(TRANSPORT,
                                    target,
                                    endpoints,
                                    executor='eventlet',
                                    serializer=serializer)


def get_notifier(service, host=None, publisher_id=None):
    assert LEGACY_NOTIFIER is not None
    if not publisher_id:
        publisher_id = "%s.%s" % (service, host or CONF.host)
    return LegacyValidatingNotifier(
            LEGACY_NOTIFIER.prepare(publisher_id=publisher_id))


def get_versioned_notifier(publisher_id):
    assert NOTIFIER is not None
    return NOTIFIER.prepare(publisher_id=publisher_id)


class LegacyValidatingNotifier(object):
    """Wraps an oslo.messaging Notifier and checks for allowed event_types."""

    # If true an exception is thrown if the event_type is not allowed, if false
    # then only a WARNING is logged
    fatal = False

    # This list contains the already existing therefore allowed legacy
    # notification event_types. New items shall not be added to the list as
    # Nova does not allow new legacy notifications any more. This list will be
    # removed when all the notification is transformed to versioned
    # notifications.
    allowed_legacy_notification_event_types = [
        'aggregate.addhost.end',
        'aggregate.addhost.start',
        'aggregate.create.end',
        'aggregate.create.start',
        'aggregate.delete.end',
        'aggregate.delete.start',
        'aggregate.removehost.end',
        'aggregate.removehost.start',
        'aggregate.updatemetadata.end',
        'aggregate.updatemetadata.start',
        'aggregate.updateprop.end',
        'aggregate.updateprop.start',
        'api.fault',
        'compute.instance.attach_volume.end',
        'compute.instance.attach_volume.start',
        'compute.instance.cold_migration.end',
        'compute.instance.cold_migration.start',
        'compute.instance.create.end',
        'compute.instance.create.error',
        'compute.instance.create_ip.end',
        'compute.instance.create_ip.start',
        'compute.instance.create.start',
        'compute.instance.delete.end',
        'compute.instance.delete_ip.end',
        'compute.instance.delete_ip.start',
        'compute.instance.delete.start',
        'compute.instance.detach_volume.end',
        'compute.instance.detach_volume.start',
        'compute.instance.evacuate',
        'compute.instance.exists',
        'compute.instance.finish_resize.end',
        'compute.instance.finish_resize.start',
        'compute.instance.live.migration.abort.start',
        'compute.instance.live.migration.abort.end',
        'compute.instance.live_migration.end',
        'compute.instance.live_migration.error',
        'compute.instance.live_migration.post.dest.end',
        'compute.instance.live_migration.post.dest.start',
        'compute.instance.live_migration._post.end',
        'compute.instance.live_migration._post.start',
        'compute.instance.live_migration.pre.end',
        'compute.instance.live_migration.pre.start',
        'compute.instance.live_migration.rollback.dest.end',
        'compute.instance.live_migration.rollback.dest.start',
        'compute.instance.live_migration.start',
        'compute.instance.live_migration._rollback.end',
        'compute.instance.live_migration._rollback.start',
        'compute.instance.pause_instance.end',
        'compute.instance.pause_instance.start',
        'compute.instance.power_off.end',
        'compute.instance.power_off.start',
        'compute.instance.power_on.end',
        'compute.instance.power_on.start',
        'compute.instance.quiesce_instance.error',
        'compute.instance.quiesce_instance.end',
        'compute.instance.quiesce_instance.start',
        'compute.instance.reboot.end',
        'compute.instance.reboot.start',
        'compute.instance.rebuild.end',
        'compute.instance.rebuild.error',
        'compute.instance.rebuild.scheduled',
        'compute.instance.rebuild.start',
        'compute.instance.rescue.end',
        'compute.instance.rescue.start',
        'compute.instance.resize.confirm.end',
        'compute.instance.resize.confirm.start',
        'compute.instance.resize.end',
        'compute.instance.resize.error',
        'compute.instance.resize.prep.end',
        'compute.instance.resize.prep.start',
        'compute.instance.resize.revert.end',
        'compute.instance.resize.revert.start',
        'compute.instance.resize.start',
        'compute.instance.restore.end',
        'compute.instance.restore.start',
        'compute.instance.resume.end',
        'compute.instance.resume.start',
        'compute.instance.shelve.end',
        'compute.instance.shelve_offload.end',
        'compute.instance.shelve_offload.start',
        'compute.instance.shelve.start',
        'compute.instance.shutdown.end',
        'compute.instance.shutdown.start',
        'compute.instance.snapshot.end',
        'compute.instance.snapshot.start',
        'compute.instance.soft_delete.end',
        'compute.instance.soft_delete.start',
        'compute.instance.suspend.end',
        'compute.instance.suspend.start',
        'compute.instance.trigger_crash_dump.end',
        'compute.instance.trigger_crash_dump.start',
        'compute.instance.unpause.end',
        'compute.instance.unpause.start',
        'compute.instance.unquiesce_instance.error',
        'compute.instance.unquiesce_instance.end',
        'compute.instance.unquiesce_instance.start',
        'compute.instance.unrescue.end',
        'compute.instance.unrescue.start',
        'compute.instance.unshelve.start',
        'compute.instance.unshelve.end',
        'compute.instance.update',
        'compute.instance.volume.attach',
        'compute.instance.volume.detach',
        'compute.libvirt.error',
        'compute_task.build_instances',
        'compute_task.migrate_server',
        'compute_task.rebuild_server',
        'HostAPI.power_action.end',
        'HostAPI.power_action.start',
        'HostAPI.set_enabled.end',
        'HostAPI.set_enabled.start',
        'HostAPI.set_maintenance.end',
        'HostAPI.set_maintenance.start',
        'keypair.create.start',
        'keypair.create.end',
        'keypair.delete.start',
        'keypair.delete.end',
        'keypair.import.start',
        'keypair.import.end',
        'network.floating_ip.allocate',
        'network.floating_ip.associate',
        'network.floating_ip.deallocate',
        'network.floating_ip.disassociate',
        'scheduler.select_destinations.end',
        'scheduler.select_destinations.start',
        'servergroup.addmember',
        'servergroup.create',
        'servergroup.delete',
        'volume.usage',
    ]

    message = _('%(event_type)s is not a versioned notification and not '
                'whitelisted. See ./doc/source/notification.rst')

    def __init__(self, notifier):
        self.notifier = notifier
        for priority in ['debug', 'info', 'warn', 'error', 'critical']:
            setattr(self, priority,
                    functools.partial(self._notify, priority))

    def _is_wrap_exception_notification(self, payload):
        # nova.exception.wrap_exception decorator emits notification where the
        # event_type is the name of the decorated function. This is used in
        # many places but it will be converted to versioned notification in one
        # run by updating the decorator so it is pointless to white list all
        # the function names here we white list the notification itself
        # detected by the special payload keys.
        return {'exception', 'args'} == set(payload.keys())

    def _notify(self, priority, ctxt, event_type, payload):
        if (event_type not in self.allowed_legacy_notification_event_types and
                not self._is_wrap_exception_notification(payload)):
            if self.fatal:
                raise AssertionError(self.message % {'event_type': event_type})
            else:
                LOG.warning(self.message, {'event_type': event_type})

        getattr(self.notifier, priority)(ctxt, event_type, payload)
