"""ADR-0016: two roles, one allowlist file, operation ids as the unit."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from kokoro_tone import access
from support import _start_server, build_app

DEFAULT = {"Authorization": "Bearer default-secret"}
ADMIN = {"Authorization": "Bearer admin-secret"}


def _api_routes(routes):
    """Flatten the app's routes; newer FastAPI keeps an included router nested."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        else:
            router = getattr(route, "original_router", None)
            if router is not None:
                yield from _api_routes(router.routes)


def _has_require_auth(dependant) -> bool:
    for dependency in dependant.dependencies:
        if getattr(dependency.call, "__name__", "") == "require_auth":
            return True
        if _has_require_auth(dependency):
            return True
    return False


# ------------------------------------------------------------------ the policy


def test_a_missing_file_is_the_built_in_policy(tmp_path):
    policy = access.load_policy(str(tmp_path / "rbac.toml"))

    assert policy.source is None
    assert policy.allow == frozenset(access.DEFAULT_ALLOW)


def test_the_built_in_policy_grants_every_operation_that_exists_today():
    # No rbac.toml means no behaviour change: every slug a bearer can call
    # today stays callable. 0017/0018 narrow this to read/render/propose.
    assert set(access.DEFAULT_ALLOW) == access.OPERATIONS


def test_the_shipped_example_is_generated_from_the_defaults():
    path = Path(__file__).parents[1] / "lexicon" / "rbac.example.toml"

    assert path.read_text(encoding="utf-8") == access.template()


def test_the_example_names_only_known_operations():
    policy = access.load_policy(str(Path(__file__).parents[1] / "lexicon" / "rbac.example.toml"))

    assert policy.allow <= access.KNOWN_SLUGS


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ("version = 2\n\n[default]\nallow = []\n", "unsupported version 2"),
        ("[admin]\nallow = []\n", "unknown role table or key 'admin'"),
        ('version = 1\n\n[default]\nallow = ["notAnOperation"]\n', "notAnOperation"),
        (
            'version = 1\n\n[default]\nallow = ["updatePronunciation"]\n',
            "updatePronunciation",
        ),
        ('version = 1\n\n[default]\nallow = "listModels"\n', "[default].allow"),
        ("version = 1\n\n[default]\npermit = []\n", "unknown key 'permit'"),
        ("version = \n", "rbac.toml"),
    ],
)
def test_a_bad_policy_names_the_problem(tmp_path, body, needle):
    path = tmp_path / "rbac.toml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(access.PolicyError) as raised:
        access.load_policy(str(path))

    assert needle in str(raised.value)
    assert str(path) in str(raised.value)


def test_a_bad_policy_refuses_to_start(monkeypatch, tmp_path):
    path = tmp_path / "rbac.toml"
    path.write_text("version = 2\n\n[default]\nallow = []\n", encoding="utf-8")
    server = _start_server()
    try:
        with pytest.raises(access.PolicyError):
            build_app(
                monkeypatch,
                server,
                KOKORO_TONE_API_KEY="secret",
                KOKORO_TONE_RBAC=str(path),
            )
    finally:
        server.shutdown()


def test_a_policy_without_keys_is_inert_and_warns(tmp_path, capsys):
    path = tmp_path / "rbac.toml"
    path.write_text("version = 1\n\n[default]\nallow = []\n", encoding="utf-8")

    loaded = access.load_access(admin_keys=(), default_keys=(), rbac_path=str(path))

    assert loaded.enabled is False
    assert "inert" in capsys.readouterr().err


# -------------------------------------------------------------------- the roles


def test_a_token_in_both_lists_is_an_admin():
    loaded = access.Access(
        admin_keys=("same",),
        default_keys=("same",),
        policy=access.Policy(frozenset()),
    )

    assert loaded.match("same") == access.ROLE_ADMIN


def test_a_default_key_is_denied_a_slug_the_policy_does_not_name(restricted_gateway):
    client, _ = restricted_gateway

    response = client.get("/v1/audio/voices", headers=DEFAULT)

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["type"] == "permission_error"
    assert error["code"] == "insufficient_scope"
    assert response.headers["www-authenticate"] == 'Bearer error="insufficient_scope"'


def test_an_admin_key_may_call_a_slug_no_allowlist_names(restricted_gateway):
    client, _ = restricted_gateway

    response = client.get("/v1/audio/voices", headers=ADMIN)

    assert response.status_code == 200


def test_the_policy_grants_exactly_the_named_operation(restricted_gateway):
    client, server = restricted_gateway

    assert client.get("/v1/models", headers=DEFAULT).status_code == 200

    denied = client.post("/v1/audio/speech", headers=DEFAULT, json={"input": "hello"})
    assert denied.status_code == 403
    assert server.requests == []


def test_authentication_is_checked_before_authorisation(restricted_gateway):
    client, _ = restricted_gateway

    # The slug is denied, but no credential means 401, never 403.
    response = client.get("/v1/audio/voices")

    assert response.status_code == 401


def test_every_401_is_the_same_401(restricted_gateway):
    client, _ = restricted_gateway
    cases = [
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer nope"},
        {"Authorization": "Basic default-secret"},
        {"Authorization": "Bearer another-deployment"},
    ]

    responses = [client.get("/v1/models", headers=case) for case in cases]

    assert {response.status_code for response in responses} == {401}
    assert {response.headers["www-authenticate"] for response in responses} == {"Bearer"}
    bodies = {response.text for response in responses}
    assert len(bodies) == 1
    assert responses[0].json()["error"]["code"] == "invalid_api_key"


def test_a_public_route_needs_no_credential(restricted_gateway):
    client, _ = restricted_gateway

    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_no_keys_leaves_every_route_open(open_gateway):
    client, _ = open_gateway

    response = client.post("/v1/audio/speech", json={"input": "hello"})

    assert response.status_code == 200
    health = client.get("/health").json()
    assert health["access"]["enabled"] is False


def test_health_reports_that_a_file_policy_is_in_force(restricted_gateway):
    client, _ = restricted_gateway

    payload = client.get("/health").json()["access"]

    assert payload == {
        "enabled": True,
        "policy": "file",
        "admin_keys": True,
        "default_keys": True,
    }


# ------------------------------------------------------- the route/slug contract


def test_every_operation_is_a_slug_and_every_slug_has_auth(gateway):
    client, _ = gateway
    routes = list(_api_routes(client.app.routes))

    assert {route.operation_id for route in routes} == access.OPERATIONS
    for route in routes:
        assert route.operation_id
        if route.operation_id in access.PUBLIC_SLUGS:
            assert not _has_require_auth(route.dependant)
        else:
            assert _has_require_auth(route.dependant), route.path
