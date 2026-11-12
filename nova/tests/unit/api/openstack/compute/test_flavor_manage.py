# Copyright 2011 Andrew Bogott for the Wikimedia Foundation
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

import copy
from unittest import mock

import webob

from nova.api.openstack.compute import flavor_access
from nova.api.openstack.compute import flavors
from nova.db import constants as db_const
from nova import exception
from nova import objects
from nova import test
from nova.tests.unit.api.openstack import fakes


def fake_create(newflavor):
    newflavor['flavorid'] = 1234
    newflavor["name"] = 'test'
    newflavor["memory_mb"] = 512
    newflavor["vcpus"] = 2
    newflavor["root_gb"] = 1
    newflavor["ephemeral_gb"] = 1
    newflavor["swap"] = 512
    newflavor["rxtx_factor"] = 1.0
    newflavor["is_public"] = True
    newflavor["disabled"] = False


def fake_create_without_swap(newflavor):
    newflavor['flavorid'] = 1234
    newflavor["name"] = 'test'
    newflavor["memory_mb"] = 512
    newflavor["vcpus"] = 2
    newflavor["root_gb"] = 1
    newflavor["ephemeral_gb"] = 1
    newflavor["swap"] = 0
    newflavor["rxtx_factor"] = 1.0
    newflavor["is_public"] = True
    newflavor["disabled"] = False
    newflavor["extra_specs"] = {"key1": "value1"}


