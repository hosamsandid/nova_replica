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

import nova.conf
from nova.conf import base
from nova import test


CONF = nova.conf.CONF


class BaseConfTestCase(test.NoDBTestCase):

    def test_graceful_shutdown_timeout_default(self):
        # Check that CONF.graceful_shutdown_timeout default is overridden
        # by the Nova.
        self.assertEqual(
            base.NOVA_DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT,
            CONF.graceful_shutdown_timeout)
