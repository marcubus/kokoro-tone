"""The pipeline seam: shaping and rendering without an HTTP request."""

from __future__ import annotations

import pytest

from kokoro_tone import pipeline
from kokoro_tone.errors import RequestError
from kokoro_tone.request import SpeechRequest


def test_prepare_marks_a_plain_string_as_passthrough(gateway):
    client, _ = gateway

    prepared = pipeline.prepare(SpeechRequest(input="Hello there."), client.app.state.ctx)

    assert prepared.passthrough is True
    assert prepared.voice == "af_heart"
    assert [turn.text for turn in prepared.turns] == ["Hello there."]


def test_prepare_turns_markup_into_turns_with_sticky_tags(gateway):
    client, _ = gateway

    prepared = pipeline.prepare(
        SpeechRequest(input="[voice:am_michael] One. [emotion:sad] Two."),
        client.app.state.ctx,
    )

    assert prepared.passthrough is False
    assert [turn.voice for turn in prepared.turns] == ["am_michael", "am_michael"]
    assert prepared.turns[1].emotion == "sad"


def test_prepare_rejects_an_unknown_model_without_http(gateway):
    client, _ = gateway

    with pytest.raises(RequestError, match="unknown model"):
        pipeline.prepare(SpeechRequest(model="nope", input="Hello."), client.app.state.ctx)


def test_the_app_builds_one_context(gateway):
    client, _ = gateway

    ctx = client.app.state.ctx
    assert ctx.settings is not None
    assert ctx.lexicon is not None
    assert ctx.upstream is not None
