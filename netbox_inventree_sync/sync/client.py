"""Pure HTTP transport for the InvenTree REST API.

Transport only: token auth, DRF pagination, OPTIONS metadata parsing and
error wrapping. Deliberately free of NetBox imports so it can be tested
hermetically (see ``netbox_inventree_sync.tests.test_client``).
"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

import requests

# DRF metadata types we normalize; anything else passes through as-is
# (lower-cased), e.g. 'date' or 'choice'.
_TYPE_ALIASES = {
    'related field': 'related',
}


@dataclass(frozen=True)
class InventreeFieldSpec:
    """Normalized metadata for a single InvenTree endpoint field.

    ``type`` is one of ``string|boolean|integer|float|related`` (related
    fields carry ``related_model``); read-only fields are never emitted.
    """

    name: str
    type: str
    required: bool
    max_length: int | None
    related_model: str | None


class InvenTreeClientError(Exception):
    """Any failure while talking to the InvenTree API.

    ``status`` is the HTTP status code (None for connection-level failures)
    and ``body`` the raw response text when a response was received. Messages
    never contain the API token (it lives in a request header only).
    """

    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class InvenTreeClient:
    """Small read/write REST client for one InvenTree server."""

    FIELD_SPEC_TTL_SECONDS = 300  # 5-minute OPTIONS cache
    MAX_PAGES = 100  # pagination guard

    def __init__(self, url: str, token: str, timeout: tuple = (10, 30)):
        self.url = url.rstrip('/')
        self.token = token
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
        })
        # (url, endpoint) -> (monotonic timestamp, specs); per-instance so
        # distinct client objects never share cache state.
        self._spec_cache: dict[tuple[str, str], tuple[float, list[InventreeFieldSpec]]] = {}

    # --- public API -----------------------------------------------------------

    def test_connection(self) -> dict:
        """GET ``/api/``; returns the server info dict (``user``, ``version``)."""
        return self._request_json('GET', '/api/')

    def get_field_specs(self, endpoint: str) -> list[InventreeFieldSpec]:
        """OPTIONS ``endpoint`` -> normalized field specs.

        ``actions.POST`` and ``actions.PUT`` are merged with PUT preferred on
        conflict; read-only fields are skipped. Cached per (url, endpoint)
        with a 5-minute TTL.
        """
        cache_key = (self.url, endpoint)
        cached = self._spec_cache.get(cache_key)
        if cached is not None and monotonic() - cached[0] < self.FIELD_SPEC_TTL_SECONDS:
            return list(cached[1])

        payload = self._request_json('OPTIONS', endpoint)
        actions = payload.get('actions', {}) if isinstance(payload, dict) else {}
        merged: dict[str, dict] = {}
        for method in ('POST', 'PUT'):  # later key wins -> PUT preferred
            for name, meta in (actions.get(method) or {}).items():
                merged[name] = meta

        specs = []
        for name, meta in merged.items():
            if not isinstance(meta, dict) or meta.get('read_only'):
                continue
            field_type = self._normalize_type(meta.get('type', ''))
            specs.append(InventreeFieldSpec(
                name=name,
                type=field_type,
                required=bool(meta.get('required', False)),
                max_length=meta.get('max_length'),
                related_model=meta.get('model') if field_type == 'related' else None,
            ))

        self._spec_cache[cache_key] = (monotonic(), specs)
        return list(specs)

    def list(self, endpoint: str, params: dict | None = None) -> list[dict]:
        """GET ``endpoint`` and follow DRF pagination (``results``/``next``) to the end.

        Endpoints without pagination (a bare JSON list) are also supported.
        Raises :class:`InvenTreeClientError` if pagination exceeds
        :attr:`MAX_PAGES` pages, to guard against runaway traversal.
        """
        results: list[dict] = []
        payload = self._request_json('GET', endpoint, params=params)
        pages = 0
        while True:
            pages += 1
            if isinstance(payload, list):
                results.extend(payload)
                break
            results.extend(payload.get('results') or [])
            next_url = payload.get('next')
            if not next_url:
                break
            if pages >= self.MAX_PAGES:
                raise InvenTreeClientError(
                    f'pagination for {endpoint} exceeded {self.MAX_PAGES} pages; '
                    'aborting to avoid unbounded traversal'
                )
            payload = self._request_json('GET', next_url)
        return results

    def get(self, endpoint: str, pk) -> dict:
        return self._request_json('GET', self._detail_path(endpoint, pk))

    def post(self, endpoint: str, data: dict) -> dict:
        return self._request_json('POST', endpoint, json_body=data)

    def patch(self, endpoint: str, pk, data: dict) -> dict:
        return self._request_json('PATCH', self._detail_path(endpoint, pk), json_body=data)

    def match(self, endpoint: str, field: str, value) -> list[dict]:
        """Exact-match lookup: server-side filter first, search fallback second.

        Some InvenTree endpoints accept ``?field=value`` filters; others
        only support the generic fuzzy ``?search=`` parameter. The result
        is ALWAYS narrowed client-side to exact, case-sensitive equality
        on ``field``: InvenTree (DRF's DjangoFilterBackend) silently
        ignores filter parameters a endpoint does not register instead of
        rejecting them, so the server-side pass may hand back unrelated
        objects. Without the narrowing, one unrelated remote object
        would be adopted as a match, and two would fail as "ambiguous".
        """
        try:
            candidates = self.list(endpoint, params={field: value})
        except InvenTreeClientError as exc:
            if exc.status != 400:
                raise
            candidates = self.list(endpoint, params={'search': value})
        return [item for item in candidates if item.get(field) == value]

    # --- internals ------------------------------------------------------------

    @staticmethod
    def _normalize_type(wire_type) -> str:
        normalized = str(wire_type).strip().lower()
        return _TYPE_ALIASES.get(normalized, normalized)

    @staticmethod
    def _detail_path(endpoint: str, pk) -> str:
        if not endpoint.endswith('/'):
            endpoint = f'{endpoint}/'
        return f'{endpoint}{pk}/'

    def _build_url(self, path: str) -> str:
        if path.startswith(('http://', 'https://')):
            return path  # absolute (e.g. DRF `next` links)
        if not path.startswith('/'):
            path = f'/{path}'
        return f'{self.url}{path}'

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> requests.Response:
        url = self._build_url(path)
        try:
            response = self._session.request(
                method, url, params=params, json=json_body, timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise InvenTreeClientError(
                f'InvenTree API {method} {path} returned HTTP {response.status_code}',
                status=response.status_code,
                body=response.text,
            ) from exc
        except requests.RequestException as exc:
            raise InvenTreeClientError(
                f'InvenTree API {method} {path} request failed: '
                f'{type(exc).__name__}: {exc}'
            ) from exc
        return response

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ):
        response = self._request(method, path, params=params, json_body=json_body)
        try:
            return response.json()
        except ValueError as exc:
            raise InvenTreeClientError(
                f'InvenTree API {method} {path} returned invalid JSON',
                status=response.status_code,
                body=response.text,
            ) from exc
