"""Number and acronym normalisation, as position matchers.

Both are pure functions of `(text, index)`, so the single scanner in
`pronounce.py` can try them in priority order and never re-scan what it has
already emitted. Nothing here builds a regular expression from client input:
the patterns are module constants, and the only client data that reaches a
match is a literal token compared with `str` methods.

Locale tables are Australian by default. `en-GB` shares them; `en-US` differs
in the three places that matter to speech: `Zee`, `zero` and `percent`.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

from .env import read_bool, read_str

# Letter names for the acronym fallback. `Aigh` is the only spelling that
# reads as the letter A in Kokoro's bm_lewis voice; the rest are the in-flow
# convention with no punctuation between them.
_BASE_LETTERS = {
    "A": "Aigh",
    "B": "Bee",
    "C": "See",
    "D": "Dee",
    "E": "Ee",
    "F": "Eff",
    "G": "Gee",
    "H": "Aitch",
    "I": "Eye",
    "J": "Jay",
    "K": "Kay",
    "L": "Ell",
    "M": "Emm",
    "N": "En",
    "O": "Oh",
    "P": "Pee",
    "Q": "Cue",
    "R": "Are",
    "S": "Ess",
    "T": "Tee",
    "U": "You",
    "V": "Vee",
    "W": "Double You",
    "X": "Ex",
    "Y": "Why",
}


@dataclass(frozen=True)
class LocaleTable:
    letters: dict[str, str]
    zero: str
    percent: str
    hundreds_and: bool


LOCALES: dict[str, LocaleTable] = {
    "en-AU": LocaleTable({**_BASE_LETTERS, "Z": "Zed"}, "nought", "per cent", True),
    "en-GB": LocaleTable({**_BASE_LETTERS, "Z": "Zed"}, "nought", "per cent", True),
    "en-US": LocaleTable({**_BASE_LETTERS, "Z": "Zee"}, "zero", "percent", False),
}
DEFAULT_LOCALE = "en-AU"

# Uppercase tokens that are words, not acronyms. Editable from the environment
# and from the pronunciation file; this is only the shipped baseline.
DEFAULT_PROTECT = ("IT", "NO", "YES")

_DEFAULT_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_DEFAULT_TENS = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)
_DEFAULT_SCALES = (
    (10**12, "trillion"),
    (10**9, "billion"),
    (10**6, "million"),
    (10**3, "thousand"),
)
_DEFAULT_ORDINALS = {
    "one": "first",
    "two": "second",
    "three": "third",
    "four": "fourth",
    "five": "fifth",
    "six": "sixth",
    "seven": "seventh",
    "eight": "eighth",
    "nine": "ninth",
    "ten": "tenth",
    "eleven": "eleventh",
    "twelve": "twelfth",
    "thirteen": "thirteenth",
    "fourteen": "fourteenth",
    "fifteen": "fifteenth",
    "sixteen": "sixteenth",
    "seventeen": "seventeenth",
    "eighteen": "eighteenth",
    "nineteen": "nineteenth",
    "twenty": "twentieth",
    "thirty": "thirtieth",
    "forty": "fortieth",
    "fifty": "fiftieth",
    "sixty": "sixtieth",
    "seventy": "seventieth",
    "eighty": "eightieth",
    "ninety": "ninetieth",
    "hundred": "hundredth",
    "thousand": "thousandth",
    "million": "millionth",
    "billion": "billionth",
    "trillion": "trillionth",
}

_PERCENT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)%")
_ORDINAL_RE = re.compile(r"(\d+)(?:st|nd|rd|th)")
_DECIMAL_RE = re.compile(r"(\d[\d,]*)\.(\d+)")
_RANGE_RE = re.compile(r"(\d+)-(\d+)")
_CARDINAL_RE = re.compile(r"\d[\d,]*")
_GROUPED_RE = re.compile(r"^\d{1,3}(?:,\d{3})+$")

# A number is only rewritten when it stands alone. These characters block a
# match when they sit immediately before it; at the right edge a punctuation
# mark only blocks when a digit follows, so `15.` is a sentence end while
# `2.1.0` stays a version.
_LEFT_BLOCKERS = frozenset("._,-/:")
_RIGHT_BLOCKERS = frozenset("._,-/:")


@dataclass(frozen=True)
class WordTables:
    ones: tuple[str, ...] = _DEFAULT_ONES
    tens: tuple[str, ...] = _DEFAULT_TENS
    scales: tuple[tuple[int, str], ...] = _DEFAULT_SCALES
    ordinals: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_ORDINALS))


@dataclass(frozen=True)
class NormaliseSettings:
    locale: str = DEFAULT_LOCALE
    default_on: bool = False
    lexicon: bool = True
    acronyms: bool = True
    numbers: bool = True
    protect: tuple[str, ...] = DEFAULT_PROTECT


@dataclass(frozen=True)
class NumberStyle:
    zero: str
    percent: str
    hundreds_and: bool
    number_max: int
    year_min: int
    year_max: int
    max_words: int
    tables: WordTables = field(default_factory=WordTables)


def load_normalise_settings() -> NormaliseSettings:
    locale = read_str("LOCALE", DEFAULT_LOCALE)
    if locale not in LOCALES:
        print(f"LOCALE={locale!r} is not supported; using {DEFAULT_LOCALE}", file=sys.stderr)
        locale = DEFAULT_LOCALE
    return NormaliseSettings(locale=locale, default_on=read_bool("NORMALISE_DEFAULT", False))


def letters_for(locale: str) -> dict[str, str]:
    table = LOCALES.get(locale) or LOCALES[DEFAULT_LOCALE]
    return dict(table.letters)


def style_for(locale: str, max_words: int) -> NumberStyle:
    table = LOCALES.get(locale) or LOCALES[DEFAULT_LOCALE]
    return NumberStyle(
        zero=table.zero,
        percent=table.percent,
        hundreds_and=table.hundreds_and,
        number_max=999_999_999_999,
        year_min=1_000,
        year_max=2_999,
        max_words=max_words,
    )


# ---------------------------------------------------------------- words


def _under_100(value: int, tables: WordTables) -> str:
    if value < 20:
        return tables.ones[value]
    tens, ones = divmod(value, 10)
    return tables.tens[tens] + (f"-{tables.ones[ones]}" if ones else "")


def _under_1000(value: int, use_and: bool, tables: WordTables) -> str:
    hundreds, rest = divmod(value, 100)
    parts: list[str] = []
    if hundreds:
        parts.append(f"{tables.ones[hundreds]} hundred")
    if rest:
        if hundreds and use_and:
            parts.append("and")
        parts.append(_under_100(rest, tables))
    return " ".join(parts)


def number_to_words(value: int, use_and: bool = True, tables: WordTables | None = None) -> str:
    """Cardinal words. British/Australian grouping: `one hundred and five`."""
    tables = tables or WordTables()
    if value < 0:
        raise ValueError("negative numbers are not normalised")
    if value == 0:
        return tables.ones[0]
    parts: list[str] = []
    remaining = value
    for scale, name in tables.scales:
        if remaining >= scale:
            count, remaining = divmod(remaining, scale)
            parts.append(f"{_under_1000(count, use_and, tables)} {name}")
    if remaining:
        if parts and use_and and remaining < 100:
            parts.append("and")
        parts.append(_under_1000(remaining, use_and, tables))
    return " ".join(parts)


def ordinal_to_words(value: int, use_and: bool = True, tables: WordTables | None = None) -> str:
    tables = tables or WordTables()
    words = number_to_words(value, use_and, tables)
    head, _, last = words.rpartition(" ")
    if "-" in last:
        prefix, _, final = last.rpartition("-")
        last = f"{prefix}-{tables.ordinals.get(final, final)}"
    else:
        last = tables.ordinals.get(last, last)
    return f"{head} {last}".strip()


# ---------------------------------------------------------------- matchers


def _blocked_left(text: str, index: int) -> bool:
    if index == 0:
        return False
    previous = text[index - 1]
    return previous.isalnum() or previous in _LEFT_BLOCKERS


def _blocked_right(text: str, index: int) -> bool:
    if index >= len(text):
        return False
    following = text[index]
    if following.isalnum():
        return True
    if following in _RIGHT_BLOCKERS:
        return index + 1 < len(text) and text[index + 1].isdigit()
    return False


def _cardinal(value: int, style: NumberStyle) -> str | None:
    if value > style.number_max:
        return None
    words = number_to_words(value, style.hundreds_and, style.tables)
    if len(words.split()) > style.max_words:
        return None
    return words


def _number_words(body: str, style: NumberStyle) -> str | None:
    if "." in body:
        whole, _, fraction = body.partition(".")
        value = int(whole.replace(",", ""))
        head = (
            style.zero if value == 0 else number_to_words(value, style.hundreds_and, style.tables)
        )
        digits = " ".join(style.tables.ones[int(digit)] for digit in fraction)
        return f"{head} point {digits}"
    return _cardinal(int(body.replace(",", "")), style)


def _match_percent(text: str, index: int, style: NumberStyle):
    match = _PERCENT_RE.match(text, index)
    if not match:
        return None
    end = match.end()
    body = match.group(1)
    if "," in body and not _GROUPED_RE.match(body.split(".")[0]):
        return None
    words = _number_words(body, style)
    if words is None:
        return end, match.group(0)
    if len(words.split()) + 1 > style.max_words:
        return end, match.group(0)
    return end, f"{words} {style.percent}"


def _match_ordinal(text: str, index: int, style: NumberStyle):
    match = _ORDINAL_RE.match(text, index)
    if not match:
        return None
    end = match.end()
    value = int(match.group(1))
    if value > style.number_max:
        return end, match.group(0)
    words = ordinal_to_words(value, style.hundreds_and, style.tables)
    if len(words.split()) > style.max_words:
        return end, match.group(0)
    return end, words


def _match_decimal(text: str, index: int, style: NumberStyle):
    match = _DECIMAL_RE.match(text, index)
    if not match:
        return None
    end = match.end()
    if _blocked_right(text, end):
        return None
    whole, fraction = match.group(1), match.group(2)
    if "," in whole and not _GROUPED_RE.match(whole):
        return None
    value = int(whole.replace(",", ""))
    if value > style.number_max:
        return end, match.group(0)
    head = style.zero if value == 0 else number_to_words(value, style.hundreds_and, style.tables)
    digits = " ".join(style.tables.ones[int(digit)] for digit in fraction)
    words = f"{head} point {digits}"
    if len(words.split()) > style.max_words:
        return end, match.group(0)
    return end, words


def _match_range(text: str, index: int, style: NumberStyle):
    match = _RANGE_RE.match(text, index)
    if not match:
        return None
    end = match.end()
    if _blocked_right(text, end):
        return None
    low, high = match.group(1), match.group(2)
    if len(low) > 3 or len(high) > 3:
        return None
    first = _cardinal(int(low), style)
    second = _cardinal(int(high), style)
    if first is None or second is None:
        return None
    words = f"{first} to {second}"
    if end < len(text) and text[end] == "%":
        end += 1
        words = f"{words} {style.percent}"
    if len(words.split()) > style.max_words:
        return end, match.group(0)
    return end, words


def _match_cardinal(text: str, index: int, style: NumberStyle):
    match = _CARDINAL_RE.match(text, index)
    if not match:
        return None
    end = match.end()
    if _blocked_right(text, end):
        return None
    token = match.group(0)
    if "," in token:
        if not _GROUPED_RE.match(token):
            return None
        words = _cardinal(int(token.replace(",", "")), style)
        return (end, words) if words else (end, token)
    if len(token) == 4 and style.year_min <= int(token) <= style.year_max:
        return end, token  # years stay as digits
    words = _cardinal(int(token), style)
    return (end, words) if words else (end, token)


_NUMBER_MATCHERS = (
    _match_percent,
    _match_ordinal,
    _match_decimal,
    _match_range,
    _match_cardinal,
)


def number_match(text: str, index: int, style: NumberStyle):
    """Return `(end, replacement)` for a number starting at `index`, or None."""
    if _blocked_left(text, index):
        return None
    for matcher in _NUMBER_MATCHERS:
        hit = matcher(text, index, style)
        if hit is not None:
            return hit
    return None


def acronym_match(
    text: str,
    index: int,
    table: dict[str, str],
    letters: dict[str, str],
    protect: frozenset[str],
):
    """Return `(end, spoken)` for an uppercase token at `index`, or None."""
    if index > 0 and (text[index - 1].isalnum() or text[index - 1] == "_"):
        return None
    if not ("A" <= text[index] <= "Z"):
        return None
    end = index
    while end < len(text) and "A" <= text[end] <= "Z" and end - index < 6:
        end += 1
    token = text[index:end]
    if len(token) < 2:
        return None
    if end < len(text) and (text[end].isalnum() or text[end] == "_"):
        return None
    if token in protect:
        return None
    spoken = table.get(token)
    if spoken is None:
        spoken = " ".join(letters.get(letter, letter) for letter in token)
    return end, spoken
