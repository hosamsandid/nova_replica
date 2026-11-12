# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from nova.api.openstack.compute import volumes_boot
from nova import exception
from nova import test
from nova.tests.unit.api.openstack import fakes


class VolumesBootControllerDeprecationTest(test.NoDBTestCase):

    def setUp(self):
        super().setUp()
        self.controller = volumes_boot.VolumesBootController()
        self.req = fakes.HTTPRequest.blank('', version='2.103')

    def test_not_found(self):
        for method in (
            self.controller.index,
            self.controller.detail,
        ):
            self.assertRaises(
                exception.VersionNotFoundForAPIMethod, method, self.req
            )

        for method in (
            self.controller.show,
            self.controller.delete,
        ):
            self.assertRaises(
                exception.VersionNotFoundForAPIMethod, method, self.req, 123
            )

        for method in (
            self.controller.update,
            self.controller._confirm_resize,
            self.controller._revert_resize,
            self.controller._reboot,
            self.controller._resize,
            self.controller._rebuild,
            self.controller._create_image,
            self.controller._start,
            self.controller._stop,
            self.controller._trigger_crash_dump,
        ):
            self.assertRaises(
                exception.VersionNotFoundForAPIMethod,
                method,
                self.req,
                123,
                # intentionally incomplete body since version validation
                # happens before schema validation
                body={},
            )
