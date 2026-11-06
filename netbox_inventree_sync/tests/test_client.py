"""Hermetic tests for the InvenTree REST client (mocked HTTP via `responses`).

No network access and no NetBox models: these tests pin the transport
contract — OPTIONS field-spec parsing, DRF pagination, match fallback,
error wrapping and the field-spec cache TTL.
"""

import copy
import json
import unittest
from unittest import mock

import requests
import responses
from responses import matchers

from netbox_inventree_sync.sync.client import (
    InvenTreeClient,
    InvenTreeClientError,
    InventreeFieldSpec,
)

BASE_URL = 'http://inventree.test'
TOKEN = 'secret-token'


def part_options_fixture():
    """OPTIONS /api/part/ metadata shaped like InvenTree 1.7.0's response."""
    post = {
        'name': {
            'type': 'string', 'required': True, 'read_only': False,
            'label': 'Name', 'help_text': 'Part name', 'max_length': 100,
        },
        'IPN': {
            'type': 'string', 'required': False, 'read_only': False,
            'label': 'IPN', 'max_length': 100,
        },
        'active': {
            'type': 'boolean', 'required': False, 'read_only': False,
            'label': 'Active',
        },
        'minimum_stock': {
            'type': 'integer', 'required': False, 'read_only': False,
            'label': 'Minimum Stock',
        },
        'length': {
            'type': 'float', 'required': False, 'read_only': False,
            'label': 'Length',
        },
        'category': {
            'type': 'related field', 'required': False, 'read_only': False,
            'label': 'Category', 'model': 'partcategory',
            'api_url': '/api/part/category/', 'pk_field': 'pk',
        },
        'creation_date': {
            'type': 'date', 'required': False, 'read_only': True,
            'label': 'Creation Date',
        },
    }
    return {'actions': {'POST': post, 'PUT': copy.deepcopy(post)}}


def page(results, next_url=None):
    """One DRF pagination page."""
    return {'count': len(results) * 10, 'next': next_url, 'previous': None, 'results': results}


