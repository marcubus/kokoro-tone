"""Declarative ffmpeg filter blocks, safe to expose through markup.

A block is a *constant* template plus typed, bounded numeric parameters. The
client supplies numbers, never filter syntax, and the gateway formats them. The
finished expression is checked against a filter-name allowlist and the same
character class `audio._filters` uses, so a registry entry cannot smuggle a new
sink into the chain.

The shipped blocks are the primitives the tone presets already use. Third-party
blocks arrive as declarative manifests through `plugins.py`; they are validated
here before they can be selected by `[filter:...]`.
"""

from __future__ import annotations

import math
import re
import string
from dataclasses import dataclass
from functools import lru_cache

from . import plugins
from .log import warning

# Filters a block template may name. Keep this in step with the Dockerfile's
# `--enable-filter` list; a name here that the binary lacks fails at runtime.
ALLOWED_FILTERS = frozenset(
    {
        "acompressor",
        "alimiter",
        "aresample",
        "asoftclip",
        "atempo",
        "bass",
        "highpass",
        "lowpass",
        "rubberband",
        "treble",
        "volume",
    }
)

# The same grammar `audio._filters` enforces. Duplicated on purpose: this module
# must be able to reject a plugin manifest without importing the audio layer.
_FILTER_SAFE = re.compile(r"^[A-Za-z0-9_=:.,+\- ]*$")
_FILTER_NAME = re.compile(r"(?:^|,)([a-z0-9_]+)=")


class FilterError(ValueError):
    """A filter block or a `[filter:]` value the gateway will not run."""


@dataclass(frozen=True)
class Param:
    name: str
    kind: str  # "float" or "int"
    minimum: float
    maximum: float
    default: float


@dataclass(frozen=True)
class Block:
    name: str
    template: str
    params: tuple[Param, ...]
    summary: str = ""

    def param(self, name: str) -> Param | None:
        return next((item for item in self.params if item.name == name), None)


@dataclass(frozen=True)
class FilterSpec:
    """One selected block and its resolved values, in declaration order."""

    block: str
    values: tuple[tuple[str, float], ...]

    def as_text(self) -> str:
        rendered = " ".join(f"{key}={value:g}" for key, value in self.values)
        return f"{self.block} {rendered}".strip()


