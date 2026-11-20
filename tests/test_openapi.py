"""ADR-0018: the generated OpenAPI document is the route contract."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from kokoro_tone import access
from support import AUTH

# The slugs and paths from ADR-0018's table. `{pron_id}` is FastAPI's name for
# the `{id}` in the prose; the URL is identical.
EXPECTED_OPERATIONS = {
    "getHealth",
    "listModels",
    "createSpeech",
    "listVoices",
    "listPronunciations",
    "createPronunciation",
    "updatePronunciation",
    "deletePronunciation",
    "retrievePronunciationAudio",
}

EXPECTED_PATHS = {
    "/health",
    "/v1/models",
    "/v1/audio/speech",
    "/v1/audio/voices",
    "/v1/pronunciations",
    "/v1/pronunciations/{pron_id}",
    "/v1/pronunciations/{pron_id}/audio",
}

# The statuses each operation answers with the OpenAI error envelope. A success
# status is not here: it carries the operation's own body.
EXPECTED_ERRORS = {
    "getHealth": {500},
    "listModels": {401, 403, 500},
    "listVoices": {401, 403, 500, 502},
    "createSpeech": {400, 401, 403, 413, 500, 502},
    "listPronunciations": {400, 401, 403, 500},
    "createPronunciation": {400, 401, 403, 413, 500},
    "updatePronunciation": {400, 401, 403, 404, 409, 500},
    "deletePronunciation": {400, 401, 403, 404, 409, 500},
    "retrievePronunciationAudio": {400, 401, 403, 404, 500, 502},
}

EXPECTED_SUCCESS = {
    "getHealth": {200, 503},
    "listModels": {200},
    "listVoices": {200},
    "createSpeech": {200},
    "listPronunciations": {200},
    "createPronunciation": {201, 202},
    "updatePronunciation": {200},
    "deletePronunciation": {200},
    "retrievePronunciationAudio": {200},
}


def _api_routes(routes):
    """Flatten the app's routes; newer FastAPI keeps an included router nested."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        else:
            router = getattr(route, "original_router", None)
            if router is not None:
                yield from _api_routes(router.routes)


def _documented(spec: dict) -> dict[tuple[str, str], dict]:
    found: dict[tuple[str, str], dict] = {}
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                found[(path, method.lower())] = operation
    return found


def _route_table(client) -> dict[tuple[str, str], dict]:
    table: dict[tuple[str, str], dict] = {}
    for route in _api_routes(client.app.routes):
        for method in route.methods:
            table[(route.path, method.lower())] = route
    return table


def _schema_ref(response: dict) -> str:
    schema = response.get("content", {}).get("application/json", {}).get("schema", {})
    return str(schema.get("$ref", ""))


def test_the_spec_is_the_route_table_in_both_directions(gateway):
    """Neither the app nor the document may name an operation the other omits."""
    client, _ = gateway
    spec = _documented(client.get("/openapi.json").json())
    routes = _route_table(client)

    assert set(spec) == set(routes)
    assert {operation["operationId"] for operation in spec.values()} == set(EXPECTED_OPERATIONS)
    assert {route.operation_id for route in routes.values()} == set(EXPECTED_OPERATIONS)


def test_no_v1_path_survives_outside_the_table(gateway):
    client, _ = gateway
    spec = client.get("/openapi.json").json()

    assert set(spec["paths"]) == EXPECTED_PATHS
    assert not {
        path for path in spec["paths"] if path.startswith("/v1") and path not in EXPECTED_PATHS
    }


def test_every_operation_names_its_role(gateway):
    client, _ = gateway
    spec = client.get("/openapi.json").json()

    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method not in {"get", "post", "patch", "delete"}:
                continue
            slug = operation["operationId"]
            roles = operation["x-kokoro-tone-roles"]
            if slug in access.PUBLIC_SLUGS:
                assert roles == ["public"], (path, method)
            elif slug in access.ADMIN_ONLY_SLUGS:
                assert roles == ["admin"], (path, method)
            else:
                assert roles == ["default", "admin"], (path, method)


def test_every_operation_documents_every_status_it_returns(gateway):
    client, _ = gateway
    spec = _documented(client.get("/openapi.json").json())

    for operation in spec.values():
        slug = operation["operationId"]
        statuses = {int(status) for status in operation["responses"]}
        assert statuses == EXPECTED_SUCCESS[slug] | EXPECTED_ERRORS[slug], slug
        # FastAPI's 422 is not what this gateway sends: a validation failure is
        # the 400 envelope from errors.py.
        assert 422 not in statuses, slug


@pytest.mark.parametrize("slug", sorted(EXPECTED_ERRORS))
def test_every_error_status_carries_the_openai_envelope(gateway, slug):
    client, _ = gateway
    spec = _documented(client.get("/openapi.json").json())
    operation = next(item for item in spec.values() if item["operationId"] == slug)

    for status in EXPECTED_ERRORS[slug]:
        assert _schema_ref(operation["responses"][str(status)]).endswith("/ErrorEnvelope")


def test_every_envelope_status_that_can_be_returned_is_documented(gateway):
    """errors.py's map is the source of the envelope; the spec must name each.

    422 is in the map only because FastAPI knows the status, but this gateway's
    validation handler rewrites it to a 400. 429 is not emitted: there is no
    rate limiter in the service. Every other envelope status is reachable and
    must appear on at least one operation.
    """
    from kokoro_tone.errors import _ERROR_TYPES

    client, _ = gateway
    spec = _documented(client.get("/openapi.json").json())
    documented = {int(status) for operation in spec.values() for status in operation["responses"]}

    assert set(_ERROR_TYPES) - {422, 429} <= documented


def test_declared_response_models_are_a_single_schema(gateway):
    """A `$ref` must not carry a second, conflicting `type` from a `dict` return."""
    client, _ = gateway
    spec = _documented(client.get("/openapi.json").json())

    for operation in spec.values():
        for response in operation["responses"].values():
            schema = response.get("content", {}).get("application/json", {}).get("schema", {})
            if "$ref" in schema:
                assert set(schema) == {"$ref"}, (operation["operationId"], schema)


def test_every_path_parameter_is_declared(gateway):
    client, _ = gateway
    spec = _documented(client.get("/openapi.json").json())

    for (path, _method), operation in spec.items():
        named = {
            parameter["name"]
            for parameter in operation.get("parameters", [])
            if parameter.get("in") == "path"
        }
        expected = {part.split("}")[0] for part in path.split("{")[1:]}
        assert named == expected, path


def test_the_dry_run_is_a_query_parameter_on_create_speech(gateway):
    client, _ = gateway
    spec = _documented(client.get("/openapi.json").json())
    operation = spec[("/v1/audio/speech", "post")]

    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    assert parameters["dry_run"]["in"] == "query"
    assert parameters["dry_run"]["required"] is False
    assert "requestBody" in operation
    assert "audio/mpeg" in operation["responses"]["200"]["content"]
    assert "application/json" in operation["responses"]["200"]["content"]


def test_the_retired_compile_route_is_gone(gateway):
    client, server = gateway

    response = client.post("/v1/script/compile", headers=AUTH, json={"input": "The bass was loud."})

    assert response.status_code == 404
    assert server.requests == []