class InvenTreeClientTestCase(unittest.TestCase):

    def setUp(self):
        self.client = InvenTreeClient(BASE_URL, token=TOKEN)

    # --- OPTIONS introspection ------------------------------------------------

    @responses.activate
    def test_field_specs_parse(self):
        responses.add(responses.OPTIONS, f'{BASE_URL}/api/part/', json=part_options_fixture())

        specs = self.client.get_field_specs('/api/part/')
        by_name = {spec.name: spec for spec in specs}

        self.assertIsInstance(by_name['name'], InventreeFieldSpec)
        self.assertEqual(by_name['name'].type, 'string')
        self.assertTrue(by_name['name'].required)

        self.assertEqual(by_name['IPN'].type, 'string')
        self.assertEqual(by_name['IPN'].max_length, 100)

        self.assertEqual(by_name['active'].type, 'boolean')

        self.assertEqual(by_name['category'].type, 'related')
        self.assertEqual(by_name['category'].related_model, 'partcategory')

        self.assertEqual(by_name['minimum_stock'].type, 'integer')
        self.assertEqual(by_name['length'].type, 'float')

        # Read-only fields must not appear in the specs.
        self.assertNotIn('creation_date', by_name)

        # Token auth header must be used.
        self.assertEqual(
            responses.calls[0].request.headers['Authorization'],
            f'Token {TOKEN}',
        )

    @responses.activate
    def test_field_specs_prefer_put_when_present(self):
        options = part_options_fixture()
        options['actions']['PUT']['IPN']['max_length'] = 50
        responses.add(responses.OPTIONS, f'{BASE_URL}/api/part/', json=options)

        specs = {spec.name: spec for spec in self.client.get_field_specs('/api/part/')}
        self.assertEqual(specs['IPN'].max_length, 50)

    def test_field_specs_cached_until_ttl_expiry(self):
        monotonic_base = 1000.0
        clock = {'now': monotonic_base}

        def fake_monotonic():
            return clock['now']

        with responses.RequestsMock(assert_all_requests_are_fired=True) as rsps:
            rsps.add(responses.OPTIONS, f'{BASE_URL}/api/part/', json=part_options_fixture())
            rsps.add(responses.OPTIONS, f'{BASE_URL}/api/part/', json=part_options_fixture())

            with mock.patch(
                'netbox_inventree_sync.sync.client.monotonic', fake_monotonic
            ):
                first = self.client.get_field_specs('/api/part/')
                # Second call inside the TTL: served from cache, no HTTP.
                second = self.client.get_field_specs('/api/part/')
                self.assertEqual(
                    [s.name for s in first], [s.name for s in second]
                )

                # Advance the clock past the TTL: the next call re-fetches.
                clock['now'] = monotonic_base + InvenTreeClient.FIELD_SPEC_TTL_SECONDS + 1
                third = self.client.get_field_specs('/api/part/')
                self.assertEqual(
                    [s.name for s in first], [s.name for s in third]
                )

            # Exactly two OPTIONS requests were fired (initial + post-TTL
            # refetch); the mid-TTL call was served from cache. Asserted
            # before exiting the context, which resets the call list.
            self.assertEqual(len(rsps.calls), 2)

    # --- test_connection -------------------------------------------------------

    @responses.activate
    def test_test_connection_returns_payload(self):
        responses.add(
            responses.GET, f'{BASE_URL}/api/',
            json={'user': {'name': 'admin'}, 'version': '1.7.0'},
        )

        payload = self.client.test_connection()

        self.assertEqual(payload['user']['name'], 'admin')
        self.assertEqual(payload['version'], '1.7.0')

    # --- list / pagination -----------------------------------------------------

    @responses.activate
    def test_list_follows_pagination(self):
        responses.add(
            responses.GET, f'{BASE_URL}/api/part/',
            json=page([{'pk': 1}, {'pk': 2}], next_url=f'{BASE_URL}/api/part/?page=2'),
        )
        responses.add(
            responses.GET, f'{BASE_URL}/api/part/?page=2',
            json=page([{'pk': 3}]),
        )

        items = self.client.list('/api/part/')

        self.assertEqual([item['pk'] for item in items], [1, 2, 3])
        self.assertIn('page=2', responses.calls[1].request.url)

    @responses.activate
    def test_list_supports_unpaginated_endpoint(self):
        responses.add(responses.GET, f'{BASE_URL}/api/part/', json=[{'pk': 1}, {'pk': 2}])

        items = self.client.list('/api/part/')

        self.assertEqual(items, [{'pk': 1}, {'pk': 2}])

    @responses.activate
    def test_list_raises_after_pagination_cap(self):
        # 100 pages, all still carrying a `next` link: the guard must trip.
        responses.add(
            responses.GET, f'{BASE_URL}/api/part/',
            json=page([{'pk': 1}], next_url=f'{BASE_URL}/api/part/?page=2'),
        )
        for number in range(2, InvenTreeClient.MAX_PAGES + 1):
            responses.add(
                responses.GET, f'{BASE_URL}/api/part/',
                match=[matchers.query_param_matcher({'page': str(number)})],
                json=page([{'pk': number}], next_url=f'{BASE_URL}/api/part/?page={number + 1}'),
            )

        with self.assertRaises(InvenTreeClientError) as ctx:
            self.client.list('/api/part/')

        self.assertIn(str(InvenTreeClient.MAX_PAGES), str(ctx.exception))
        # The 101st page was never requested.
        self.assertNotIn('page=101', responses.calls[-1].request.url)

    # --- get / post / patch ------------------------------------------------------

    @responses.activate
    def test_get_post_patch_round_trip(self):
        responses.add(responses.GET, f'{BASE_URL}/api/part/7/', json={'pk': 7, 'name': 'Widget'})
        responses.add(
            responses.POST, f'{BASE_URL}/api/part/',
            json={'pk': 8, 'name': 'New'}, status=201,
        )
        responses.add(responses.PATCH, f'{BASE_URL}/api/part/7/', json={'pk': 7, 'name': 'Renamed'})

        self.assertEqual(self.client.get('/api/part/', 7)['name'], 'Widget')
        self.assertEqual(self.client.post('/api/part/', {'name': 'New'})['pk'], 8)
        self.assertEqual(self.client.patch('/api/part/', 7, {'name': 'Renamed'})['name'], 'Renamed')

        self.assertEqual(json.loads(responses.calls[1].request.body), {'name': 'New'})
        self.assertEqual(json.loads(responses.calls[2].request.body), {'name': 'Renamed'})

    # --- match -------------------------------------------------------------------

    @responses.activate
    def test_match_uses_filter_param(self):
        responses.add(
            responses.GET, f'{BASE_URL}/api/part/',
            match=[matchers.query_param_matcher({'name': 'Widget'}, strict_match=True)],
            json=page([{'pk': 1, 'name': 'Widget'}]),
        )

        hits = self.client.match('/api/part/', 'name', 'Widget')

        self.assertEqual(hits, [{'pk': 1, 'name': 'Widget'}])

    @responses.activate
    def test_match_narrows_silently_ignored_filter(self):
        """A server that ignores an unregistered ?field= filter (DRF's
        DjangoFilterBackend drops unknown params instead of erroring —
        InvenTree 1.7 does this for /api/part/?name=) hands back the whole
        collection; match() must narrow to exact matches itself or the
        engine adopts an unrelated object / fails as spurious "ambiguous".
        """
        responses.add(
            responses.GET, f'{BASE_URL}/api/part/',
            match=[matchers.query_param_matcher({'name': 'Widget'}, strict_match=True)],
            json=page([
                {'pk': 1, 'name': 'Bolt'},       # unrelated
                {'pk': 2, 'name': 'Widget'},     # the only exact match
                {'pk': 3, 'name': 'Widget Pro'},  # not an exact match
            ]),
        )

        hits = self.client.match('/api/part/', 'name', 'Widget')

        self.assertEqual(hits, [{'pk': 2, 'name': 'Widget'}])

    @responses.activate
    def test_match_falls_back_to_search_and_filters_exactly(self):
        # The ?name= filter is rejected...
        responses.add(
            responses.GET, f'{BASE_URL}/api/part/',
            match=[matchers.query_param_matcher({'name': 'Widget'}, strict_match=True)],
            status=400, json={'detail': 'Invalid filter'},
        )
        # ...so the client retries with ?search= (fuzzy) and narrows itself.
        responses.add(
            responses.GET, f'{BASE_URL}/api/part/',
            match=[matchers.query_param_matcher({'search': 'Widget'}, strict_match=True)],
            json=page([{'pk': 1, 'name': 'Widget'}, {'pk': 2, 'name': 'Widget Pro'}]),
        )

        hits = self.client.match('/api/part/', 'name', 'Widget')

        self.assertEqual(hits, [{'pk': 1, 'name': 'Widget'}])
        # The filter attempt must have been tried first.
        self.assertIn('name=Widget', responses.calls[0].request.url)
        self.assertIn('search=Widget', responses.calls[1].request.url)

    # --- error handling ------------------------------------------------------------

    @responses.activate
    def test_error_raises_client_error_with_body(self):
        responses.add(
            responses.POST, f'{BASE_URL}/api/part/',
            status=400, body='Serial number invalid',
        )

        with self.assertRaises(InvenTreeClientError) as ctx:
            self.client.post('/api/part/', {'name': 'Widget'})

        error = ctx.exception
        self.assertEqual(error.status, 400)
        self.assertIn('Serial number invalid', error.body)
        # The token lives in a header, never in the error message.
        self.assertNotIn(TOKEN, str(error))

    @responses.activate
    def test_connection_error_raises_client_error(self):
        responses.add(
            responses.GET, f'{BASE_URL}/api/',
            body=requests.exceptions.ConnectionError('connection refused'),
        )

        with self.assertRaises(InvenTreeClientError) as ctx:
            self.client.test_connection()

        self.assertIsNone(ctx.exception.status)
