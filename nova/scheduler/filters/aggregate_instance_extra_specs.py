# Copyright (c) 2012 OpenStack Foundation
# Copyright (c) 2012 Cloudscaling
# All Rights Reserved.
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

from oslo_log import log as logging
import six

from nova.scheduler import filters
from nova.scheduler.filters import extra_specs_ops
from nova.scheduler.filters import utils


LOG = logging.getLogger(__name__)

_SCOPE = 'aggregate_instance_extra_specs'


class AggregateInstanceExtraSpecsFilter(filters.BaseHostFilter):
    """AggregateInstanceExtraSpecsFilter works with InstanceType records."""

    # Aggregate data and instance type does not change within a request
    run_filter_once_per_request = True

    def host_passes(self, host_state, spec_obj):
        """Return a list of hosts that can create instance_type

        Check that the extra specs associated with the instance type match
        the metadata provided by aggregates.  If not present return False.
        """
        instance_type = spec_obj.flavor

        # add method check host metadata for custom config
        def check_host_metata_for_custom(host_state):
            metadata = utils.aggregate_metadata_get_by_host(host_state)
            # delete availability_zone key value
            # for host in aggragate but without any key value
            az_key = 'availability_zone'
            if az_key in metadata:
                del metadata[az_key]
            # if dict is empty,host is without any aggregate
            # else dict is not empty,host with aggregate not useful
            # for empty falvor extra_specs
            if not metadata:
                return True
            else:
                return False

        # If 'extra_specs' is not present or extra_specs are empty then we
        # need not proceed further
        if (not instance_type.obj_attr_is_set('extra_specs')
                or not instance_type.extra_specs):
            # when extra_specs is empty filter host without aggregate
            return check_host_metata_for_custom(host_state)

        # Add flow for 2003 hw:cpu_max_sockets
        # when only with hw:cpu_max_sockets not to aggregate host
        extra_spec = instance_type.extra_specs
        if len(extra_spec) == 1 and \
                extra_spec.keys()[0] == "hw:cpu_max_sockets":
            return check_host_metata_for_custom(host_state)

        # Add flow for flavor only with cpu_max and cpu_mem
        # when with cpu_max and cpu_mem not to aggregate host
        extra_spec = instance_type.extra_specs
        if "cpu_max" in extra_spec.keys() and \
           "mem_max" in extra_spec.keys():
            if len(extra_spec) == 2 or ("lr_sha224" in extra_spec.keys()
               and len(extra_spec) == 3):
                return check_host_metata_for_custom(host_state)

        if 'hw:vif_multiqueue_enabled' in extra_spec.keys() \
                and len(extra_spec) == 1:
            return check_host_metata_for_custom(host_state)

        if 'flavor_type' in extra_spec.keys() \
                and 'hw:vif_multiqueue_enabled' in extra_spec.keys() \
                and len(extra_spec) == 2:
            return check_host_metata_for_custom(host_state)

        if 'flavor_type' in extra_spec.keys() \
                and 'hw:vif_multiqueue_enabled' in extra_spec.keys() \
                and 'hw:cpu_max_sockets' in extra_spec.keys() \
                and len(extra_spec) == 3:
            return check_host_metata_for_custom(host_state)

        metadata = utils.aggregate_metadata_get_by_host(host_state)

        for key, req in six.iteritems(instance_type.extra_specs):
            # Either not scope format, or aggregate_instance_extra_specs scope
            scope = key.split(':', 1)
            if len(scope) >= 1:
                if scope[0] != _SCOPE:
                    continue
                else:
                    del scope[0]
            # for aggregate_instance_extra_specs:1 this case
            if len(scope) < 1:
                continue
            key = scope[0]
            aggregate_vals = metadata.get(key, None)
            if not aggregate_vals:
                LOG.debug("%(host_state)s fails instance_type extra_specs "
                    "requirements. Extra_spec %(key)s is not in aggregate.",
                    {'host_state': host_state, 'key': key})
                return False
            for aggregate_val in aggregate_vals:
                if extra_specs_ops.match(aggregate_val, req):
                    break
            else:
                LOG.debug("%(host_state)s fails instance_type extra_specs "
                            "requirements. '%(aggregate_vals)s' do not "
                            "match '%(req)s'",
                          {'host_state': host_state, 'req': req,
                           'aggregate_vals': aggregate_vals})
                return False
        return True
