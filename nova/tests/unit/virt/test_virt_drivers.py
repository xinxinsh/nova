#    Copyright 2010 OpenStack Foundation
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

import sys
import traceback

import fixtures
import mock
import netaddr
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import importutils
import six

from nova.compute import manager
from nova import exception
from nova import objects
from nova import test
from nova.tests import fixtures as nova_fixtures
from nova.tests.unit.image import fake as fake_image
from nova.tests.unit import utils as test_utils
from nova.virt import event as virtevent
from nova.virt import fake
from nova.virt import libvirt
from nova.virt.libvirt import imagebackend

LOG = logging.getLogger(__name__)


def catch_notimplementederror(f):
    """Decorator to simplify catching drivers raising NotImplementedError

    If a particular call makes a driver raise NotImplementedError, we
    log it so that we can extract this information afterwards as needed.
    """
    def wrapped_func(self, *args, **kwargs):
        try:
            return f(self, *args, **kwargs)
        except NotImplementedError:
            frame = traceback.extract_tb(sys.exc_info()[2])[-1]
            LOG.error("%(driver)s does not implement %(method)s "
                      "required for test %(test)s" %
                      {'driver': type(self.connection),
                       'method': frame[2], 'test': f.__name__})

    wrapped_func.__name__ = f.__name__
    wrapped_func.__doc__ = f.__doc__
    return wrapped_func


class _FakeDriverBackendTestCase(object):
    def _setup_fakelibvirt(self):
        # So that the _supports_direct_io does the test based
        # on the current working directory, instead of the
        # default instances_path which doesn't exist
        self.flags(instances_path=self.useFixture(fixtures.TempDir()).path)

        # Put fakelibvirt in place
        if 'libvirt' in sys.modules:
            self.saved_libvirt = sys.modules['libvirt']
        else:
            self.saved_libvirt = None

        import nova.tests.unit.virt.libvirt.fake_imagebackend as \
            fake_imagebackend
        import nova.tests.unit.virt.libvirt.fake_libvirt_utils as \
            fake_libvirt_utils
        import nova.tests.unit.virt.libvirt.fakelibvirt as fakelibvirt

        import nova.tests.unit.virt.libvirt.fake_os_brick_connector as \
            fake_os_brick_connector

        sys.modules['libvirt'] = fakelibvirt
        import nova.virt.libvirt.driver
        import nova.virt.libvirt.firewall
        import nova.virt.libvirt.host

        self.useFixture(fixtures.MonkeyPatch(
            'nova.virt.libvirt.driver.imagebackend',
            fake_imagebackend))
        self.useFixture(fixtures.MonkeyPatch(
            'nova.virt.libvirt.driver.libvirt',
            fakelibvirt))
        self.useFixture(fixtures.MonkeyPatch(
            'nova.virt.libvirt.driver.libvirt_utils',
            fake_libvirt_utils))
        self.useFixture(fixtures.MonkeyPatch(
            'nova.virt.libvirt.host.libvirt',
            fakelibvirt))
        self.useFixture(fixtures.MonkeyPatch(
            'nova.virt.libvirt.imagebackend.libvirt_utils',
            fake_libvirt_utils))
        self.useFixture(fixtures.MonkeyPatch(
            'nova.virt.libvirt.firewall.libvirt',
            fakelibvirt))

        self.useFixture(fixtures.MonkeyPatch(
            'nova.virt.libvirt.driver.connector',
            fake_os_brick_connector))

        fakelibvirt.disable_event_thread(self)

        self.flags(rescue_image_id="2",
                   rescue_kernel_id="3",
                   rescue_ramdisk_id=None,
                   snapshots_directory='./',
                   sysinfo_serial='none',
                   group='libvirt')

        def fake_extend(image, size):
            pass

        def fake_migrateToURI(*a):
            pass

        def fake_make_drive(_self, _path):
            pass

        def fake_get_instance_disk_info(_self, instance, xml=None,
                                        block_device_info=None):
            return '[]'

        def fake_delete_instance_files(_self, _instance):
            pass

        def fake_wait():
            pass

        def fake_detach_device_with_retry(_self, get_device_conf_func, device,
                                          persistent, live,
                                          max_retry_count=7,
                                          inc_sleep_time=2,
                                          max_sleep_time=30):
            # Still calling detach, but instead of returning function
            # that actually checks if device is gone from XML, just continue
            # because XML never gets updated in these tests
            _self.detach_device(get_device_conf_func(device),
                                persistent=persistent,
                                live=live)
            return fake_wait

        self.stubs.Set(nova.virt.libvirt.driver.LibvirtDriver,
                       '_get_instance_disk_info',
                       fake_get_instance_disk_info)

        self.stubs.Set(nova.virt.libvirt.driver.disk,
                       'extend', fake_extend)

        self.stubs.Set(nova.virt.libvirt.driver.LibvirtDriver,
                       'delete_instance_files',
                       fake_delete_instance_files)

        self.stubs.Set(nova.virt.libvirt.guest.Guest,
                       'detach_device_with_retry',
                       fake_detach_device_with_retry)

        # Like the existing fakelibvirt.migrateToURI, do nothing,
        # but don't fail for these tests.
        self.stubs.Set(nova.virt.libvirt.driver.libvirt.Domain,
                       'migrateToURI', fake_migrateToURI)

        # We can't actually make a config drive v2 because ensure_tree has
        # been faked out
        self.stubs.Set(nova.virt.configdrive.ConfigDriveBuilder,
                       'make_drive', fake_make_drive)

    def _teardown_fakelibvirt(self):
        # Restore libvirt
        if self.saved_libvirt:
            sys.modules['libvirt'] = self.saved_libvirt

    def setUp(self):
        super(_FakeDriverBackendTestCase, self).setUp()
        # TODO(sdague): it would be nice to do this in a way that only
        # the relevant backends where replaced for tests, though this
        # should not harm anything by doing it for all backends
        fake_image.stub_out_image_service(self)
        self._setup_fakelibvirt()

    def tearDown(self):
        fake_image.FakeImageService_reset()
        self._teardown_fakelibvirt()
        super(_FakeDriverBackendTestCase, self).tearDown()


