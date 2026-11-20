"""ADR-0017: a proposal becomes policy only after an admin decides."""

from __future__ import annotations

import json

import pytest

from kokoro_tone import proposals
from kokoro_tone.limits import DEFAULT_LIMITS
from kokoro_tone.settings import SettingsError, load_settings
from support import ADMIN, AUTH, _start_server, build_app


def _create(client, **body):
    return client.post("/v1/pronunciations", headers=AUTH, json=body)


def _pending(client):
    return client.get("/v1/pronunciations", headers=AUTH, params={"status": "pending"}).json()[
        "data"
    ]


# ------------------------------------------------------------------ the UUIDv7


def test_uuid7_orders_by_creation():
    first = proposals.uuid7()
    second = proposals.uuid7()

    assert first < second
    assert first[14] == "7"


def test_uuid7_is_a_uuid():
    import uuid

    parsed = uuid.UUID(proposals.uuid7())

    assert parsed.variant == uuid.RFC_4122
    assert parsed.version == 7


def test_an_uppercase_run_is_two_letters():
    assert proposals.has_uppercase_run("MEL-bun")
    assert proposals.has_uppercase_run("NISSUH")
    assert proposals.has_uppercase_run("al-buh-NEEZ")
    assert not proposals.has_uppercase_run("ban-yool")
    assert not proposals.has_uppercase_run("Melbourne")


# -------------------------------------------------------------- the store file


