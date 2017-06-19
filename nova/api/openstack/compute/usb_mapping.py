#   Copyright 2013 OpenStack Foundation
#
#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#   WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#   License for the specific language governing permissions and limitations
#   under the License.

import eventlet
from eventlet import GreenPool
eventlet.monkey_patch()

from nova.api.openstack import common
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova import compute
from nova.compute import power_state
from nova.compute import vm_states
from nova import db
from nova import exception
from nova.i18n import _
from nova import objects
from nova import servicegroup
from oslo_log import log as logging
import webob
from webob import exc

LOG = logging.getLogger(__name__)

ALIAS = "os-usb_mapping"
authorize = extensions.os_compute_authorizer(ALIAS)


class UsbMappingController(wsgi.Controller):
    def __init__(self, *args, **kwargs):
        super(UsbMappingController, self).__init__(*args, **kwargs)
        self.compute_api = compute.API()
        self.host_api = compute.HostAPI()
        self.servicegroup_api = servicegroup.API()
        self.pool = GreenPool(10000)

    def _get_hosts(self, context):
        """Returns a list of hosts"""

        hosts = []
        try:
            filters = {'disabled': False, 'binary': 'nova-compute'}
            services = self.host_api.service_get_all(context, filters)

            for s in services:
                if self.servicegroup_api.service_is_up(s):
                    hosts.append(s['host'])
        except exception.NotFound as e:
            raise exc.HTTPNotFound(explanation=e.format_message())

        return hosts

    def index(self, req):
        """Returns a usb_host_list of all host"""

        context = req.environ['nova.context']
        authorize(context)

        usb_host_list = []
        rets = []
        try:
            hosts = self._get_hosts(context)
            for host in hosts:
                ret = self.pool.spawn(self.compute_api.get_usb_host_list,
                                      *[context, host])
                rets.append({'ret': ret, 'host': host})
        except Exception:
            msg = _("get usb index action failed")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        for info in rets:
            usb_list = info['ret'].wait()

            host_info = dict()

            host_info['hostname'] = info['host']
            for i in range(len(usb_list)):
                if 'in use by' in usb_list[i]['usb_host_status']:
                    usb_host = usb_list[i]['usb_host_status'].split()[3]
                else:
                    usb_host = info['host']
                usb_list[i]['usb_vm_status'] = \
                    self.compute_api.get_usb_vm_status(
                        context,
                        usb_host,
                        usb_list[i]['usb_vid'],
                        usb_list[i]['usb_pid'])

            host_info['usb_list'] = usb_list

            usb_host_list.append(host_info)

        self.pool.waitall()

        return {'usb_host_list': usb_host_list}

    def usb_shared(self, req, body):
        """Set the usb device for shared state"""

        context = req.environ['nova.context']
        authorize(context)

        if not self.is_valid_body(body, 'usb_shared'):
            msg = (_("Missing required element '%s' in request body") %
                   'set_shared')
            raise exc.HTTPBadRequest(explanation=msg)
        usb_shared = body.get("usb_shared", {})

        host_name = usb_shared.get("host_name")
        if not host_name:
            msg = _("'host_name' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        usb_vid = usb_shared.get("usb_vid")
        if not usb_vid:
            msg = _("'usb_vid' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        usb_pid = usb_shared.get("usb_pid")
        if not usb_pid:
            msg = _("'usb_pid' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        usb_port = usb_shared.get("usb_port")
        if not usb_port:
            msg = _("'usb_port' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        shared = usb_shared.get("shared")
        if not shared:
            msg = _("'shared' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        hosts = self._get_hosts(context)
        if host_name not in hosts:
            msg = _("host:%s is not found or is down") % host_name
            raise exc.HTTPNotFound(explanation=msg)

        # check the USB status in real time
        try:
            usb_list = self.compute_api.get_usb_host_list(context, host_name)
        except exception.NotFound as e:
            raise exc.HTTPNotFound(explanation=e.format_message())

        usb_available = False
        msg_err = ''
        for usb_info in usb_list:
            if(usb_info['usb_vid'] == usb_vid
               and usb_info['usb_pid'] == usb_pid
               and usb_info['usb_port'] == usb_port):

                usb_available = True
                if((shared == "True"
                    and usb_info['usb_host_status'] != 'plugged')
                   or(shared == "False"
                      and usb_info['usb_host_status'] != 'plugged, shared')):
                    msg_err = ('host:[%s] usb [Vid %s Pid %s Port %s] is in '
                               'wrong state[%s]') % \
                              (host_name, usb_vid,
                               usb_pid, usb_port,
                               usb_info['usb_host_status'])

        if not usb_available:
            msg_err = ('host:[%s] usb [Vid %s Pid %s Port %s] can not be '
                       'found') % (host_name, usb_vid, usb_pid, usb_port)

        if msg_err != '':
            raise exc.HTTPNotFound(explanation=msg_err)

        self.compute_api.usb_shared(context,
                                    host_name,
                                    usb_vid,
                                    usb_pid,
                                    usb_port,
                                    shared)

        return webob.Response(status_int=200)

    def usb_mapped(self, req, body):
        """Set the usb device from source host remote map to the other host"""

        context = req.environ['nova.context']
        authorize(context)

        if not self.is_valid_body(body, 'usb_mapped'):
            msg = (_("Missing required element '%s' in request body") %
                   'usb_mapped')
            raise exc.HTTPBadRequest(explanation=msg)
        usb_mapped = body.get("usb_mapped", {})

        src_host_name = usb_mapped.get("src_host_name")
        if not src_host_name:
            msg = _("'src_host_name' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        dst_host_name = usb_mapped.get("dst_host_name")
        if not dst_host_name:
            msg = _("'dst_host_name' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        usb_vid = usb_mapped.get("usb_vid")
        if not usb_vid:
            msg = _("'usb_vid' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        usb_pid = usb_mapped.get("usb_pid")
        if not usb_pid:
            msg = _("'usb_pid' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        usb_port = usb_mapped.get("usb_port")
        if not usb_port:
            msg = _("'usb_port' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        mapped = usb_mapped.get("mapped")
        if not mapped:
            msg = _("'mapped' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        hosts = self._get_hosts(context)
        if src_host_name not in hosts:
            msg = _("host:%s is not found or is down") % src_host_name
            raise exc.HTTPNotFound(explanation=msg)
        if dst_host_name not in hosts:
            msg = _("host:%s is not found or is down") % dst_host_name
            raise exc.HTTPNotFound(explanation=msg)

        # check the USB status in real time
        try:
            usb_list = self.compute_api.get_usb_host_list(context,
                                                          src_host_name)
        except exception.NotFound as e:
            raise exc.HTTPNotFound(explanation=e.format_message())

        usb_available = False
        msg_err = ''
        for usb_info in usb_list:
            if(usb_info['usb_vid'] == usb_vid
               and usb_info['usb_pid'] == usb_pid
               and usb_info['usb_port'] == usb_port):

                usb_available = True
                if((mapped == "True"
                    and usb_info['usb_host_status'] != 'plugged, shared')
                   or(mapped == "False"
                      and _('in use by %s') % dst_host_name not in
                            usb_info['usb_host_status'])):
                    msg_err = ('host:[%s] usb [Vid %s Pid %s Port %s] is in '
                               'wrong state[%s]') % \
                              (src_host_name, usb_vid,
                               usb_pid, usb_port,
                               usb_info['usb_host_status'])

        if not usb_available:
            msg_err = ('host:[%s] usb [Vid %s Pid %s Port %s] can not be '
                       'found') % (src_host_name, usb_vid, usb_pid, usb_port)

        if msg_err != '':
            raise exc.HTTPNotFound(explanation=msg_err)

        self.compute_api.usb_mapped(context,
                                    src_host_name,
                                    dst_host_name,
                                    usb_vid,
                                    usb_pid,
                                    usb_port,
                                    mapped)

        return webob.Response(status_int=200)

    def _check_usb_dynamically(self, context, src_host_name,
                               dst_host_name, usb_vid, usb_pid):

        hosts = self._get_hosts(context)
        if src_host_name not in hosts:
            msg = _("host:%s is not found or is down") % src_host_name
            raise exc.HTTPNotFound(explanation=msg)
        if dst_host_name not in hosts:
            msg = _("host:%s is not found or is down") % dst_host_name
            raise exc.HTTPNotFound(explanation=msg)

        # check the USB status in real time
        try:
            usb_list = self.compute_api.get_usb_host_list(context,
                                                          src_host_name)
        except exception.NotFound as e:
            raise exc.HTTPNotFound(explanation=e.format_message())

        usb_available = False
        msg_err = ''
        for usb_info in usb_list:
            if(usb_info['usb_vid'] == usb_vid
               and usb_info['usb_pid'] == usb_pid):

                usb_available = True
                if src_host_name == dst_host_name:
                    if 'not plugged' in usb_info['usb_host_status']:
                        msg_err = ('host:[%s] usb [Vid %s Pid %s] can not be '
                                   'found') % (src_host_name, usb_vid,
                                               usb_pid)

                else:
                    if _('in use by %s') % dst_host_name not in \
                            usb_info['usb_host_status']:
                        msg_err = ('host:[%s] usb [Vid %s Pid %s] is in wrong '
                                   'state[%s]') % (src_host_name,
                                                   usb_vid, usb_pid,
                                                   usb_info['usb_host_status'])

        if not usb_available:
            msg_err = ('host:[%s] usb [Vid %s Pid %s] can not be '
                       'found') % (src_host_name,
                                   usb_vid,
                                   usb_pid)

        if msg_err != '':
            raise exc.HTTPNotFound(explanation=msg_err)

    def _get_usb(self, context, src_host_name, usb_vid, usb_pid):
        hosts = self._get_hosts(context)
        if src_host_name not in hosts:
            msg = _("host:%s is not found or is down") % src_host_name
            raise exc.HTTPNotFound(explanation=msg)

        # check the USB status in real time
        try:
            usb_list = self.compute_api.get_usb_host_list(context,
                                                          src_host_name)
        except exception.NotFound as e:
            raise exc.HTTPNotFound(explanation=e.format_message())

        for usb_info in usb_list:
            if usb_info['usb_vid'] == usb_vid and usb_info['usb_pid'] == \
                    usb_pid:
                return usb_info
        return None

    def _usb_auto_mapping(self, req, context, instance, usb_mounted):
        src_host_name = usb_mounted.get("src_host_name")
        usb_vid = usb_mounted.get("usb_vid")
        usb_pid = usb_mounted.get("usb_pid")

        usb = self._get_usb(context, src_host_name, usb_vid, usb_pid)
        if usb and 'in use by' not in usb.get('usb_host_status'):
            dst_host_name = src_host_name
        else:
            dst_host_name = usb['usb_host_status'].split('in use by ')[1]

        usb_port = usb.get('usb_port')

        if dst_host_name == instance.host:
            pass
        else:
            if dst_host_name != instance.host \
                    and dst_host_name != src_host_name:
                self.compute_api.usb_mapped(
                    context,
                    src_host_name,
                    dst_host_name,
                    usb_vid,
                    usb_pid,
                    usb_port,
                    "False")
                eventlet.sleep(3)

            # check the USB status in real time
            usb_info = self._get_usb(context, src_host_name, usb_vid, usb_pid)
            if usb_info['usb_host_status'] \
                    == 'plugged':
                self.compute_api.usb_shared(
                    context,
                    src_host_name,
                    usb_vid,
                    usb_pid,
                    usb_port,
                    "True")

            if src_host_name != instance.host:
                self.compute_api.usb_mapped(
                    context,
                    src_host_name,
                    instance.host,
                    usb_vid,
                    usb_pid,
                    usb_port,
                    "True")
                eventlet.sleep(5)

    def usb_mounted(self, req, body):
        """Mount the usb device to the local instance"""

        context = req.environ['nova.context']
        authorize(context)

        if not self.is_valid_body(body, 'usb_mounted'):
            msg = (_("Missing required element '%s' in request body") %
                   'usb_mounted')
            raise exc.HTTPBadRequest(explanation=msg)
        usb_mounted = body.get("usb_mounted", {})

        src_host_name = usb_mounted.get("src_host_name")
        if not src_host_name:
            msg = _("'src_host_name' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        usb_vid = usb_mounted.get("usb_vid")
        if not usb_vid:
            msg = _("'usb_vid' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        usb_pid = usb_mounted.get("usb_pid")
        if not usb_pid:
            msg = _("'usb_pid' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        instance_id = usb_mounted.get("instance_id")
        if not instance_id:
            msg = _("'instance_id' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        mounted = usb_mounted.get("mounted")
        if not mounted:
            msg = _("'mounted' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        instance = common.get_instance(self.compute_api,
                                       context,
                                       instance_id)

        dst_host_name = instance.host
        if dst_host_name != src_host_name:
            msg = (_("usb device:%(src)s and instance:%(dst)s must in the "
                     "same host") %
                   {'src': src_host_name, 'dst': dst_host_name})
            raise exc.HTTPNotFound(explanation=msg)

        if(instance['vm_state'] != vm_states.ACTIVE
           or instance['power_state'] != power_state.RUNNING
           or instance['task_state'] is not None):
            raise exception.InstanceNotReady(instance_id=instance['uuid'])

        # set usb auto mapping if vm not in src_host
        auto_mapping = usb_mounted.get("auto_map", True)
        if auto_mapping and mounted == "True":
            self._usb_auto_mapping(req, context, instance, usb_mounted)
            dst_host_name = instance.host

        self._check_usb_dynamically(context, src_host_name,
                                    dst_host_name, usb_vid, usb_pid)

        self.compute_api.usb_mounted(context,
                                     instance,
                                     dst_host_name,
                                     usb_vid,
                                     usb_pid,
                                     mounted)

        # detach_type = usb_mounted.get("detach_type", "auto")

        usb_port = None
        hosts = self._get_hosts(context)
        for host in hosts:
            usb_list = self.compute_api.get_usb_host_list(context, host)

            for i in range(len(usb_list)):
                if usb_vid == usb_list[i]['usb_vid'] \
                        and usb_pid == usb_list[i]['usb_pid']:
                    usb_port = usb_list[i]['usb_port']

        usb_mount = objects.UsbMount.get_by_vid_pid(context, usb_vid, usb_pid)
        if not usb_mount:
            usb_mount = objects.UsbMount(context=context)
            usb_mount.usb_vid = usb_vid
            usb_mount.usb_pid = usb_pid
            usb_mount.usb_port = usb_port
            usb_mount.src_host_name = src_host_name
            usb_mount.dst_host_name = dst_host_name
            if mounted == "True":
                usb_mount.instance_id = instance_id
                usb_mount.mounted = True
            else:
                usb_mount.instance_id = None
                usb_mount.mounted = False
            usb_mount.create()
        else:
            usb_mount.src_host_name = src_host_name
            usb_mount.dst_host_name = dst_host_name
            usb_mount.usb_port = usb_port
            if mounted == "True":
                usb_mount.instance_id = instance_id
                usb_mount.mounted = True
                usb_mount.save()
            else:
                # if detach_type != "auto":
                usb_mount.instance_id = None
                usb_mount.mounted = False
                usb_mount.save()
                # else:
                #     usb_mount.is_auto = True
                #     usb_mount.save()

        return webob.Response(status_int=200)

    def usb_status(self, req, body):
        """Get usb device status from the instance"""

        context = req.environ['nova.context']
        authorize(context)

        if not self.is_valid_body(body, 'usb_status'):
            msg = (_("Missing required element '%s' in request body") %
                   'usb_status')
            raise exc.HTTPBadRequest(explanation=msg)
        usb_status = body.get("usb_status", {})

        src_host_name = usb_status.get("src_host_name")
        if not src_host_name:
            msg = _("'src_host_name' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        dst_host_name = usb_status.get("dst_host_name")
        if not dst_host_name:
            dst_host_name = src_host_name

        usb_vid = usb_status.get("usb_vid")
        if not usb_vid:
            msg = _("'usb_vid' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        usb_pid = usb_status.get("usb_pid")
        if not usb_pid:
            msg = _("'usb_pid' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        instance_id = usb_status.get("instance_id")
        if not instance_id:
            msg = _("'instance_id' must be specified")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

        instance = common.get_instance(self.compute_api,
                                       context,
                                       instance_id)

        if(instance['vm_state'] != vm_states.ACTIVE
           or instance['power_state'] != power_state.RUNNING
           or instance['task_state'] is not None):
            raise exception.InstanceNotReady(instance_id=instance['uuid'])

        self._check_usb_dynamically(context, src_host_name,
                                    dst_host_name, usb_vid, usb_pid)

        status = self.compute_api.usb_status(context,
                                             instance,
                                             dst_host_name,
                                             usb_vid,
                                             usb_pid)

        return {'usb_status': status}

    def list_usb_project(self, req, body):
        context = req.environ['nova.context']
        authorize(context)

        list_usb_project = body.get("list_usb_project")
        usb_pid = list_usb_project.get("usb_pid")
        usb_vid = list_usb_project.get("usb_vid")
        result = db.USBAccessManagement_get(context, usb_pid, usb_vid)
        return result

    def usb_add_project(self, req, body):
        """"""
        context = req.environ['nova.context']
        authorize(context)

        usb_add = body.get("usb_add")
        usb_pid = usb_add.get("usb_pid")
        usb_vid = usb_add.get("usb_vid")
        project_id = usb_add.get("project_id")
        try:
            db.USBAccessManagement_add(context, usb_pid, usb_vid, project_id)
        except Exception:
            msg = _("Failed to insert data to database")
            raise exc.HTTPUnprocessableEntity(explanation=msg)

    def usb_delete_project(self, req, body):
        """"""
        context = req.environ['nova.context']
        authorize(context)

        usb_delete = body.get("usb_delete")
        usb_pid = usb_delete.get("usb_pid")
        usb_vid = usb_delete.get("usb_vid")
        project_id = usb_delete.get("project_id")
        try:
            db.USBAccessManagement_delete(context,
                                          usb_pid,
                                          usb_vid,
                                          project_id)
        except Exception:
            msg = _("Failed to delete data from database")
            raise exc.HTTPUnprocessableEntity(explanation=msg)


class UsbMapping(extensions.V21APIExtensionBase):
    """Usb actions between hosts and instances"""

    name = "UsbMapping"
    alias = ALIAS
    version = 1

    def get_resources(self):
        actions = {'usb_shared': 'POST',
                   'usb_mapped': 'POST',
                   'usb_mounted': 'POST',
                   "usb_status": 'POST',
                   "list_usb_project": 'POST',
                   "usb_add_project": 'POST',
                   "usb_delete_project": 'POST'}
        resources = extensions.ResourceExtension(ALIAS,
                                                 UsbMappingController(),
                                                 collection_actions=actions)

        return [resources]

    def get_controller_extensions(self):
        return []