class FlavorManageTestV21(test.NoDBTestCase):
    microversion = '2.1'

    def setUp(self):
        super().setUp()
        self.stub_out("nova.objects.Flavor.create", fake_create)

        self.request_body = {
            "flavor": {
                "name": "test",
                "ram": 512,
                "vcpus": 2,
                "disk": 1,
                "OS-FLV-EXT-DATA:ephemeral": 1,
                "id": '1234',
                "swap": 512,
                "rxtx_factor": 1,
                "os-flavor-access:is_public": True,
            }
        }
        self.expected_flavor = self.request_body
        self.controller = flavors.FlavorsController()

    def _get_http_request(self, url=''):
        return fakes.HTTPRequest.blank(url, version=self.microversion,
                                       use_admin_context=True)

    @mock.patch('nova.objects.Flavor.destroy')
    def test_delete(self, mock_destroy):
        req = self._get_http_request()
        self.controller.delete(req, 1234)

        status_int = self.controller.delete.wsgi_codes(req)
        self.assertEqual(202, status_int)

        # subsequent delete should fail
        mock_destroy.side_effect = exception.FlavorNotFound(flavor_id=1234)
        self.assertRaises(webob.exc.HTTPNotFound,
                          self.controller.delete, self._get_http_request(),
                          1234)

    def _test_create_missing_parameter(self, parameter):
        body = {
            "flavor": {
                "name": "azAZ09. -_",
                "ram": 512,
                "vcpus": 2,
                "disk": 1,
                "OS-FLV-EXT-DATA:ephemeral": 1,
                "id": '1234',
                "swap": 512,
                "rxtx_factor": 1,
                "os-flavor-access:is_public": True,
            }
        }

        del body['flavor'][parameter]

        self.assertRaises(
            exception.ValidationError, self.controller.create,
            self._get_http_request(), body=body)

    def test_create_missing_name(self):
        self._test_create_missing_parameter('name')

    def test_create_missing_ram(self):
        self._test_create_missing_parameter('ram')

    def test_create_missing_vcpus(self):
        self._test_create_missing_parameter('vcpus')

    def test_create_missing_disk(self):
        self._test_create_missing_parameter('disk')

    def test_create(self):
        req = self._get_http_request('')
        body = self.controller.create(req, body=self.request_body)
        for key in self.expected_flavor["flavor"]:
            self.assertEqual(body["flavor"][key],
                             self.expected_flavor["flavor"][key])

    def test_create_public_default(self):
        del self.request_body['flavor']['os-flavor-access:is_public']
        req = self._get_http_request('')
        body = self.controller.create(req, body=self.request_body)
        for key in self.expected_flavor["flavor"]:
            self.assertEqual(body["flavor"][key],
                             self.expected_flavor["flavor"][key])

    def test_create_without_flavorid(self):
        del self.request_body['flavor']['id']
        req = self._get_http_request('')
        body = self.controller.create(req, body=self.request_body)
        for key in self.expected_flavor["flavor"]:
            self.assertEqual(body["flavor"][key],
                             self.expected_flavor["flavor"][key])

    def _create_flavor_bad_request_case(self, body):
        self.assertRaises(
            exception.ValidationError, self.controller.create,
            self._get_http_request(), body=body)

    def test_create_invalid_name(self):
        self.request_body['flavor']['name'] = 'bad !@#!$%\x00 name'
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_flavor_name_is_whitespace(self):
        self.request_body['flavor']['name'] = ' '
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_name_too_long(self):
        self.request_body['flavor']['name'] = 'a' * 256
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_short_name(self):
        self.request_body['flavor']['name'] = ''
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_name_leading_trailing_spaces(self):
        self.request_body['flavor']['name'] = '  test  '
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_name_leading_trailing_spaces_compat_mode(self):
        req = self._get_http_request('/v2.1/flavors')
        req.set_legacy_v2()
        self.request_body['flavor']['name'] = '  test  '
        body = self.controller.create(req, body=self.request_body)
        self.assertEqual('test', body['flavor']['name'])

    def test_create_without_flavorname(self):
        del self.request_body['flavor']['name']
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_empty_body(self):
        body = {
            "flavor": {}
        }
        self._create_flavor_bad_request_case(body)

    def test_create_no_body(self):
        body = {}
        self._create_flavor_bad_request_case(body)

    def test_create_invalid_format_body(self):
        body = {
            "flavor": []
        }
        self._create_flavor_bad_request_case(body)

    def test_create_invalid_flavorid(self):
        self.request_body['flavor']['id'] = "!@#!$#!$^#&^$&"
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_check_flavor_id_length(self):
        MAX_LENGTH = 255
        self.request_body['flavor']['id'] = "a" * (MAX_LENGTH + 1)
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_leading_trailing_whitespaces_in_flavor_id(self):
        self.request_body['flavor']['id'] = "   bad_id   "
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_without_ram(self):
        del self.request_body['flavor']['ram']
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_0_ram(self):
        self.request_body['flavor']['ram'] = 0
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_ram_exceed_max_limit(self):
        self.request_body['flavor']['ram'] = db_const.MAX_INT + 1
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_minus_ram(self):
        self.request_body['flavor']['ram'] = -1
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_invalid_ram(self):
        self.request_body['flavor']['ram'] = 'invalid'
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_without_vcpus(self):
        del self.request_body['flavor']['vcpus']
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_0_vcpus(self):
        self.request_body['flavor']['vcpus'] = 0
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_vcpus_exceed_max_limit(self):
        self.request_body['flavor']['vcpus'] = db_const.MAX_INT + 1
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_minus_vcpus(self):
        self.request_body['flavor']['vcpus'] = -1
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_invalid_vcpus(self):
        self.request_body['flavor']['vcpus'] = 'invalid'
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_without_disk(self):
        del self.request_body['flavor']['disk']
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_disk_exceed_max_limit(self):
        self.request_body['flavor']['disk'] = db_const.MAX_INT + 1
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_minus_disk(self):
        self.request_body['flavor']['disk'] = -1
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_invalid_disk(self):
        self.request_body['flavor']['disk'] = 'invalid'
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_minus_ephemeral(self):
        self.request_body['flavor']['OS-FLV-EXT-DATA:ephemeral'] = -1
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_invalid_ephemeral(self):
        self.request_body['flavor']['OS-FLV-EXT-DATA:ephemeral'] = 'invalid'
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_ephemeral_exceed_max_limit(self):
        self.request_body['flavor'][
            'OS-FLV-EXT-DATA:ephemeral'] = db_const.MAX_INT + 1
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_minus_swap(self):
        self.request_body['flavor']['swap'] = -1
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_invalid_swap(self):
        self.request_body['flavor']['swap'] = 'invalid'
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_swap_exceed_max_limit(self):
        self.request_body['flavor']['swap'] = db_const.MAX_INT + 1
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_minus_rxtx_factor(self):
        self.request_body['flavor']['rxtx_factor'] = -1
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_rxtx_factor_exceed_max_limit(self):
        self.request_body['flavor']['rxtx_factor'] = \
            db_const.SQL_SP_FLOAT_MAX * 2
        self._create_flavor_bad_request_case(self.request_body)

    def test_create_with_non_boolean_is_public(self):
        self.request_body['flavor']['os-flavor-access:is_public'] = 123
        self._create_flavor_bad_request_case(self.request_body)

    def test_flavor_exists_exception_returns_409(self):
        expected = {
            "flavor": {
                "name": "test",
                "ram": 512,
                "vcpus": 2,
                "disk": 1,
                "OS-FLV-EXT-DATA:ephemeral": 1,
                "id": 1235,
                "swap": 512,
                "rxtx_factor": 1,
                "os-flavor-access:is_public": True,
            }
        }

        def fake_create(name, memory_mb, vcpus, root_gb, ephemeral_gb,
                        flavorid, swap, rxtx_factor, is_public, description):
            raise exception.FlavorExists(name=name)

        self.stub_out('nova.compute.flavors.create', fake_create)
        self.assertRaises(webob.exc.HTTPConflict, self.controller.create,
                          self._get_http_request(), body=expected)

    def test_create_with_description(self):
        """With microversion <2.55 this should return a failure."""
        self.request_body['flavor']['description'] = 'invalid'
        ex = self.assertRaises(
            exception.ValidationError, self.controller.create,
            self._get_http_request(), body=self.request_body)
        self.assertIn('description', str(ex))

    def test_flavor_update_description(self):
        """With microversion <2.55 this should return a failure."""
        req = self._get_http_request('')
        flavor = self.controller.create(req, body=self.request_body)['flavor']
        self.assertRaises(
            exception.VersionNotFoundForAPIMethod, self.controller.update,
            self._get_http_request(), flavor['id'],
            body={'flavor': {'description': 'nope'}})


