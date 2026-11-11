"""Pronunciation mapping: ordered rules, per-voice sets, and the scanner.

A request can carry `pronunciations` (explicit rules) and `normalise` (the
built-in layers). The deployment file adds server-wide rules, per-voice sets
and server aliases. Everything reduces to one left-to-right scan per turn, so
a replacement is never re-scanned and a tag value is never touched.

Priority, most specific first:

1. request rules for this turn's token (the alias or voice as written)
2. request rules for the resolved voice
3. request global rules (`*`)
4. deployment rules for the token
5. deployment rules for the resolved voice
6. deployment `defaults`
7. acronym table, then the letter-spelling fallback
8. number normalisation

Steps 1-3 always apply. Steps 4-6 are the deployment file, gated by
`normalise.lexicon`; step 7 by `normalise.acronyms`; step 8 by
`normalise.numbers`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field

from . import normalise
from .limits import Limits
from .log import event
from .parse import MarkupError, sanitise_text, validate_voice

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

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
class Ambiguity:
    """A heteronym no bare rule settled, so the ordered rules decide it.

    `say` is the first candidate's respelling; `chosen` is the compact
    `word=sense` the response header reports.
    """

    word: str
    offset: int
    say: str
    chosen: str


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
            kept: list[str] = []
            for item in raw:
                try:
                    kept.append(_acronym_key(item, limits))
                except MarkupError as exc:
                    problems.append(f"{where}.protect: {exc}")
            protect = tuple(kept)
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


def load_lexicon(path: str, limits: Limits) -> LexiconData:
    """Read and verify the deployment file. Never raises: problems are data."""
    if not path:
        return EMPTY_LEXICON
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return LexiconData(problems=(f"{path}: {exc.strerror or exc}",))
    if size > limits.lexicon_bytes:
        return LexiconData(problems=(f"{path}: {size} bytes exceeds {limits.lexicon_bytes}",))
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        return LexiconData(problems=(f"{path}: {exc}",))
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
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


class Lexicon:
    """The shipped defaults plus the user's overlay, reloaded on change.

    The two files are composed in memory, so the overlay never has to repeat the
    defaults and the defaults never have to know about a deployment.
    """

    def __init__(self, path: str, limits: Limits, overlay_path: str | None = None) -> None:
        self.path = path
        self.overlay_path = overlay_path
        self.limits = limits
        self._lock = threading.Lock()
        self._stamp: tuple[tuple[int, int] | None, tuple[int, int] | None] | None = None
        self._data = EMPTY_LEXICON

    def current(self) -> LexiconData:
        stamp = (_stamp(self.path), _stamp(self.overlay_path))
        with self._lock:
            if stamp != self._stamp:
                base = load_lexicon(self.path, self.limits) if stamp[0] else EMPTY_LEXICON
                overlay = (
                    load_lexicon(self.overlay_path, self.limits)
                    if self.overlay_path and stamp[1]
                    else EMPTY_LEXICON
                )
                self._data = compose(base, overlay)
                for problem in self._data.problems:
                    print(f"pronunciation lexicon: {problem}", file=sys.stderr)
                event(
                    "lexicon",
                    rules=len(self._data.defaults.rules),
                    voices=len(self._data.voices),
                    aliases=len(self._data.aliases),
                    locale=self._data.defaults.locale,
                    overlay="yes" if stamp[1] else "no",
                    problems=len(self._data.problems),
                )
                self._stamp = stamp
            return self._data


# ---------------------------------------------------------------- precedence


@dataclass(frozen=True)
class EffectiveSet:
    # `(from_deployment, rule)` in priority order. Specificity interleaves the
    # sources: a request rule for this alias beats the deployment set for the
    # same alias, which beats a request rule for the resolved voice, and so on.
    rules: tuple[tuple[bool, Rule], ...] = ()
    acronyms: dict[str, str] = field(default_factory=dict)
    protect: frozenset[str] = frozenset()
    letters: dict[str, str] = field(default_factory=dict)
    locale: str = normalise.DEFAULT_LOCALE


def _ordered_tokens(token: str | None, voice: str | None) -> list[str]:
    ordered: list[str] = []
    for item in (token, voice):
        if item and item not in ordered:
            ordered.append(item)
    return ordered


def effective_set(
    token: str | None,
    voice: str | None,
    request_global: tuple[Rule, ...],
    request_alias: dict[str, tuple[Rule, ...]],
    lexicon: LexiconData,
    settings: normalise.NormaliseSettings,
) -> EffectiveSet:
    tokens = _ordered_tokens(token, voice)

    ordered: list[tuple[bool, Rule]] = []
    for name in tokens:
        ordered.extend((False, rule) for rule in request_alias.get(name, ()))
        ruleset = lexicon.voices.get(name)
        if ruleset:
            ordered.extend((True, rule) for rule in ruleset.rules)
    ordered.extend((False, rule) for rule in request_global)
    ordered.extend((True, rule) for rule in lexicon.defaults.rules)

    # Scalars merge least specific first, so the most specific set wins.
    chain = [lexicon.defaults]
    chain.extend(lexicon.voices[name] for name in reversed(tokens) if name in lexicon.voices)

    locale = settings.locale
    for ruleset in chain:
        if ruleset.locale:
            locale = ruleset.locale

    letters = normalise.letters_for(locale)
    protect = set(settings.protect)
    acronyms: dict[str, str] = {}
    for ruleset in chain:
        letters.update(ruleset.letters)
        protect.update(ruleset.protect)
        acronyms.update(ruleset.acronyms)

    return EffectiveSet(
        rules=tuple(ordered),
        acronyms=acronyms,
        protect=frozenset(protect),
        letters=letters,
        locale=locale,
    )


# ---------------------------------------------------------------- scanner


# A hint never reaches across a sentence boundary: "Two does. It does." is two
# decisions, not one. Abbreviations shorten the window, which can only make a
# rule miss, never misfire.
_SENTENCE_END = frozenset(".!?\n")

Word = tuple[int, int, str]


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for index, char in enumerate(text):
        if char in _SENTENCE_END:
            spans.append((start, index))
            start = index + 1
    spans.append((start, len(text)))
    return spans


def _words(text: str, spans: list[tuple[int, int]]) -> dict[int, list[Word]]:
    """Words grouped by sentence, so a context check never scans the whole text."""
    groups: dict[int, list[Word]] = {}
    sentence = 0
    for match in _WORD_RE.finditer(text):
        while sentence + 1 < len(spans) and match.start() >= spans[sentence][1]:
            sentence += 1
        groups.setdefault(sentence, []).append(
            (match.start(), match.end(), match.group().casefold())
        )
    return groups


def _context_ok(index: int, end: int, rule: Rule, words: list[Word], before_words: int) -> bool:
    if rule.before:
        prior = [word for start, stop, word in words if stop <= index]
        # A `before` cue is syntax: "they record" is a verb, but "I think the
        # record" is a noun. It only counts when it is adjacent, so the default
        # is a short look-back rather than the whole sentence.
        prior = prior[-rule.within :] if rule.within is not None else prior[-before_words:]
        if not any(word in rule.before for word in prior):
            return False
    if rule.after:
        later = [word for start, stop, word in words if start >= end]
        if rule.within is not None:
            later = later[: rule.within]
        if not any(word in rule.after for word in later):
            return False
    return True


def _rule_end(
    text: str,
    index: int,
    rule: Rule,
    folded: str,
    words: list[Word],
    before_words: int,
) -> int | None:
    end = index + len(rule.match)
    if end > len(text):
        return None
    if rule.case_sensitive:
        if text[index:end] != rule.match:
            return None
    elif text[index:end].casefold() != folded:
        return None
    if rule.whole_word:
        if rule.match[0].isalnum() and index > 0:
            if text[index - 1].isalnum() or text[index - 1] == "_":
                return None
        if rule.match[-1].isalnum() and end < len(text):
            if text[end].isalnum() or text[end] == "_":
                return None
    if not _context_ok(index, end, rule, words, before_words):
        return None
    return end


class _RuleMatcher:
    def __init__(self, rules: tuple[Rule, ...], limits: Limits) -> None:
        # Index by folded first character: at any position only the rules that
        # could start there are tried, and the original order is kept inside a
        # bucket, which is all the tie-break needs.
        self.by_head: dict[str, list[tuple[Rule, str]]] = {}
        for rule in rules:
            folded = rule.match.casefold()
            self.by_head.setdefault(folded[0], []).append((rule, folded))
        self.before_words = limits.before_words
        self.text = ""
        self.starts: list[int] = []
        self.groups: dict[int, list[Word]] = {}
        # A word with two decision rules that disagree is a heteronym the hints
        # may fail to settle. The markers are what the caller can write instead.
        self.pairs, self.pairs_by_head = self._pairs(rules)

    @staticmethod
    def _pairs(rules: tuple[Rule, ...]) -> tuple[dict[str, tuple[str, str]], dict[str, list[str]]]:
        # The header reports the compact `word=sense`; the scanner uses the
        # first candidate's respelling.
        markers: dict[str, list[tuple[str, str]]] = {}
        says: dict[str, list[str]] = {}
        for rule in rules:
            if "|" in rule.match:
                word, _, key = rule.match.partition("|")
                markers.setdefault(word.casefold(), []).append((f"{word}={key}", rule.say))
            elif rule.before or rule.after:
                bucket = says.setdefault(rule.match.casefold(), [])
                if rule.say not in bucket:
                    bucket.append(rule.say)
        pairs: dict[str, tuple[str, str]] = {}
        for word, values in says.items():
            if len(values) < 2:
                continue
            available = markers.get(word, [])
            # `values` keeps rule order, so the first is the authored default,
            # and `chosen` must name the sense that produces it: a guess and its
            # header have to agree even if a lexicon orders markers differently.
            chosen = next((compact for compact, say in available if say == values[0]), values[0])
            pairs[word] = (values[0], chosen)
        by_head: dict[str, list[str]] = {}
        for word in pairs:
            by_head.setdefault(word[0], []).append(word)
        return pairs, by_head

    def bind(self, text: str) -> Callable[[int], tuple[int, str] | None]:
        spans = _sentence_spans(text)
        self.text = text
        self.starts = [start for start, _ in spans]
        self.groups = _words(text, spans)
        return self._match

    def _match(self, index: int) -> tuple[int, str] | None:
        sentence = bisect_right(self.starts, index) - 1
        words = self.groups.get(sentence, ())
        head = self.text[index].casefold()
        best: tuple[int, str] | None = None
        for rule, folded in self.by_head.get(head, ()):
            end = _rule_end(self.text, index, rule, folded, words, self.before_words)
            if end is None:
                continue
            if best is None or end - index > best[0] - index:
                # Keep a sentence-initial capital: "Wind the clock" must not
                # become "wined the clock".
                say = rule.say
                if say and self.text[index].isupper() and say[0].islower():
                    say = say[0].upper() + say[1:]
                best = (end, say)
        return best

    def ambiguity_at(self, index: int) -> Ambiguity | None:
        """The word starting at `index`, if it is a heteronym nothing resolved."""
        if not self.pairs_by_head:
            return None
        head = self.text[index].casefold()
        for word in self.pairs_by_head.get(head, ()):
            end = index + len(word)
            if end > len(self.text) or self.text[index:end].casefold() != word:
                continue
            if index > 0 and (self.text[index - 1].isalnum() or self.text[index - 1] == "_"):
                continue
            if end < len(self.text) and (self.text[end].isalnum() or self.text[end] == "_"):
                continue
            # `word|sense` is a marker the caller wrote, not a bare word: an
            # unknown sense must fail loudly, never be guessed over.
            if end < len(self.text) and self.text[end] == "|":
                continue
            # A marker, a longer rule, or a hint that fired all settle it.
            if self._match(index) is not None:
                continue
            say, chosen = self.pairs[word]
            return Ambiguity(word=self.text[index:end], offset=index, say=say, chosen=chosen)
        return None

    def guess_at(self, index: int) -> tuple[Ambiguity, int, str] | None:
        """The unsettled word and the replacement a guess would make."""
        ambiguity = self.ambiguity_at(index)
        if ambiguity is None:
            return None
        say = ambiguity.say
        if say and self.text[index].isupper() and say[0].islower():
            say = say[0].upper() + say[1:]
        return ambiguity, index + len(ambiguity.word), say


class _AcronymMatcher:
    def __init__(self, table: dict[str, str], letters: dict[str, str], protect: frozenset[str]):
        self.table = table
        self.letters = letters
        self.protect = protect

    def bind(self, text: str) -> Callable[[int], tuple[int, str] | None]:
        return lambda index: normalise.acronym_match(
            text, index, self.table, self.letters, self.protect
        )


class _NumberMatcher:
    def __init__(self, style: normalise.NumberStyle) -> None:
        self.style = style

    def bind(self, text: str) -> Callable[[int], tuple[int, str] | None]:
        return lambda index: normalise.number_match(text, index, self.style)


class Rewriter:
    """One left-to-right scan. Emitted text is never re-scanned."""

    def __init__(self, matchers: list[object]) -> None:
        self.matchers = matchers

    def apply(
        self,
        text: str,
        *,
        ambiguities: list[Ambiguity] | None = None,
        guess: bool = False,
    ) -> str:
        """Rewrite `text`.

        With `ambiguities`, collect the heteronyms nothing settled. With
        `guess`, substitute the first candidate for each of them, so the caller
        gets audio and a warning rather than an error.
        """
        if not self.matchers or not text:
            return text
        bound = [matcher.bind(text) for matcher in self.matchers]
        out: list[str] = []
        index = 0
        length = len(text)
        while index < length:
            for match in bound:
                hit = match(index)
                if hit is not None and hit[0] > index:
                    out.append(hit[1])
                    index = hit[0]
                    break
            else:
                replacement = None
                if ambiguities is not None:
                    replacement = self._note_ambiguity(index, ambiguities, guess)
                if replacement is not None:
                    end, say = replacement
                    out.append(say)
                    index = end
                else:
                    out.append(text[index])
                    index += 1
        return "".join(out)

    def _note_ambiguity(
        self, index: int, found: list[Ambiguity], guess: bool
    ) -> tuple[int, str] | None:
        for matcher in self.matchers:
            probe = getattr(matcher, "guess_at", None)
            if probe is None:
                continue
            hit = probe(index)
            if hit is None:
                continue
            ambiguity, end, say = hit
            found.append(ambiguity)
            return (end, say) if guess else None
        return None


@dataclass(frozen=True)
class Flags:
    lexicon: bool = False
    acronyms: bool = False
    numbers: bool = False


def parse_flags(value: object, settings: normalise.NormaliseSettings) -> Flags:
    """`true` uses the env layer defaults; an object turns on exactly its keys."""
    if value is None:
        if not settings.default_on:
            return Flags()
        return Flags(settings.lexicon, settings.acronyms, settings.numbers)
    if isinstance(value, bool):
        if not value:
            return Flags()
        return Flags(settings.lexicon, settings.acronyms, settings.numbers)
    if not isinstance(value, dict):
        raise MarkupError("normalise must be true, false, or an object of layers")
    unknown = set(value) - {"lexicon", "acronyms", "numbers"}
    if unknown:
        raise MarkupError(f"unknown normalise keys {sorted(unknown)}")
    for key, item in value.items():
        if not isinstance(item, bool):
            raise MarkupError(f"normalise.{key} must be a boolean")
    return Flags(
        lexicon=value.get("lexicon", False),
        acronyms=value.get("acronyms", False),
        numbers=value.get("numbers", False),
    )


def build_rewriter(
    effective: EffectiveSet,
    flags: Flags,
    settings: normalise.NormaliseSettings,
    limits: Limits,
) -> Rewriter:
    matchers: list[object] = []
    rules = tuple(
        rule for from_deployment, rule in effective.rules if flags.lexicon or not from_deployment
    )
    if rules:
        matchers.append(_RuleMatcher(rules, limits))
    if flags.acronyms:
        matchers.append(_AcronymMatcher(effective.acronyms, effective.letters, effective.protect))
    if flags.numbers:
        style = normalise.style_for(effective.locale, limits.number_words)
        matchers.append(_NumberMatcher(style))
    return Rewriter(matchers)