class VirtDriverLoaderTestCase(_FakeDriverBackendTestCase, test.TestCase):
    """Test that ComputeManager can successfully load both
    old style and new style drivers and end up with the correct
    final class.
    """

    # if your driver supports being tested in a fake way, it can go here
    #
    # both long form and short form drivers are supported
    new_drivers = {
        'nova.virt.fake.FakeDriver': 'FakeDriver',
        'nova.virt.libvirt.LibvirtDriver': 'LibvirtDriver',
        'fake.FakeDriver': 'FakeDriver',
        'libvirt.LibvirtDriver': 'LibvirtDriver'
        }

    def test_load_new_drivers(self):
        for cls, driver in six.iteritems(self.new_drivers):
            self.flags(compute_driver=cls)
            # NOTE(sdague) the try block is to make it easier to debug a
            # failure by knowing which driver broke
            try:
                cm = manager.ComputeManager()
            except Exception as e:
                self.fail("Couldn't load driver %s - %s" % (cls, e))

            self.assertEqual(cm.driver.__class__.__name__, driver,
                             "Could't load driver %s" % cls)

    def test_fail_to_load_new_drivers(self):
        self.flags(compute_driver='nova.virt.amiga')

        def _fake_exit(error):
            raise test.TestingException()

        self.stubs.Set(sys, 'exit', _fake_exit)
        self.assertRaises(test.TestingException, manager.ComputeManager)