class FlavorManageTestV255(FlavorManageTestV21):
    microversion = '2.55'

    def get_flavor(self, flavor, **kwargs):
        return objects.Flavor(
            flavorid=flavor['id'], name=flavor['name'],
            memory_mb=flavor['ram'], vcpus=flavor['vcpus'],
            root_gb=flavor['disk'], swap=flavor['swap'],
            ephemeral_gb=flavor['OS-FLV-EXT-DATA:ephemeral'],
            disabled=flavor['OS-FLV-DISABLED:disabled'],
            is_public=flavor['os-flavor-access:is_public'],
            rxtx_factor=flavor['rxtx_factor'],
            description=flavor['description'],
            **kwargs)

    def setUp(self):
        super().setUp()
        # Send a description in POST /flavors requests.
        self.request_body['flavor']['description'] = 'test description'

    def test_create_with_description(self):
        # test_create already tests this.
        pass

    @mock.patch('nova.objects.Flavor.get_by_flavor_id')
    @mock.patch('nova.objects.Flavor.save')
    def test_flavor_update_description(self, mock_flavor_save, mock_get):
        """Tests updating a flavor description."""
        # First create a flavor.
        req = self._get_http_request('')
        flavor = self.controller.create(req, body=self.request_body)['flavor']
        self.assertEqual('test description', flavor['description'])
        mock_get.return_value = self.get_flavor(flavor)

        # Now null out the flavor description.
        flavor = self.controller.update(
            self._get_http_request(), flavor['id'],
            body={'flavor': {'description': None}})['flavor']
        self.assertIsNone(flavor['description'])
        mock_get.assert_called_once_with(
            test.MatchType(fakes.FakeRequestContext), flavor['id'])
        mock_flavor_save.assert_called_once_with()

    @mock.patch('nova.objects.Flavor.get_by_flavor_id',
                side_effect=exception.FlavorNotFound(flavor_id='notfound'))
    def test_flavor_update_not_found(self, mock_get):
        """Tests that a 404 is returned if the flavor is not found."""
        self.assertRaises(webob.exc.HTTPNotFound,
                          self.controller.update,
                          self._get_http_request(), 'notfound',
                          body={'flavor': {'description': None}})

    def test_flavor_update_missing_description(self):
        """Tests that a schema validation error is raised if no description
        is provided in the update request body.
        """
        self.assertRaises(exception.ValidationError,
                          self.controller.update,
                          self._get_http_request(), 'invalid',
                          body={'flavor': {}})

    def test_create_with_invalid_description(self):
        # NOTE(mriedem): Intentionally not using ddt for this since ddt will
        # create a test name that has 65536 'a's in the name which blows up
        # the console output.
        for description in ('bad !@#!$%\x00 description',   # printable chars
                            'a' * 65536):                   # maxLength
            self.request_body['flavor']['description'] = description
            self.assertRaises(
                exception.ValidationError, self.controller.create,
                self._get_http_request(), body=self.request_body)

    @mock.patch('nova.objects.Flavor.get_by_flavor_id')
    @mock.patch('nova.objects.Flavor.save')
    def test_update_with_invalid_description(self, mock_flavor_save, mock_get):
        # First create a flavor.
        req = self._get_http_request('')
        flavor = self.controller.create(req, body=self.request_body)['flavor']
        self.assertEqual('test description', flavor['description'])
        mock_get.return_value = objects.Flavor(
            flavorid=flavor['id'], name=flavor['name'],
            memory_mb=flavor['ram'], vcpus=flavor['vcpus'],
            root_gb=flavor['disk'], swap=flavor['swap'],
            ephemeral_gb=flavor['OS-FLV-EXT-DATA:ephemeral'],
            disabled=flavor['OS-FLV-DISABLED:disabled'],
            is_public=flavor['os-flavor-access:is_public'],
            description=flavor['description'])
        # NOTE(mriedem): Intentionally not using ddt for this since ddt will
        # create a test name that has 65536 'a's in the name which blows up
        # the console output.
        for description in ('bad !@#!$%\x00 description',   # printable chars
                            'a' * 65536):                   # maxLength
            self.request_body['flavor']['description'] = description
            self.assertRaises(
                exception.ValidationError, self.controller.update,
                self._get_http_request(), flavor['id'],
                body={'flavor': {'description': description}})


