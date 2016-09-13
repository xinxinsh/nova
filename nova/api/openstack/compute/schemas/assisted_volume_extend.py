# Copyright 2016 Chinac Corporation.  All rights reserved.
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

volume_extend = {
    'type': 'object',
    'properties': {
        'volume': {
            'type': 'object',
            'properties': {
                'volume_id': {
                    'type': 'string', 'minLength': 1,
                },
                'extend_info': {
                    'type': 'object',
                    'properties': {
                        'type': {
                            'type': 'string', 'enum': ['qcow2', 'rbd', 'fc'],
                        },
                        'new_size': {
                            'type': ['integer', 'string'],
                            'pattern': '^[0-9]+$',
                            'minLength': 1,
                        },
                        'id': {
                            'type': 'string', 'minLength': 1,
                        },
                    },
                    'required': ['type', 'new_size'],
                    'additionalProperties': False,
                 },
            },
            'required': ['volume_id', 'extend_info'],
            'additionalProperties': False,
        },
        'required': ['volume'],
        'additionalProperties': False,
    },
}
