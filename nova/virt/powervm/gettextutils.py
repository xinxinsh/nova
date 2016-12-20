# =================================================================
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

"""
gettext functionality for nova.

Usage in nova modules:

    from nova.gettextutils import _
"""

import copy
import gettext
import inspect
import re
import six
import time


t = gettext.translation('nova', fallback=True)
T_ = t.ugettext

RLMAP = {}

# minimum amount in seconds before cleaning up an entry
# in RLMAP since it was added to the dict
EXP_TIME = 30


def _lazy_gettext(msg):
    """Create and return a Message object encapsulating a string
    so that we can translate it later when needed.

    If the message has no parameters, just return the string
    after remembering it for later translation.
    """
    # if the message has a %s or %(something)s or similar
    # return a Message object to encapsulate it and
    # capture the injected parameters;
    # ignore %% and deal with case where % is first character on the line
    if re.findall('(?:[^%]|^)%\((\w*)\)[a-z]|(?:[^%]|^)%[a-z]', msg):
        return Message(msg)
    else:
        translated = T_(msg)
        add_to_map(translated, msg)
        return translated


class Message(object):
    """Class used to encapsulate localizable messages."""
    def __init__(self, msg=''):
        self.message = msg
        self.extra_msg = ''
        self.params = None
        self.locale = None

    def _serialize_other(self, other):
        """Helper method that checks for python code-like objects
        and turns them into strings instead of trying to carry
        the full object around.
        """
        needs_str = [inspect.ismodule, inspect.isclass, inspect.ismethod,
                     inspect.isfunction, inspect.isgeneratorfunction,
                     inspect.isgenerator, inspect.istraceback,
                     inspect.isframe, inspect.iscode,
                     inspect.isbuiltin, inspect.isroutine,
                     inspect.isabstract, inspect.ismethoddescriptor,
                     inspect.isdatadescriptor, inspect.isgetsetdescriptor,
                     inspect.ismemberdescriptor]

        for atype in needs_str:
            if atype(other):
                return unicode(other)

        return None

    def __mod__(self, other):
        if isinstance(other, str):
            self.params = other
        elif isinstance(other, dict):
            full_msg = self.message + self.extra_msg
            # look for %(blah) fields in string;
            # ignore %% and deal with the
            # case where % is first character on the line
            keys = re.findall('(?:[^%]|^)%\((\w*)\)[a-z]', full_msg)

            self.params = {}
            for key in keys:
                try:
                    self.params[key] = copy.deepcopy(other[key])
                except Exception:
                    # cast uncopyable thing to string
                    self.params[key] = str(other[key])
        else:
            # attempt to cast nasty python object to string
            serialized_other = self._serialize_other(other)
            if serialized_other is not None:
                self.params = serialized_other
            else:
                copied = copy.copy(other)
                # we check for None later to see if
                # we actually have parameters to inject,
                # so encapsulate if our parameter is actually None
                if copied is None:
                    self.params = (copied, )
                else:
                    self.params = copied

        # for now, just remember the information about
        # the message and return a string we can look up
        # later on when we need to translate it
        translated = self.__str__()
        add_to_map(translated, self.message,
                   extra_string=self.extra_msg,
                   params=self.params)
        return translated

    def __add__(self, other):
        self.extra_msg += other.__str__()

    def __unicode__(self):
        """Behave like a string when needed

        Localize the message and inject parameters when
        we are requested to behave like a string (unicode) type
        """
        if self.locale:
            try:
                lang = gettext.translation('nova', languages=[self.locale])
                full_msg = lang.ugettext(self.message) + self.extra_msg
            except IOError:
                # no locale found so default to using no translation
                full_msg = self.message + self.extra_msg
        else:
            full_msg = T_(self.message) + self.extra_msg
        if self.params is not None:
            return unicode(full_msg % self.params)
        else:
            return unicode(full_msg)

    def __str__(self):
        """Behave like a string when needed

        Localize the message and inject parameters when
        we are requested to behave like a string (str) type
        """
        return unicode(self).encode('utf-8')

    def iteritems(self):
        local = dict(self.__dict__)
        return six.iteritems(local)

    def __getattr__(self, name):
        supported = ['find']
        if name in supported:
            return getattr(unicode(self), name)
        raise AttributeError()


def add_to_map(key, base_string, extra_string='', params=None):
    message = Message(base_string)
    message.extra_msg = extra_string
    message.params = params

    RLMAP[key] = {'message': message,
                  'created_at': time.time()}

    _cleanup_map()


def get_message_from_map(key):
    base_msg = RLMAP.get(key, None)
    # no reverse translation found
    if base_msg is None:
        return None

    _cleanup_map()

    # return a Message since they are easy to translate
    new_msg = base_msg['message']
    return new_msg


def _cleanup_map():
    now = time.time()

    for key in RLMAP.keys():
        # use None as default for case where key was
        # removed while we are iterating
        value = RLMAP.get(key, None)
        if value is not None:
            if (value['created_at'] + EXP_TIME) < now:
                RLMAP.pop(key, None)


_ = _lazy_gettext
