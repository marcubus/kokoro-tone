"""Pronunciation mapping: precedence, the scanner and the built-in layers.

The deployment file and its validation live in `lexicon.py`. This module takes
the effective rules for a turn and scans the text once, left to right, so a
replacement is never re-scanned and a tag value is never touched.

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

import re
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field

from . import normalise
from .lexicon import LexiconData, Rule
from .limits import Limits
from .parse import MarkupError

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


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