class _VirtDriverTestCase(_FakeDriverBackendTestCase):
    def setUp(self):
        super(_VirtDriverTestCase, self).setUp()

        self.flags(instances_path=self.useFixture(fixtures.TempDir()).path)
        self.connection = importutils.import_object(self.driver_module,
                                                    fake.FakeVirtAPI())
        self.ctxt = test_utils.get_test_admin_context()
        self.image_service = fake_image.FakeImageService()
        # NOTE(dripton): resolve_driver_format does some file reading and
        # writing and chowning that complicate testing too much by requiring
        # using real directories with proper permissions.  Just stub it out
        # here; we test it in test_imagebackend.py
        self.stubs.Set(imagebackend.Image, 'resolve_driver_format',
                       imagebackend.Image._get_driver_format)

    def _get_running_instance(self, obj=True):
        pass

    @catch_notimplementederror
    def test_init_host(self):
        self.connection.init_host('myhostname')

    @catch_notimplementederror
    def test_list_instances(self):
        self.connection.list_instances()

    @catch_notimplementederror
    def test_list_instance_uuids(self):
        self.connection.list_instance_uuids()

    @catch_notimplementederror
    def test_spawn(self):
        pass

    @catch_notimplementederror
    def test_snapshot_not_running(self):
        instance_ref = test_utils.get_test_instance()
        img_ref = self.image_service.create(self.ctxt, {'name': 'snap-1'})
        self.assertRaises(exception.InstanceNotRunning,
                          self.connection.snapshot,
                          self.ctxt, instance_ref, img_ref['id'],
                          lambda *args, **kwargs: None)

    @catch_notimplementederror
    def test_snapshot_running(self):
        pass

    @catch_notimplementederror
    def test_post_interrupted_snapshot_cleanup(self):
        pass

    @catch_notimplementederror
    def test_reboot(self):
        pass

    @catch_notimplementederror
    def test_get_host_ip_addr(self):
        host_ip = self.connection.get_host_ip_addr()

        # Will raise an exception if it's not a valid IP at all
        ip = netaddr.IPAddress(host_ip)

        # For now, assume IPv4.
        self.assertEqual(ip.version, 4)

    @catch_notimplementederror
    def test_set_admin_password(self):
        pass

    @catch_notimplementederror
    def test_inject_file(self):
        pass

    @catch_notimplementederror
    def test_resume_state_on_host_boot(self):
        pass

    @catch_notimplementederror
    def test_rescue(self):
        pass

    @catch_notimplementederror
    def test_unrescue_unrescued_instance(self):
        pass

    @catch_notimplementederror
    def test_unrescue_rescued_instance(self):
        pass

    @catch_notimplementederror
    def test_poll_rebooting_instances(self):
        pass

    @catch_notimplementederror
    def test_migrate_disk_and_power_off(self):
        pass

    @catch_notimplementederror
    def test_power_off(self):
        pass

    @catch_notimplementederror
    def test_power_on_running(self):
        pass

    @catch_notimplementederror
    def test_power_on_powered_off(self):
        pass

    @catch_notimplementederror
    def test_trigger_crash_dump(self):
        pass

    @catch_notimplementederror
    def test_soft_delete(self):
        pass

    @catch_notimplementederror
    def test_restore_running(self):
        pass

    @catch_notimplementederror
    def test_restore_soft_deleted(self):
        pass

    @catch_notimplementederror
    def test_resume_unsuspended_instance(self):
        pass

    @catch_notimplementederror
    def test_resume_suspended_instance(self):
        pass

    @catch_notimplementederror
    def test_destroy_instance_nonexistent(self):
        fake_instance = test_utils.get_test_instance(obj=True)
        network_info = test_utils.get_test_network_info()
        self.connection.destroy(self.ctxt, fake_instance, network_info)

    @catch_notimplementederror
    def test_destroy_instance(self):
        pass

    @catch_notimplementederror
    def test_get_volume_connector(self):
        result = self.connection.get_volume_connector({'id': 'fake'})
        self.assertIn('ip', result)
        self.assertIn('initiator', result)
        self.assertIn('host', result)

    @catch_notimplementederror
    def test_get_volume_connector_storage_ip(self):
        ip = 'my_ip'
        storage_ip = 'storage_ip'
        self.flags(my_block_storage_ip=storage_ip, my_ip=ip)
        result = self.connection.get_volume_connector({'id': 'fake'})
        self.assertIn('ip', result)
        self.assertIn('initiator', result)
        self.assertIn('host', result)
        self.assertEqual(storage_ip, result['ip'])

    @catch_notimplementederror
    def test_attach_detach_volume(self):
        pass

    @catch_notimplementederror
    def test_swap_volume(self):
        pass

    @catch_notimplementederror
    def test_attach_detach_different_power_states(self):
        pass

    @catch_notimplementederror
    def test_get_info(self):
        pass

    @catch_notimplementederror
    def test_get_info_for_unknown_instance(self):
        fake_instance = test_utils.get_test_instance(obj=True)
        self.assertRaises(exception.NotFound,
                          self.connection.get_info,
                          fake_instance)

    @catch_notimplementederror
    def test_get_diagnostics(self):
        pass

    @catch_notimplementederror
    def test_get_instance_diagnostics(self):
        pass

    @catch_notimplementederror
    def test_block_stats(self):
        pass

    @catch_notimplementederror
    def test_get_console_output(self):
        pass

    @catch_notimplementederror
    def test_get_vnc_console(self):
        pass

    @catch_notimplementederror
    def test_get_spice_console(self):
        pass

    @catch_notimplementederror
    def test_get_rdp_console(self):
        pass

    @catch_notimplementederror
    def test_get_serial_console(self):
        pass

    @catch_notimplementederror
    def test_get_mks_console(self):
        pass

    @catch_notimplementederror
    def test_get_console_pool_info(self):
        pass

    @catch_notimplementederror
    def test_refresh_security_group_rules(self):
        pass

    @catch_notimplementederror
    def test_refresh_instance_security_rules(self):
        pass

    @catch_notimplementederror
    def test_ensure_filtering_for_instance(self):
        instance = test_utils.get_test_instance(obj=True)
        network_info = test_utils.get_test_network_info()
        self.connection.ensure_filtering_rules_for_instance(instance,
                                                            network_info)

    @catch_notimplementederror
    def test_unfilter_instance(self):
        instance_ref = test_utils.get_test_instance()
        network_info = test_utils.get_test_network_info()
        self.connection.unfilter_instance(instance_ref, network_info)

    def test_live_migration(self):
        pass

    @catch_notimplementederror
    def test_live_migration_force_complete(self):
        pass

    @catch_notimplementederror
    def test_live_migration_abort(self):
        pass

    @catch_notimplementederror
    def _check_available_resource_fields(self, host_status):
        keys = ['vcpus', 'memory_mb', 'local_gb', 'vcpus_used',
                'memory_mb_used', 'hypervisor_type', 'hypervisor_version',
                'hypervisor_hostname', 'cpu_info', 'disk_available_least',
                'supported_instances']
        for key in keys:
            self.assertIn(key, host_status)
        self.assertIsInstance(host_status['hypervisor_version'], int)

    @catch_notimplementederror
    def test_get_available_resource(self):
        available_resource = self.connection.get_available_resource(
                'myhostname')
        self._check_available_resource_fields(available_resource)

    @catch_notimplementederror
    def test_get_available_nodes(self):
        self.connection.get_available_nodes(False)

    @catch_notimplementederror
    def _check_host_cpu_status_fields(self, host_cpu_status):
        self.assertIn('kernel', host_cpu_status)
        self.assertIn('idle', host_cpu_status)
        self.assertIn('user', host_cpu_status)
        self.assertIn('iowait', host_cpu_status)
        self.assertIn('frequency', host_cpu_status)

    @catch_notimplementederror
    def test_get_host_cpu_stats(self):
        host_cpu_status = self.connection.get_host_cpu_stats()
        self._check_host_cpu_status_fields(host_cpu_status)

    @catch_notimplementederror
    def test_set_host_enabled(self):
        self.connection.set_host_enabled(True)

    @catch_notimplementederror
    def test_get_host_uptime(self):
        self.connection.get_host_uptime()

    @catch_notimplementederror
    def test_host_power_action_reboot(self):
        self.connection.host_power_action('reboot')

    @catch_notimplementederror
    def test_host_power_action_shutdown(self):
        self.connection.host_power_action('shutdown')

    @catch_notimplementederror
    def test_host_power_action_startup(self):
        self.connection.host_power_action('startup')

    @catch_notimplementederror
    def test_add_to_aggregate(self):
        self.connection.add_to_aggregate(self.ctxt, 'aggregate', 'host')

    @catch_notimplementederror
    def test_remove_from_aggregate(self):
        self.connection.remove_from_aggregate(self.ctxt, 'aggregate', 'host')

    def test_events(self):
        got_events = []

        def handler(event):
            got_events.append(event)

        self.connection.register_event_listener(handler)

        event1 = virtevent.LifecycleEvent(
            "cef19ce0-0ca2-11df-855d-b19fbce37686",
            virtevent.EVENT_LIFECYCLE_STARTED)
        event2 = virtevent.LifecycleEvent(
            "cef19ce0-0ca2-11df-855d-b19fbce37686",
            virtevent.EVENT_LIFECYCLE_PAUSED)

        self.connection.emit_event(event1)
        self.connection.emit_event(event2)
        want_events = [event1, event2]
        self.assertEqual(want_events, got_events)

        event3 = virtevent.LifecycleEvent(
            "cef19ce0-0ca2-11df-855d-b19fbce37686",
            virtevent.EVENT_LIFECYCLE_RESUMED)
        event4 = virtevent.LifecycleEvent(
            "cef19ce0-0ca2-11df-855d-b19fbce37686",
            virtevent.EVENT_LIFECYCLE_STOPPED)

        self.connection.emit_event(event3)
        self.connection.emit_event(event4)

        want_events = [event1, event2, event3, event4]
        self.assertEqual(want_events, got_events)

    def test_event_bad_object(self):
        # Passing in something which does not inherit
        # from virtevent.Event

        def handler(event):
            pass

        self.connection.register_event_listener(handler)

        badevent = {
            "foo": "bar"
        }

        self.assertRaises(ValueError,
                          self.connection.emit_event,
                          badevent)

    def test_event_bad_callback(self):
        # Check that if a callback raises an exception,
        # it does not propagate back out of the
        # 'emit_event' call

        def handler(event):
            raise Exception("Hit Me!")

        self.connection.register_event_listener(handler)

        event1 = virtevent.LifecycleEvent(
            "cef19ce0-0ca2-11df-855d-b19fbce37686",
            virtevent.EVENT_LIFECYCLE_STARTED)

        self.connection.emit_event(event1)

    def test_set_bootable(self):
        self.assertRaises(NotImplementedError, self.connection.set_bootable,
                          'instance', True)

    @catch_notimplementederror
    def test_get_instance_disk_info(self):
        pass

    @catch_notimplementederror
    def test_get_device_name_for_instance(self):
        pass

    def test_network_binding_host_id(self):
        # NOTE(jroll) self._get_running_instance calls spawn(), so we can't
        # use it to test this method. Make a simple object instead; we just
        # need instance.host.
        instance = objects.Instance(self.ctxt, host='somehost')
        self.assertEqual(instance.host,
            self.connection.network_binding_host_id(self.ctxt, instance))


