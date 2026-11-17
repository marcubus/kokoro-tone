"""The deployment lexicon: rules, per-voice sets, aliases and reload.

A request can carry its own `pronunciations`, but a deployment also wants a
file it can edit without touching the code. This module owns that file: the
JSON schema, validation, the overlay composition, and the mtime-based reload.

Everything here is data, never code: rules are literal strings, the schema is
a closed allowlist, and a bad entry is skipped rather than raised, so one typo
cannot stop the gateway from starting.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import normalise
from .limits import Limits
from .log import event, warning
from .parse import MarkupError, sanitise_text, validate_voice
from .telemetry import Telemetry

_SET_KEYS = frozenset({"rules", "acronyms", "protect", "letters", "locale"})
_RULE_KEYS = frozenset({"match", "say", "when", "case_sensitive", "whole_word"})
_WHEN_KEYS = frozenset({"before", "after", "within"})


@dataclass(frozen=True)
class Rule:
    match: str
    say: str
    before: frozenset[str] = frozenset()
    after: frozenset[str] = frozenset()
    # `None` means the sentence containing the match: the natural unit, and one
    # the author does not have to count. An int narrows it deliberately.
    within: int | None = None
    case_sensitive: bool = False
    whole_word: bool = True


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[Rule, ...] = ()
    acronyms: dict[str, str] = field(default_factory=dict)
    protect: tuple[str, ...] = ()
    letters: dict[str, str] = field(default_factory=dict)
    locale: str | None = None


@dataclass(frozen=True)
class LexiconData:
    defaults: RuleSet = field(default_factory=RuleSet)
    voices: dict[str, RuleSet] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    default_alias: str | None = None
    problems: tuple[str, ...] = ()


EMPTY_LEXICON = LexiconData()


# ---------------------------------------------------------------- validation


def _clean(
    value: object,
    field_name: str,
    limits: Limits,
    *,
    allow_empty: bool = False,
    strip: bool = True,
) -> str:
    if not isinstance(value, str):
        raise MarkupError(f"{field_name} must be a string")
    text = value.strip() if strip else value
    if not text.strip() and not allow_empty:
        raise MarkupError(f"{field_name} must not be empty")
    if "[" in text or "]" in text:
        raise MarkupError(f"{field_name} must not contain brackets")
    if sanitise_text(text) != text:
        raise MarkupError(f"{field_name} must not contain control characters")
    if len(text) > limits.pronunciation_chars:
        raise MarkupError(f"{field_name} exceeds {limits.pronunciation_chars} characters")
    return text


def _acronym_key(value: object, limits: Limits) -> str:
    if not isinstance(value, str):
        raise MarkupError("acronym must be a string")
    key = value.strip()
    if not (2 <= len(key) <= 6) or not all("A" <= letter <= "Z" for letter in key):
        raise MarkupError(f"acronym {key!r} must be 2-6 uppercase letters")
    if len(key) > limits.pronunciation_chars:
        raise MarkupError(f"acronym exceeds {limits.pronunciation_chars} characters")
    return key


def _word_list(value: object, field_name: str, limits: Limits) -> frozenset[str]:
    if value is None:
        return frozenset()
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list):
        raise MarkupError(f"when.{field_name} must be a list of words")
    if len(items) > limits.context_words:
        raise MarkupError(f"when.{field_name} exceeds {limits.context_words} words")
    words = set()
    for item in items:
        words.add(_clean(item, f"when.{field_name}", limits).casefold())
    return frozenset(words)


def _parse_rule(item: object, limits: Limits) -> Rule:
    if not isinstance(item, dict):
        raise MarkupError("each pronunciation rule must be an object")
    unknown = set(item) - _RULE_KEYS
    if unknown:
        raise MarkupError(f"unknown rule keys {sorted(unknown)}")
    # `match` keeps its own spacing, so a rule can delete "um, " including the
    # space that follows it. `say` is stripped: a whitespace-only replacement
    # is a deletion, written as "".
    match = _clean(item.get("match"), "match", limits, strip=False)
    say = _clean(item.get("say"), "say", limits, allow_empty=True)

    when = item.get("when") or {}
    if not isinstance(when, dict):
        raise MarkupError("when must be an object")
    unknown = set(when) - _WHEN_KEYS
    if unknown:
        raise MarkupError(f"unknown when keys {sorted(unknown)}")
    within = when.get("within")
    if within is not None and (
        isinstance(within, bool)
        or not isinstance(within, int)
        or not 1 <= within <= limits.context_words
    ):
        raise MarkupError(f"when.within must be between 1 and {limits.context_words}")

    case_sensitive = item.get("case_sensitive", False)
    whole_word = item.get("whole_word", True)
    if not isinstance(case_sensitive, bool) or not isinstance(whole_word, bool):
        raise MarkupError("case_sensitive and whole_word must be booleans")

    return Rule(
        match=match,
        say=say,
        before=_word_list(when.get("before"), "before", limits),
        after=_word_list(when.get("after"), "after", limits),
        within=within,
        case_sensitive=case_sensitive,
        whole_word=whole_word,
    )


def _rule_items(value: object) -> list[object]:
    """Normalise the `{match: say}` shorthand and the list form to one list."""
    if isinstance(value, dict):
        return [{"match": key, "say": say} for key, say in value.items()]
    if isinstance(value, list):
        return value
    raise MarkupError("pronunciations must be an object of word: replacement, or a list of rules")


def parse_rules(value: object, limits: Limits) -> tuple[Rule, ...]:
    """Accept the `{match: say}` shorthand or a list of full rule objects."""
    if value is None:
        return ()
    items = _rule_items(value)
    rules = tuple(_parse_rule(item, limits) for item in items)
    if len(rules) > limits.pronunciations:
        raise MarkupError(f"pronunciations exceeds {limits.pronunciations} rules")
    return rules


def parse_request_sets(
    value: object, limits: Limits
) -> tuple[tuple[Rule, ...], dict[str, tuple[Rule, ...]]]:
    """Split `pronunciations` into (global rules, rules per alias)."""
    if value is None:
        return (), {}
    if isinstance(value, list):
        return parse_rules(value, limits), {}
    if not isinstance(value, dict):
        raise MarkupError("pronunciations must be an object or a list of rules")

    if "*" in value or not all(isinstance(item, str) for item in value.values()):
        per_alias: dict[str, tuple[Rule, ...]] = {}
        for key, item in value.items():
            if key == "*":
                continue
            validate_voice(key, limits.voice_chars)
            if not isinstance(item, list):
                raise MarkupError("per-alias pronunciations must be a list of rules")
            per_alias[key] = parse_rules(item, limits)
        global_rules = parse_rules(value.get("*"), limits) if "*" in value else ()
        total = len(global_rules) + sum(len(rules) for rules in per_alias.values())
        if total > limits.pronunciations:
            raise MarkupError(f"pronunciations exceeds {limits.pronunciations} rules")
        if len(per_alias) > limits.voice_sets:
            raise MarkupError(f"pronunciations exceeds {limits.voice_sets} voice sets")
        return global_rules, per_alias

    return parse_rules(value, limits), {}


# ---------------------------------------------------------------- lexicon


def parse_set(value: object, limits: Limits, where: str) -> tuple[RuleSet, list[str]]:
    problems: list[str] = []
    if value is None:
        return RuleSet(), problems
    if not isinstance(value, dict):
        return RuleSet(), [f"{where}: must be an object"]

    unknown = set(value) - _SET_KEYS
    if unknown:
        problems.append(f"{where}: ignoring unknown keys {sorted(unknown)}")

    rules: tuple[Rule, ...] = ()
    if "rules" in value:
        try:
            items = _rule_items(value["rules"])
        except MarkupError as exc:
            problems.append(f"{where}.rules: {exc}")
        else:
            kept: list[Rule] = []
            for item in items:
                try:
                    kept.append(_parse_rule(item, limits))
                except MarkupError as exc:
                    problems.append(f"{where}.rules: {exc}")
            rules = tuple(kept)

    acronyms: dict[str, str] = {}
    if "acronyms" in value:
        raw = value["acronyms"]
        if isinstance(raw, dict):
            for key, say in raw.items():
                try:
                    acronyms[_acronym_key(key, limits)] = _clean(say, "acronym value", limits)
                except MarkupError as exc:
                    problems.append(f"{where}.acronyms: {exc}")
        else:
            problems.append(f"{where}.acronyms: must be an object")

    protect: tuple[str, ...] = ()
    if "protect" in value:
        raw = value["protect"]
        if isinstance(raw, list):
            protected: list[str] = []
            for item in raw:
                try:
                    protected.append(_acronym_key(item, limits))
                except MarkupError as exc:
                    problems.append(f"{where}.protect: {exc}")
            protect = tuple(protected)
        else:
            problems.append(f"{where}.protect: must be a list")

    letters: dict[str, str] = {}
    if "letters" in value:
        raw = value["letters"]
        if isinstance(raw, dict):
            for key, say in raw.items():
                letter = str(key).upper()
                if len(letter) != 1 or not ("A" <= letter <= "Z"):
                    problems.append(f"{where}.letters: {key!r} is not a letter")
                    continue
                try:
                    letters[letter] = _clean(say, "letter value", limits)
                except MarkupError as exc:
                    problems.append(f"{where}.letters: {exc}")
        else:
            problems.append(f"{where}.letters: must be an object")

    locale: str | None = None
    if "locale" in value:
        raw = value["locale"]
        if isinstance(raw, str) and raw in normalise.LOCALES:
            locale = raw
        else:
            problems.append(f"{where}.locale: {raw!r} is not supported")

    return (
        RuleSet(rules=rules, acronyms=acronyms, protect=protect, letters=letters, locale=locale),
        problems,
    )


def _alias_target(value: object, limits: Limits) -> str:
    if isinstance(value, dict):
        value = value.get("id")
    if not isinstance(value, str):
        raise MarkupError("alias values must be a voice name or {'id': ...}")
    return validate_voice(value.strip(), limits.voice_chars)


def _drop_cycles(aliases: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    problems: list[str] = []
    kept: dict[str, str] = {}
    for name in aliases:
        seen: set[str] = set()
        current = name
        while current in aliases and current not in seen:
            seen.add(current)
            current = aliases[current]
        if current in seen:
            problems.append(f"alias {name!r} forms a cycle; skipped")
            continue
        kept[name] = aliases[name]
    return kept, problems


def load_lexicon(path: str | Path, limits: Limits) -> LexiconData:
    """Read and verify the deployment file. Never raises: problems are data."""
    if not path:
        return EMPTY_LEXICON
    file = Path(path)
    try:
        size = file.stat().st_size
    except OSError as exc:
        return LexiconData(problems=(f"{file}: {exc.strerror or exc}",))
    if size > limits.lexicon_bytes:
        return LexiconData(problems=(f"{file}: {size} bytes exceeds {limits.lexicon_bytes}",))
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return LexiconData(problems=(f"{file}: {exc}",))
    if not isinstance(raw, dict):
        return LexiconData(problems=(f"{path}: top level must be an object",))

    problems: list[str] = []
    unknown = set(raw) - {"defaults", "voices", "aliases"}
    if unknown:
        problems.append(f"{path}: ignoring unknown keys {sorted(unknown)}")

    defaults, set_problems = parse_set(raw.get("defaults"), limits, "defaults")
    problems.extend(f"{path}: {problem}" for problem in set_problems)

    voices: dict[str, RuleSet] = {}
    raw_voices = raw.get("voices")
    if raw_voices is not None:
        if not isinstance(raw_voices, dict):
            problems.append(f"{path}: voices must be an object")
        elif len(raw_voices) > limits.voice_sets:
            problems.append(f"{path}: voices exceeds {limits.voice_sets} sets")
        else:
            for name, value in raw_voices.items():
                try:
                    key = validate_voice(str(name).strip(), limits.voice_chars)
                except MarkupError as exc:
                    problems.append(f"{path}: voice set {name!r}: {exc}")
                    continue
                voices[key], voice_problems = parse_set(value, limits, f"voices.{key}")
                problems.extend(f"{path}: {problem}" for problem in voice_problems)

    aliases: dict[str, str] = {}
    default_alias: str | None = None
    raw_aliases = raw.get("aliases")
    if raw_aliases is not None:
        if not isinstance(raw_aliases, dict):
            problems.append(f"{path}: aliases must be an object")
        else:
            for name, value in raw_aliases.items():
                try:
                    key = str(name).strip()
                    if key == "id":
                        default_alias = _alias_target(value, limits)
                        continue
                    validate_voice(key, limits.voice_chars)
                    aliases[key.casefold()] = _alias_target(value, limits)
                except MarkupError as exc:
                    problems.append(f"{path}: alias {name!r}: {exc}")
            if len(aliases) > limits.aliases:
                problems.append(f"{path}: aliases exceeds {limits.aliases}")
                aliases = dict(list(aliases.items())[: limits.aliases])
            aliases, cycle_problems = _drop_cycles(aliases)
            problems.extend(f"{path}: {problem}" for problem in cycle_problems)

    total_rules = len(defaults.rules) + sum(len(value.rules) for value in voices.values())
    if total_rules > limits.lexicon_rules:
        problems.append(f"{path}: {total_rules} rules exceeds {limits.lexicon_rules}; file ignored")
        return LexiconData(problems=tuple(problems))

    return LexiconData(
        defaults=defaults,
        voices=voices,
        aliases=aliases,
        default_alias=default_alias,
        problems=tuple(problems),
    )


def _compose_set(base: RuleSet, overlay: RuleSet) -> RuleSet:
    """Overlay one set on another: the overlay is tried first, then the base."""
    return RuleSet(
        rules=overlay.rules + base.rules,
        acronyms={**base.acronyms, **overlay.acronyms},
        protect=base.protect + tuple(item for item in overlay.protect if item not in base.protect),
        letters={**base.letters, **overlay.letters},
        locale=overlay.locale or base.locale,
    )


def compose(defaults: LexiconData, overlay: LexiconData) -> LexiconData:
    """Lay a user's customisations over the shipped defaults.

    The result is what the gateway uses as its deployment layer; request rules
    still sit on top of it. An overlay rule is tried before a default rule with
    the same span, which is what "overlay" means for an ordered scan.
    """
    if overlay is EMPTY_LEXICON:
        return defaults
    voices = dict(defaults.voices)
    for name, ruleset in overlay.voices.items():
        voices[name] = _compose_set(voices[name], ruleset) if name in voices else ruleset
    return LexiconData(
        defaults=_compose_set(defaults.defaults, overlay.defaults),
        voices=voices,
        aliases={**defaults.aliases, **overlay.aliases},
        default_alias=overlay.default_alias or defaults.default_alias,
        problems=defaults.problems + overlay.problems,
    )


def _stamp(path: str | None) -> tuple[int, int] | None:
    if not path:
        return None
    try:
        info = Path(path).stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


_WORD_PUNCTUATION = frozenset(" .'&+#-")
_SAY_PUNCTUATION = frozenset(" .'-,:")


def _validate_word(value: str, limits: Limits) -> str:
    """A learned word is a name, not markup. Keep the set deliberately small."""
    text = value.strip()
    if not text:
        raise MarkupError("word must not be empty")
    if len(text) > limits.pronunciation_chars:
        raise MarkupError(f"word exceeds {limits.pronunciation_chars} characters")
    if "|" in text:
        raise MarkupError("word must not contain |")
    for char in text:
        if not (char.isalnum() or char in _WORD_PUNCTUATION):
            raise MarkupError(
                "word may contain only letters, digits, spaces, apostrophes, "
                "hyphens, periods, ampersands, plus signs and hashes"
            )
    return text


def _validate_say(value: str, limits: Limits) -> str:
    """A respelling is letters, digits and a few punctuation marks, nothing else."""
    text = value.strip()
    if not text:
        raise MarkupError("say must not be empty")
    if len(text) > limits.pronunciation_chars:
        raise MarkupError(f"say exceeds {limits.pronunciation_chars} characters")
    for char in text:
        if not (char.isalnum() or char in _SAY_PUNCTUATION):
            raise MarkupError(
                "say may contain only letters, digits, spaces, apostrophes, "
                "hyphens, periods, commas and colons"
            )
    return text


def _validate_sense(value: str) -> str:
    """A sense is a short label, never markup."""
    text = value.strip()
    if not text:
        raise MarkupError("sense must not be empty")
    if len(text) > 64:
        raise MarkupError("sense exceeds 64 characters")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise MarkupError("sense must be letters, digits, underscore or hyphen")
    return text


def _rule_entry(rule: Rule, source: str) -> dict[str, object]:
    """One rule in the lookup response, including its sense and cues."""
    entry: dict[str, object] = {"match": rule.match, "say": rule.say, "source": source}
    if "|" in rule.match:
        entry["sense"] = rule.match.split("|", 1)[1]
    when: dict[str, object] = {}
    if rule.before:
        when["before"] = sorted(rule.before)
    if rule.after:
        when["after"] = sorted(rule.after)
    if rule.within is not None:
        when["within"] = rule.within
    if when:
        entry["when"] = when
    if rule.case_sensitive:
        entry["case_sensitive"] = True
    if not rule.whole_word:
        entry["whole_word"] = False
    return entry


def _rule_key(entry: dict[str, object]) -> tuple[object, ...]:
    when = entry.get("when")
    cues: tuple[object, ...] = ()
    if isinstance(when, dict):
        cues = (
            tuple(when.get("before", ())),
            tuple(when.get("after", ())),
            when.get("within"),
        )
    return (
        entry.get("match"),
        entry.get("say"),
        cues,
        entry.get("case_sensitive", False),
        entry.get("whole_word", True),
    )


class Lexicon:
    """The shipped defaults, the user overlay and the learned overlay.

    Priority is request rules, then the user overlay, then the learned overlay,
    then the shipped defaults. The learned file is the one the gateway writes;
    the user overlay stays read-only and always wins over a learned rule.
    """

    def __init__(
        self,
        path: str,
        limits: Limits,
        overlay_path: str | None = None,
        learned_path: str | None = None,
        write_enabled: bool = False,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.path = path
        self.overlay_path = overlay_path
        self.learned_path = learned_path
        self.write_enabled = write_enabled
        self.limits = limits
        self._telemetry = telemetry
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stamp: tuple[tuple[int, int] | None, ...] | None = None
        self._data = EMPTY_LEXICON
        self._layers: dict[str, LexiconData] = {}

    def current(self) -> LexiconData:
        stamp = (_stamp(self.path), _stamp(self.overlay_path), _stamp(self.learned_path))
        with self._lock:
            if stamp == self._stamp:
                return self._data

        # Read outside the lock: a slow disk must not block every request.
        base = load_lexicon(self.path, self.limits) if stamp[0] else EMPTY_LEXICON
        custom = (
            load_lexicon(self.overlay_path, self.limits)
            if self.overlay_path and stamp[1]
            else EMPTY_LEXICON
        )
        learned = (
            load_lexicon(self.learned_path, self.limits)
            if self.learned_path and stamp[2]
            else EMPTY_LEXICON
        )
        data = compose(compose(base, learned), custom)
        with self._lock:
            self._data = data
            self._layers = {"defaults": base, "learned": learned, "custom": custom}
            self._stamp = stamp
        if self._telemetry is not None:
            self._telemetry.lexicon_problems.set(len(data.problems))
        for problem in data.problems:
            warning(f"pronunciation lexicon: {problem}")
        event(
            "lexicon",
            rules=len(data.defaults.rules),
            voices=len(data.voices),
            aliases=len(data.aliases),
            locale=data.defaults.locale,
            overlay="yes" if stamp[1] else "no",
            learned="yes" if stamp[2] else "no",
            problems=len(data.problems),
        )
        return data

    @property
    def learned_rules(self) -> int:
        """How many rules the learned overlay currently contributes."""
        self.current()
        layer = self._layers.get("learned")
        return len(layer.defaults.rules) if layer is not None else 0

    def lookup(self, word: str) -> dict[str, object]:
        """Every deployment rule for one word, in priority order.

        Marker rules carry their sense, so a caller can choose a heteronym
        reading instead of guessing. Cue rules keep their context.
        """
        self.current()
        text = _validate_word(word, self.limits)
        head = text.casefold()
        entries: list[dict[str, object]] = []
        senses: list[dict[str, object]] = []
        seen: set[tuple[object, ...]] = set()
        seen_senses: set[str] = set()
        for source in ("custom", "learned", "defaults"):
            layer = self._layers.get(source)
            if layer is None:
                continue
            for rule in layer.defaults.rules:
                match = rule.match.casefold()
                if match != head and not match.startswith(head + "|"):
                    continue
                entry = _rule_entry(rule, source)
                key = _rule_key(entry)
                if key in seen:
                    continue
                seen.add(key)
                entries.append(entry)
                sense = entry.get("sense")
                if isinstance(sense, str) and sense not in seen_senses:
                    seen_senses.add(sense)
                    senses.append({"sense": sense, "say": rule.say, "source": source})
        return {"word": text, "found": bool(entries), "senses": senses, "rules": entries}

    def learn(self, word: str, say: str, sense: str | None = None) -> dict[str, object]:
        """Add or replace one rule in the learned overlay."""
        if not self.write_enabled:
            raise PermissionError("pronunciation learning is disabled")
        text = _validate_word(word, self.limits)
        spoken = _validate_say(say, self.limits)
        sense = _validate_sense(sense) if sense is not None else None
        match = f"{text}|{sense}" if sense else text
        rule = parse_rules([{"match": match, "say": spoken}], self.limits)[0]
        with self._write_lock:
            payload = self._read_learned()
            defaults = payload.setdefault("defaults", {})
            if not isinstance(defaults, dict):
                defaults = {}
                payload["defaults"] = defaults
            rules = defaults.get("rules")
            if not isinstance(rules, list):
                rules = []
            rules = [
                item
                for item in rules
                if not (
                    isinstance(item, dict)
                    and str(item.get("match", "")).casefold() == rule.match.casefold()
                )
            ]
            rules.append({"match": rule.match, "say": rule.say})
            if len(rules) > self.limits.lexicon_rules:
                raise MarkupError(f"learned lexicon would exceed {self.limits.lexicon_rules} rules")
            defaults["rules"] = rules
            self._write_learned(payload)
            self._stamp = None
        event("pronunciation_learn", word=text, sense=sense or "", match=rule.match)
        return {
            "ok": True,
            "word": text,
            "match": rule.match,
            "say": rule.say,
            "sense": sense,
            "source": "learned",
        }

    def forget(self, word: str, sense: str | None = None) -> dict[str, object]:
        """Remove one rule from the learned overlay, leaving other layers alone."""
        if not self.write_enabled:
            raise PermissionError("pronunciation learning is disabled")
        text = _validate_word(word, self.limits)
        sense = _validate_sense(sense) if sense is not None else None
        match = f"{text}|{sense}" if sense else text
        removed = 0
        with self._write_lock:
            payload = self._read_learned()
            defaults = payload.get("defaults")
            rules = defaults.get("rules") if isinstance(defaults, dict) else None
            if not isinstance(rules, list):
                rules = []
            kept = [
                item
                for item in rules
                if not (
                    isinstance(item, dict)
                    and str(item.get("match", "")).casefold() == match.casefold()
                )
            ]
            removed = len(rules) - len(kept)
            if removed:
                assert isinstance(defaults, dict)
                defaults["rules"] = kept
                self._write_learned(payload)
                self._stamp = None
        event("pronunciation_forget", word=text, sense=sense or "", removed=removed)
        return {"ok": True, "word": text, "match": match, "removed": removed}

    def _read_learned(self) -> dict[str, object]:
        if not self.learned_path:
            return {}
        path = Path(self.learned_path)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise MarkupError(f"learned lexicon is not readable: {exc}") from exc
        if path.is_symlink():
            raise MarkupError("learned lexicon must not be a symlink")
        if size > self.limits.lexicon_bytes:
            raise MarkupError(f"learned lexicon is {size} bytes, over {self.limits.lexicon_bytes}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MarkupError(f"learned lexicon is not readable: {exc}") from exc
        if not isinstance(raw, dict):
            raise MarkupError("learned lexicon top level must be an object")
        return raw

    def _write_learned(self, payload: dict[str, object]) -> None:
        if not self.learned_path:
            raise OSError("learned lexicon path is not configured")
        path = Path(self.learned_path)
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if len(text.encode("utf-8")) > self.limits.lexicon_bytes:
            raise MarkupError(f"learned lexicon would exceed {self.limits.lexicon_bytes} bytes")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".learned-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            # The learned file is not a secret; make it readable to the host
            # operator. The directory stays writable only by the service user.
            os.chmod(path, 0o644)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