def _param(raw: object, where: str) -> Param:
    if not isinstance(raw, dict):
        raise FilterError(f"{where}: each param must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise FilterError(f"{where}: param name must be a lowercase identifier")
    kind = raw.get("kind", "float")
    if kind not in ("float", "int"):
        raise FilterError(f"{where}.{name}: kind must be 'float' or 'int'")
    try:
        minimum = float(raw["min"])
        maximum = float(raw["max"])
        default = float(raw.get("default", minimum))
    except (KeyError, TypeError, ValueError) as exc:
        raise FilterError(f"{where}.{name}: min, max and default must be numbers") from exc
    if not all(math.isfinite(value) for value in (minimum, maximum, default)):
        raise FilterError(f"{where}.{name}: bounds must be finite")
    if minimum > maximum:
        raise FilterError(f"{where}.{name}: min is above max")
    if not minimum <= default <= maximum:
        raise FilterError(f"{where}.{name}: default is outside its bounds")
    return Param(name=name, kind=kind, minimum=minimum, maximum=maximum, default=default)


def parse_block(name: str, raw: object) -> Block:
    """Validate one manifest entry. Raises `FilterError` on anything unexpected."""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise FilterError("block name must be a lowercase identifier")
    if not isinstance(raw, dict):
        raise FilterError(f"{name}: block must be an object")
    unknown = set(raw) - {"template", "params", "summary"}
    if unknown:
        raise FilterError(f"{name}: unknown keys {sorted(unknown)}")
    template = raw.get("template")
    if not isinstance(template, str) or not template.strip():
        raise FilterError(f"{name}: template must be a non-empty string")
    raw_params = raw.get("params", [])
    if not isinstance(raw_params, list):
        raise FilterError(f"{name}: params must be a list")
    params = tuple(_param(item, f"{name}.params") for item in raw_params)
    if len({item.name for item in params}) != len(params):
        raise FilterError(f"{name}: duplicate param name")

    block = Block(
        name=name,
        template=template.strip(),
        params=params,
        summary=str(raw.get("summary", "")),
    )
    _check_template(block)
    return block


def _check_template(block: Block) -> None:
    """Every placeholder must be a declared param, and every filter allowed."""
    known = {item.name for item in block.params}
    formatter = string.Formatter()
    try:
        fields = {field for _, field, _, _ in formatter.parse(block.template) if field is not None}
    except ValueError as exc:
        raise FilterError(f"{block.name}: malformed template: {exc}") from exc
    unknown = fields - known
    if unknown:
        raise FilterError(f"{block.name}: template names undeclared params {sorted(unknown)}")

    # Render with defaults so the filter names can be checked before any client
    # value exists. The result must satisfy the same grammar as the audio chain.
    probe = _build(block, {}, _check=False)
    names = set(_FILTER_NAME.findall(probe))
    if not names:
        raise FilterError(f"{block.name}: template names no filter")
    disallowed = names - ALLOWED_FILTERS
    if disallowed:
        raise FilterError(f"{block.name}: filter(s) not allowed {sorted(disallowed)}")
    if not _FILTER_SAFE.fullmatch(probe):
        raise FilterError(f"{block.name}: template is outside the filter grammar")


# The primitives the tone presets are built from. They are deliberately a small
# set: the point is to compose known-safe blocks, not to expose all of ffmpeg.
_BASE: dict[str, Block] = {
    "low_shelf": Block(
        name="low_shelf",
        template="bass=g={gain}:f={freq}:w={width}",
        params=(
            Param("gain", "float", -20.0, 20.0, 0.0),
            Param("freq", "int", 40.0, 2000.0, 250.0),
            Param("width", "float", 0.1, 4.0, 0.7),
        ),
        summary="Low shelf; positive gain adds body.",
    ),
    "high_shelf": Block(
        name="high_shelf",
        template="treble=g={gain}:f={freq}:w={width}",
        params=(
            Param("gain", "float", -20.0, 20.0, 0.0),
            Param("freq", "int", 1000.0, 16000.0, 6000.0),
            Param("width", "float", 0.1, 4.0, 0.7),
        ),
        summary="High shelf; positive gain adds brightness.",
    ),
    "lowpass": Block(
        name="lowpass",
        template="lowpass=f={freq}",
        params=(Param("freq", "int", 200.0, 20000.0, 8000.0),),
        summary="Remove everything above a corner frequency.",
    ),
    "highpass": Block(
        name="highpass",
        template="highpass=f={freq}",
        params=(Param("freq", "int", 20.0, 8000.0, 300.0),),
        summary="Remove everything below a corner frequency.",
    ),
    "compressor": Block(
        name="compressor",
        template="acompressor=ratio={ratio}:attack={attack}:release={release}:threshold={threshold}",
        params=(
            Param("ratio", "float", 1.0, 20.0, 2.5),
            Param("attack", "int", 1.0, 2000.0, 15.0),
            Param("release", "int", 10.0, 9000.0, 200.0),
            Param("threshold", "float", 0.001, 1.0, 0.125),
        ),
        summary="Downward compression; a lower threshold compresses more.",
    ),
    "saturate": Block(
        name="saturate",
        template="asoftclip=type=tanh:threshold={threshold}",
        params=(Param("threshold", "float", 0.1, 1.0, 0.7),),
        summary="Soft tanh saturation; a lower threshold drives harder.",
    ),
    "gain": Block(
        name="gain",
        template="volume={db}dB",
        params=(Param("db", "float", -30.0, 30.0, 0.0),),
        summary="A flat gain stage, in dB.",
    ),
}


@lru_cache(maxsize=1)
def registry() -> dict[str, Block]:
    """The shipped blocks plus any validated plugin manifests.

    Cached for the process. A new or edited manifest needs `reload()` (the
    gateway restarts; the CLI is a fresh process each run).
    """
    merged = dict(_BASE)
    for origin, payload in plugins.manifests(plugins.FILTER_GROUP):
        if not isinstance(payload, dict):
            warning(f"filter plugin {origin!r}: manifest must be an object; ignored")
            continue
        for name, raw in payload.items():
            try:
                merged[str(name)] = parse_block(str(name), raw)
            except FilterError as exc:
                warning(f"filter plugin {origin!r}: {exc}; ignored")
    return merged


def reload() -> None:
    """Drop the cached registry, so a new manifest is picked up (tests, reload)."""
    registry.cache_clear()


def catalogue() -> list[dict[str, object]]:
    """A JSON-ready description of every available block, for discovery."""
    return [
        {
            "name": block.name,
            "summary": block.summary,
            "template": block.template,
            "params": [
                {
                    "name": param.name,
                    "kind": param.kind,
                    "min": param.minimum,
                    "max": param.maximum,
                    "default": param.default,
                }
                for param in block.params
            ],
        }
        for block in sorted(registry().values(), key=lambda item: item.name)
    ]


def _value(param: Param, raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise FilterError(f"{param.name} must be a number") from exc
    if not math.isfinite(value):
        raise FilterError(f"{param.name} must be finite")
    if param.kind == "int" and not value.is_integer():
        raise FilterError(f"{param.name} must be a whole number")
    if not param.minimum <= value <= param.maximum:
        raise FilterError(f"{param.name} must be between {param.minimum:g} and {param.maximum:g}")
    return value


def _build(block: Block, values: dict[str, float], *, _check: bool = True) -> str:
    """Format one block. Values are numbers; the template is the only syntax."""
    rendered: dict[str, str] = {}
    for param in block.params:
        value = values.get(param.name, param.default)
        if param.kind == "int":
            rendered[param.name] = str(int(round(value)))
        else:
            rendered[param.name] = f"{value:.4f}".rstrip("0").rstrip(".")
    text = block.template.format(**rendered)
    if _check and not _FILTER_SAFE.fullmatch(text):
        raise FilterError("filter expression is outside the known grammar")
    return text


def parse_spec(value: str) -> FilterSpec | None:
    """Parse `block key=value ...` from a `[filter:]` tag.

    `none` clears the slot. An unknown block or key is an error rather than
    something silently ignored, because the caller asked for a specific sound.
    """
    tokens = value.split()
    if not tokens:
        raise FilterError("filter needs a block name; use [filter:none] to clear")
    name = tokens[0].lower()
    if name == "none":
        if len(tokens) > 1:
            raise FilterError("filter none takes no parameters")
        return None

    blocks = registry()
    block = blocks.get(name)
    if block is None:
        known = ", ".join(sorted(blocks))
        raise FilterError(f"unknown filter block {name!r}; known: {known}")

    supplied: dict[str, float] = {}
    for token in tokens[1:]:
        key, separator, raw = token.partition("=")
        if not separator or not key:
            raise FilterError(f"filter parameter {token!r} must be key=value")
        param = block.param(key)
        if param is None:
            known = ", ".join(item.name for item in block.params)
            raise FilterError(f"unknown parameter {key!r} for {name}; known: {known}")
        supplied[key] = _value(param, raw)

    # Freeze the resolved values in declaration order, so the spec is stable
    # even if the registry later gains a parameter.
    values = tuple((param.name, supplied.get(param.name, param.default)) for param in block.params)
    return FilterSpec(block=name, values=values)


def render(spec: FilterSpec) -> str:
    """Turn a parsed spec back into one checked filter fragment."""
    block = registry().get(spec.block)
    if block is None:
        raise FilterError(f"unknown filter block {spec.block!r}")
    return _build(block, dict(spec.values))