class FlavorManageTestV261(FlavorManageTestV255):
    """Run the same tests as we would for v2.55 but with a extra_specs."""
    microversion = '2.61'

    def get_flavor(self, flavor):
        return super().get_flavor(
            flavor, extra_specs={"key1": "value1"})

    def setUp(self):
        super().setUp()
        self.expected_flavor = copy.deepcopy(self.request_body)
        self.expected_flavor['flavor']['extra_specs'] = {}

    @mock.patch('nova.objects.Flavor.get_by_flavor_id')
    @mock.patch('nova.objects.Flavor.save')
    def test_flavor_update_extra_spec(self, mock_flavor_save, mock_get):
        # First create a flavor.
        req = self._get_http_request('')
        flavor = self.controller.create(req, body=self.request_body)['flavor']
        mock_get.return_value = self.get_flavor(flavor)
        flavor = self.controller.update(
            self._get_http_request(), flavor['id'],
            body={'flavor': {'description': None}})['flavor']
        self.assertEqual({"key1": "value1"}, flavor['extra_specs'])


class FlavorManageTestV275(FlavorManageTestV261):
    microversion = '2.75'

    FLAVOR_WITH_NO_SWAP = objects.Flavor(
        name='test',
        memory_mb=512,
        vcpus=2,
        root_gb=1,
        ephemeral_gb=1,
        flavorid=1234,
        rxtx_factor=1.0,
        disabled=False,
        is_public=True,
        swap=0,
        extra_specs={"key1": "value1"}
    )

    def test_create_flavor_default_swap_value_old_version(self):
        self.stub_out("nova.objects.Flavor.create", fake_create_without_swap)
        del self.request_body['flavor']['swap']
        req = fakes.HTTPRequest.blank(
            '', version='2.74', use_admin_context=True)
        resp = self.controller.create(req, body=self.request_body)
        self.assertEqual(resp['flavor']['swap'], "")

    @mock.patch('nova.objects.Flavor.get_by_flavor_id')
    @mock.patch('nova.objects.Flavor.save')
    def test_update_flavor_default_swap_value_old_version(self, mock_save,
                                                          mock_get):
        self.stub_out("nova.objects.Flavor.create", fake_create_without_swap)
        del self.request_body['flavor']['swap']
        req = fakes.HTTPRequest.blank(
            '', version='2.74', use_admin_context=True)
        flavor = self.controller.create(req, body=self.request_body)['flavor']

        mock_get.return_value = self.FLAVOR_WITH_NO_SWAP
        flavor = self.controller.update(
            req, flavor['id'],
            body={'flavor': {'description': None}})['flavor']
        self.assertEqual(flavor['swap'], '')

    @mock.patch('nova.objects.FlavorList.get_all')
    def test_create_flavor_default_swap_value(self, mock_get):
        self.stub_out("nova.objects.Flavor.create", fake_create_without_swap)
        del self.request_body['flavor']['swap']
        req = self._get_http_request('')
        body = self.controller.create(req, body=self.request_body)
        self.assertEqual(body['flavor']['swap'], 0)

    @mock.patch('nova.objects.Flavor.get_by_flavor_id')
    @mock.patch('nova.objects.Flavor.save')
    def test_update_flavor_default_swap_value(self, mock_save, mock_get):
        self.stub_out("nova.objects.Flavor.create", fake_create_without_swap)
        del self.request_body['flavor']['swap']
        mock_get.return_value = self.FLAVOR_WITH_NO_SWAP
        req = self._get_http_request('')
        flavor = self.controller.create(req, body=self.request_body)['flavor']

        flavor = self.controller.update(
            req, flavor['id'],
            body={'flavor': {'description': None}})['flavor']
        self.assertEqual(flavor['swap'], 0)