class AbstractDriverTestCase(_VirtDriverTestCase, test.TestCase):
    def setUp(self):
        self.driver_module = "nova.virt.driver.ComputeDriver"
        super(AbstractDriverTestCase, self).setUp()

    def test_live_migration(self):
        self.skipTest('Live migration is not implemented in the base '
                      'virt driver.')


class FakeConnectionTestCase(_VirtDriverTestCase, test.TestCase):
    def setUp(self):
        self.driver_module = 'nova.virt.fake.FakeDriver'
        fake.set_nodes(['myhostname'])
        super(FakeConnectionTestCase, self).setUp()

    def _check_available_resource_fields(self, host_status):
        super(FakeConnectionTestCase, self)._check_available_resource_fields(
            host_status)

        hypervisor_type = host_status['hypervisor_type']
        supported_instances = host_status['supported_instances']
        try:
            # supported_instances could be JSON wrapped
            supported_instances = jsonutils.loads(supported_instances)
        except TypeError:
            pass
        self.assertTrue(any(hypervisor_type in x for x in supported_instances))


class LibvirtConnTestCase(_VirtDriverTestCase, test.TestCase):

    REQUIRES_LOCKING = True

    def setUp(self):
        # Point _VirtDriverTestCase at the right module
        self.driver_module = 'nova.virt.libvirt.LibvirtDriver'
        super(LibvirtConnTestCase, self).setUp()
        self.stubs.Set(self.connection,
                       '_set_host_enabled', mock.MagicMock())
        self.useFixture(fixtures.MonkeyPatch(
            'nova.context.get_admin_context',
            self._fake_admin_context))
        # This is needed for the live migration tests which spawn off the
        # operation for monitoring.
        self.useFixture(nova_fixtures.SpawnIsSynchronousFixture())

    def _fake_admin_context(self, *args, **kwargs):
        return self.ctxt

    def test_force_hard_reboot(self):
        pass

    def test_migrate_disk_and_power_off(self):
        # there is lack of fake stuff to execute this method. so pass.
        self.skipTest("Test nothing, but this method"
                      " needed to override superclass.")

    def test_internal_set_host_enabled(self):
        self.mox.UnsetStubs()
        service_mock = mock.MagicMock()

        # Previous status of the service: disabled: False
        service_mock.configure_mock(disabled_reason='None',
                                    disabled=False)
        with mock.patch.object(objects.Service, "get_by_compute_host",
                               return_value=service_mock):
            self.connection._set_host_enabled(False, 'ERROR!')
            self.assertTrue(service_mock.disabled)
            self.assertEqual(service_mock.disabled_reason, 'AUTO: ERROR!')

    def test_set_host_enabled_when_auto_disabled(self):
        self.mox.UnsetStubs()
        service_mock = mock.MagicMock()

        # Previous status of the service: disabled: True, 'AUTO: ERROR'
        service_mock.configure_mock(disabled_reason='AUTO: ERROR',
                                    disabled=True)
        with mock.patch.object(objects.Service, "get_by_compute_host",
                               return_value=service_mock):
            self.connection._set_host_enabled(True)
            self.assertFalse(service_mock.disabled)
            self.assertIsNone(service_mock.disabled_reason)

    def test_set_host_enabled_when_manually_disabled(self):
        self.mox.UnsetStubs()
        service_mock = mock.MagicMock()

        # Previous status of the service: disabled: True, 'Manually disabled'
        service_mock.configure_mock(disabled_reason='Manually disabled',
                                    disabled=True)
        with mock.patch.object(objects.Service, "get_by_compute_host",
                               return_value=service_mock):
            self.connection._set_host_enabled(True)
            self.assertTrue(service_mock.disabled)
            self.assertEqual(service_mock.disabled_reason, 'Manually disabled')

    def test_set_host_enabled_dont_override_manually_disabled(self):
        self.mox.UnsetStubs()
        service_mock = mock.MagicMock()

        # Previous status of the service: disabled: True, 'Manually disabled'
        service_mock.configure_mock(disabled_reason='Manually disabled',
                                    disabled=True)
        with mock.patch.object(objects.Service, "get_by_compute_host",
                               return_value=service_mock):
            self.connection._set_host_enabled(False, 'ERROR!')
            self.assertTrue(service_mock.disabled)
            self.assertEqual(service_mock.disabled_reason, 'Manually disabled')

    @catch_notimplementederror
    @mock.patch.object(libvirt.driver.LibvirtDriver, '_unplug_vifs')
    def test_unplug_vifs_with_destroy_vifs_false(self, unplug_vifs_mock):
        pass

    @catch_notimplementederror
    @mock.patch.object(libvirt.driver.LibvirtDriver, '_unplug_vifs')
    def test_unplug_vifs_with_destroy_vifs_true(self, unplug_vifs_mock):
        pass

    def test_get_device_name_for_instance(self):
        self.skipTest("Tested by the nova.tests.unit.virt.libvirt suite")

    @catch_notimplementederror
    @mock.patch('nova.utils.get_image_from_system_metadata')
    @mock.patch("nova.virt.libvirt.host.Host.has_min_version")
    def test_set_admin_password(self, ver, mock_image):
        pass
