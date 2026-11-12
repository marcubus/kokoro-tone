"""The declarative filter registry and the `[filter:]` tag."""

from __future__ import annotations

import json

import pytest

from kokoro_tone import filters, parse, plugins, prosody
from kokoro_tone.parse import Turn


def test_a_block_renders_from_typed_values():
    spec = filters.parse_spec("high_shelf gain=5 freq=6000 width=0.7")
    assert spec is not None
    assert spec.block == "high_shelf"
    assert filters.render(spec) == "treble=g=5:f=6000:w=0.7"


def test_defaults_fill_omitted_parameters():
    spec = filters.parse_spec("lowpass")
    assert spec is not None
    assert filters.render(spec) == "lowpass=f=8000"


def test_none_clears_the_slot():
    assert filters.parse_spec("none") is None


@pytest.mark.parametrize(
    "value",
    [
        "no_such_block",
        "high_shelf gain=5 nope=1",
        "high_shelf gain=999",
        "high_shelf gain=loud",
        "gain db=1); rm -rf /",
        "none gain=1",
    ],
)
def test_bad_values_are_rejected(value):
    with pytest.raises(filters.FilterError):
        filters.parse_spec(value)


def test_a_template_cannot_name_an_unapproved_filter():
    with pytest.raises(filters.FilterError, match="not allowed"):
        filters.parse_block(
            "evil",
            {"template": "aeval=val(0)", "params": []},
        )


def test_a_template_cannot_name_an_undeclared_parameter():
    with pytest.raises(filters.FilterError, match="undeclared"):
        filters.parse_block(
            "evil",
            {"template": "volume={db}dB", "params": []},
        )


def test_the_filter_tag_is_sticky_and_can_be_cleared():
    turns = parse.parse_markup(
        "[filter:high_shelf gain=3] One. [voice:am_michael] Two. [filter:none] Three."
    )

    assert turns[0].filter is not None
    assert turns[1].filter == turns[0].filter
    assert turns[2].filter is None


def test_the_filter_tag_reaches_the_prosody_chain():
    spec = filters.parse_spec("high_shelf gain=5")
    assert spec is not None
    resolved = prosody.resolve(Turn(text="x", filter=spec))

    assert "treble=g=5:f=6000:w=0.7" in resolved.filters


def test_an_unknown_filter_tag_is_a_markup_error():
    with pytest.raises(parse.MarkupError, match="unknown filter block"):
        parse.parse_markup("[filter:nope] Hi.")


def test_a_turn_array_accepts_a_filter():
    turns = parse.parse_input([{"text": "Hi.", "filter": "lowpass freq=5000"}])
    assert turns[0].filter is not None
    assert filters.render(turns[0].filter) == "lowpass=f=5000"


def test_a_plugin_manifest_adds_a_validated_block(monkeypatch):
    manifest = {
        "air": {
            "template": "highpass=f={freq}",
            "params": [{"name": "freq", "kind": "int", "min": 20, "max": 2000, "default": 200}],
            "summary": "Test block.",
        }
    }
    monkeypatch.setattr(plugins, "manifests", lambda group: [("test:air", manifest)])
    filters.reload()
    try:
        spec = filters.parse_spec("air freq=150")
        assert spec is not None
        assert filters.render(spec) == "highpass=f=150"
    finally:
        filters.reload()


def test_a_bad_plugin_manifest_is_skipped(monkeypatch):
    manifest = {"evil": {"template": "aeval=val(0)", "params": []}}
    monkeypatch.setattr(plugins, "manifests", lambda group: [("test:evil", manifest)])
    filters.reload()
    try:
        assert "evil" not in filters.registry()
    finally:
        filters.reload()


def test_no_manifest_env_means_no_file_plugins(monkeypatch):
    monkeypatch.delenv(plugins.MANIFEST_ENV, raising=False)
    assert plugins.file_manifests(plugins.FILTER_GROUP) == []


def test_a_json_manifest_file_adds_a_block(monkeypatch, tmp_path):
    path = tmp_path / "filters.json"
    path.write_text(
        json.dumps(
            {
                "telephone": {
                    "summary": "Narrow band.",
                    "template": "highpass=f={low}:lowpass=f={high}",
                    "params": [
                        {"name": "low", "kind": "int", "min": 200, "max": 2000, "default": 500},
                        {"name": "high", "kind": "int", "min": 2000, "max": 8000, "default": 3000},
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(plugins.MANIFEST_ENV, str(tmp_path))
    filters.reload()
    try:
        spec = filters.parse_spec("telephone low=400 high=3200")
        assert spec is not None
        assert filters.render(spec) == "highpass=f=400:lowpass=f=3200"
        assert any(block["name"] == "telephone" for block in filters.catalogue())
    finally:
        filters.reload()


def test_a_local_file_overrides_an_entry_point(monkeypatch, tmp_path):
    installed = {
        "air": {
            "template": "highpass=f={freq}",
            "params": [{"name": "freq", "kind": "int", "min": 20, "max": 2000, "default": 200}],
        }
    }
    local = {
        "air": {
            "template": "lowpass=f={freq}",
            "params": [{"name": "freq", "kind": "int", "min": 200, "max": 8000, "default": 1000}],
        }
    }
    path = tmp_path / "air.json"
    path.write_text(json.dumps(local), encoding="utf-8")
    monkeypatch.setenv(plugins.MANIFEST_ENV, str(path))
    monkeypatch.setattr(plugins, "entry_point_manifests", lambda group: [("test:air", installed)])
    filters.reload()
    try:
        spec = filters.parse_spec("air")
        assert spec is not None
        assert filters.render(spec) == "lowpass=f=1000"
    finally:
        filters.reload()