class PrivateFlavorManageTestV21(test.TestCase):

    def setUp(self):
        super().setUp()
        self.flavor_controller = flavors.FlavorsController()
        self.flavor_access_controller = flavor_access.FlavorAccessController()
        self.expected = {
            "flavor": {
                "name": "test",
                "ram": 512,
                "vcpus": 2,
                "disk": 1,
                "OS-FLV-EXT-DATA:ephemeral": 1,
                "swap": 512,
                "rxtx_factor": 1
            }
        }

    def test_create_private_flavor_should_not_grant_flavor_access(self):
        self.expected["flavor"]["os-flavor-access:is_public"] = False
        body = self.flavor_controller.create(
            fakes.HTTPRequest.blank(''), body=self.expected
        )
        for key in self.expected["flavor"]:
            self.assertEqual(body["flavor"][key], self.expected["flavor"][key])

        # Because for normal user can't access the non-public flavor without
        # access. So it need admin context at here.
        flavor_access_body = self.flavor_access_controller.index(
            fakes.HTTPRequest.blank('', use_admin_context=True),
            body["flavor"]["id"])
        expected_flavor_access_body = {
            "tenant_id": fakes.FAKE_PROJECT_ID,
            "flavor_id": "%s" % body["flavor"]["id"]
        }
        self.assertNotIn(expected_flavor_access_body,
                         flavor_access_body["flavor_access"])

    def test_create_public_flavor_should_not_create_flavor_access(self):
        self.expected["flavor"]["os-flavor-access:is_public"] = True
        body = self.flavor_controller.create(
            fakes.HTTPRequest.blank(''), body=self.expected
        )
        for key in self.expected["flavor"]:
            self.assertEqual(body["flavor"][key], self.expected["flavor"][key])