def test_the_store_is_a_versioned_file_beside_the_lexicon(tmp_path):
    store = proposals.ProposalStore(str(tmp_path / "proposals.json"), DEFAULT_LIMITS)
    store.propose(word="Rishworth", say="rish-wuth", sense=None, context=None, role="default")

    raw = json.loads((tmp_path / "proposals.json").read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["proposals"][0]["status"] == proposals.PENDING
    assert raw["proposals"][0]["created_by_role"] == "default"


def test_a_corrupt_store_is_loud_not_silent(tmp_path):
    path = tmp_path / "proposals.json"
    path.write_text("not json", encoding="utf-8")
    store = proposals.ProposalStore(str(path), DEFAULT_LIMITS)

    with pytest.raises(proposals.StoreError, match="not readable"):
        store.list()


def test_the_store_refuses_a_symlink(tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    path = tmp_path / "proposals.json"
    path.symlink_to(target)
    store = proposals.ProposalStore(str(path), DEFAULT_LIMITS)

    with pytest.raises(proposals.StoreError, match="symlink"):
        store.list()
    assert target.read_text(encoding="utf-8") == "{}"


def test_the_queue_is_oldest_first_and_pages(tmp_path):
    store = proposals.ProposalStore(str(tmp_path / "proposals.json"), DEFAULT_LIMITS)
    for word in ("A", "B", "C"):
        store.propose(word=word, say=word.lower(), sense=None, context=None, role="default")

    rows = store.list(status=proposals.PENDING, limit=2)
    assert [row.word for row in rows] == ["A", "B"]

    rest = store.list(status=proposals.PENDING, limit=2, after=rows[-1].id)
    assert [row.word for row in rest] == ["C"]


def test_a_decided_row_keeps_its_reason(tmp_path):
    store = proposals.ProposalStore(str(tmp_path / "proposals.json"), DEFAULT_LIMITS)
    row = store.propose(word="Nyssa", say="niss-uh", sense=None, context=None, role="default")
    store.decide(row.id, status=proposals.REJECTED, role="admin", reason="uppercase guess")

    kept = store.get(row.id)
    assert kept is not None
    assert kept.status == proposals.REJECTED
    assert kept.reason == "uppercase guess"
    assert kept.decided_by_role == "admin"


# ------------------------------------------------------------- review mode API


def test_review_mode_records_a_proposal_and_leaves_the_lexicon_alone(review_gateway):
    client, _, learned = review_gateway
    response = _create(client, word="Isi", say="izzy")

    assert response.status_code == 202
    body = response.json()
    assert body["object"] == "proposal"
    assert body["status"] == "pending"
    assert body["created_by_role"] == "default"
    assert body["id"][14] == "7"
    assert not learned.exists()


def test_review_mode_refuses_an_uppercase_say(review_gateway):
    client, _, _ = review_gateway
    response = _create(client, word="Melbourne", say="MEL-bun")

    assert response.status_code == 400
    assert "uppercase" in response.json()["error"]["message"]
    assert _pending(client) == []


def test_the_queue_lists_pending_proposals_oldest_first(review_gateway):
    client, _, _ = review_gateway
    for word in ("Nyssa", "Rishworth"):
        _create(client, word=word, say=word.lower())

    rows = _pending(client)

    assert [row["word"] for row in rows] == ["Nyssa", "Rishworth"]
    assert all(row["object"] == "proposal" for row in rows)


def test_an_unknown_status_is_rejected(review_gateway):
    client, _, _ = review_gateway
    response = client.get("/v1/pronunciations", headers=AUTH, params={"status": "maybe"})

    assert response.status_code == 400


def test_an_admin_approval_makes_the_next_render_use_the_reading(review_gateway):
    client, server, learned = review_gateway
    created = _create(client, word="Isi", say="izzy").json()

    response = client.patch(
        f"/v1/pronunciations/{created['id']}",
        headers=ADMIN,
        json={"status": "approved"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["applied"] is True
    assert body["decided_by_role"] == "admin"
    assert {"match": "Isi", "say": "izzy"} in json.loads(learned.read_text(encoding="utf-8"))[
        "defaults"
    ]["rules"]

    render = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "Ask Isi.", "normalise": {"lexicon": True}},
    )
    assert render.status_code == 200
    assert server.last_request["input"] == "Ask Izzy."


def test_a_rejection_leaves_the_render_alone_and_keeps_the_row(review_gateway):
    client, server, learned = review_gateway
    created = _create(client, word="Isi", say="izzy").json()

    response = client.patch(
        f"/v1/pronunciations/{created['id']}",
        headers=ADMIN,
        json={"status": "rejected", "reason": "not a word"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert response.json()["reason"] == "not a word"
    assert not learned.exists()

    client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "Ask Isi.", "normalise": {"lexicon": True}},
    )
    assert server.last_request["input"] == "Ask Isi."

    rows = client.get("/v1/pronunciations", headers=AUTH, params={"status": "rejected"}).json()[
        "data"
    ]
    assert [row["id"] for row in rows] == [created["id"]]


def test_a_second_decision_is_a_state_conflict(review_gateway):
    client, _, _ = review_gateway
    created = _create(client, word="Isi", say="izzy").json()
    client.patch(f"/v1/pronunciations/{created['id']}", headers=ADMIN, json={"status": "approved"})

    response = client.patch(
        f"/v1/pronunciations/{created['id']}", headers=ADMIN, json={"status": "rejected"}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_transition"


def test_a_default_key_may_not_decide(review_gateway):
    client, _, _ = review_gateway
    created = _create(client, word="Isi", say="izzy").json()

    response = client.patch(
        f"/v1/pronunciations/{created['id']}", headers=AUTH, json={"status": "approved"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"


def test_an_unknown_proposal_is_404(review_gateway):
    client, _, _ = review_gateway
    response = client.patch(
        "/v1/pronunciations/00000000-0000-7000-8000-000000000000",
        headers=ADMIN,
        json={"status": "approved"},
    )

    assert response.status_code == 404


def test_deciding_a_listed_rule_is_immutable(review_gateway):
    client, _, _ = review_gateway
    listed = client.get("/v1/pronunciations", headers=AUTH, params={"word": "Banyule"}).json()
    rule_id = listed["data"][0]["id"]

    response = client.patch(
        f"/v1/pronunciations/{rule_id}", headers=ADMIN, json={"status": "approved"}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "immutable_rule"


def test_deleting_a_proposal_removes_the_row(review_gateway):
    client, _, _ = review_gateway
    created = _create(client, word="Isi", say="izzy").json()

    response = client.delete(f"/v1/pronunciations/{created['id']}", headers=ADMIN)

    assert response.status_code == 200
    assert response.json()["object"] == "proposal"
    assert _pending(client) == []


def test_the_audition_renders_the_same_sentence_twice(review_gateway):
    client, server, _ = review_gateway
    created = _create(
        client,
        word="Banyule",
        say="ban-yool",
        context="We drove through Banyule on Sunday.",
    ).json()

    as_is = client.get(
        f"/v1/pronunciations/{created['id']}/audio",
        headers=ADMIN,
        params={"variant": "as-is"},
    )
    proposed = client.get(
        f"/v1/pronunciations/{created['id']}/audio",
        headers=ADMIN,
        params={"variant": "proposed"},
    )

    assert as_is.status_code == 200
    assert proposed.status_code == 200
    assert as_is.content.startswith(b"ID3")
    assert server.requests[-2]["input"] == "We drove through Banyule on Sunday."
    assert server.requests[-1]["input"] == "We drove through Ban-yool on Sunday."


def test_the_audition_rejects_an_unknown_variant(review_gateway):
    client, _, _ = review_gateway
    created = _create(client, word="Isi", say="izzy").json()

    response = client.get(
        f"/v1/pronunciations/{created['id']}/audio",
        headers=ADMIN,
        params={"variant": "guess"},
    )

    assert response.status_code == 400


def test_the_audition_is_admin_only(review_gateway):
    client, _, _ = review_gateway
    created = _create(client, word="Isi", say="izzy").json()

    response = client.get(f"/v1/pronunciations/{created['id']}/audio", headers=AUTH)

    assert response.status_code == 403


# -------------------------------------------------------------- force mode API


def test_force_mode_has_nothing_pending(learned_gateway):
    client, _, _ = learned_gateway
    created = _create(client, word="Isi", say="izzy").json()

    response = client.patch(
        f"/v1/pronunciations/{created['id']}", headers=ADMIN, json={"status": "approved"}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_pending"


def test_force_mode_warns_but_applies_an_uppercase_say(learned_gateway, capsys):
    client, _, learned = learned_gateway
    response = _create(client, word="Melbourne", say="MEL-bun")

    assert response.status_code == 201
    assert "uppercase stress markers" in capsys.readouterr().err
    assert {"match": "Melbourne", "say": "MEL-bun"} in json.loads(
        learned.read_text(encoding="utf-8")
    )["defaults"]["rules"]


def test_health_reports_the_mode_and_the_queue(review_gateway):
    client, _, _ = review_gateway
    _create(client, word="Isi", say="izzy")

    normalise = client.get("/health").json()["normalise"]

    assert normalise["approval"] == "review"
    assert normalise["pending_proposals"] == 1


# --------------------------------------------------------------------- startup


def test_review_mode_refuses_to_start_without_an_admin_key(monkeypatch):
    server = _start_server()
    try:
        with pytest.raises(SettingsError, match="requires KOKORO_TONE_ADMIN_KEY"):
            build_app(
                monkeypatch,
                server,
                KOKORO_TONE_API_KEY="secret",
                KOKORO_TONE_ADMIN_KEY=None,
                KOKORO_TONE_ADMIN_KEYS=None,
                PRONUNCIATION_APPROVAL="review",
            )
    finally:
        server.shutdown()


def test_an_unknown_approval_mode_stops_the_process(monkeypatch):
    monkeypatch.setenv("PRONUNCIATION_APPROVAL", "maybe")

    with pytest.raises(SettingsError, match="PRONUNCIATION_APPROVAL"):
        load_settings()
