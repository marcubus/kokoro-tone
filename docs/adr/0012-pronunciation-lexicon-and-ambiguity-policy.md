# Pronunciation belongs to the gateway, and a guess is better than a failed render

**Status:** accepted
**Date:** 2026-11-11
**Author:** sys-admin
**Refs:** ADR-0009, [`../pronunciation.md`](../pronunciation.md),
[`../../lexicon/README.md`](../../lexicon/README.md)

## Context

ADR-0009 put a small gateway in front of Kokoro-FastAPI so a transcript could
become mastered narration. It deliberately stopped at prosody: `instructions`
presets, per-turn pitch, a warmth chain and a loudness target. Everything about
*what the words are* was left to the caller and to the engine.

That gap showed up as soon as the first real caller - an agent-generated podcast
script - used the service. The script's reference index already listed 100 words
spelt the same but spoken differently, alongside acronym spell-outs, proper nouns
whose phonetic form is not obvious, and Australian English number conventions. The
caller worked around the gap by hand-editing the script text: writing
`bass|music`, respelling names inline, expanding `2026` by hand. Every workaround
was a place a script could silently get it wrong, and the same edits had to be
repeated in every script.

Three problems had to be solved together, because they share one scan:

1. **Inbound pronunciation mapping.** A caller should send prose and have the
   gateway substitute the form the engine should say. Some rules are
   unconditional (`Lachie` → `Locky`); some only apply in context (`bass` before
   `band` is music, before `swam` is fish).
2. **Normalisation.** Digits, decimals, percentages, ranges, ordinals, years and
   acronyms need to become words, in the configured locale, without mangling
   versions, model names, dates or IPs.
3. **Heteronyms.** The 100-word index must be active, not a document the script
   author is expected to consult. Kokoro's own tagger is unreliable on these
   words, so the gateway has to decide, or say it cannot.

The tension is the third one. A wrong sense is worse than an obvious failure, so
the first design made an undecided heteronym a `400`. In practice that made the
service unusable for batch narration: one sentence with a genuinely ambiguous
word stopped a whole episode, and the caller had no way to fix it except by
editing prose it did not want to own. The error was also the *common* case, not
the rare one, because many of the 100 words have no reliable local cue.

## Decision

Move pronunciation into the gateway, keep the request contract OpenAI-shaped, and
make an unsettled heteronym a **guess that is reported**, not an error.

### One ordered rule list, resolved by specificity

`pronunciations` on `POST /v1/audio/speech` is an ordered list of literal rules:

```json
{"match": "bass", "say": "base", "when": {"after": ["band", "music"]}}
```

`match` and `say` are literal strings, never regular expressions, and `[` / `]`
are rejected in both so a rule cannot forge a tag. The effective rule set for a
turn is assembled by specificity, not by merge order:

```
request alias → deployment alias → request resolved voice
→ deployment resolved voice → request global `*` → deployment `defaults` → built-in
```

The deployment lexicon (`PRONUNCIATION_LEXICON`) uses the same rule shape plus
`acronyms`, `protect`, `letters` and `locale`, at three levels: `defaults`,
`voices.<token>` and `aliases`. `voices` is keyed by the token **as written in
the request**, so `[voice:narrator]` and `[voice:bm_lewis]` can carry different
sets even when the alias resolves to the same engine voice. A request set always
beats a deployment set at the same specificity.

Request rules always apply. Deployment rules apply only when
`normalise.lexicon` is on (or `NORMALISE_DEFAULT=true`), so a caller can opt out
of deployment policy without opting out of its own rules.

The scan is one left-to-right pass. Emitted text is never re-scanned, so a
substitution cannot cascade. Longest match wins; ties fall to rule order, and
rule order is meaningful.

### The deployment lexicon is the shipped defaults plus an in-memory overlay

`lexicon/lexicon.json` is the shipped defaults: hand-written rules, acronyms,
letters, protect list and locale, plus the 200 heteronym marker rules and 262
bare context rules. There is no build step, no separate table and no CSV.

A deployment may add `custom.json` (`PRONUNCIATION_OVERRIDES`) beside it. The
gateway composes the two **in memory** when it loads them: overlay rules are
tried before default rules, overlay acronyms and letters win, `protect` is the
union, and an overlay `locale` wins. The overlay never repeats the defaults, and
the defaults never contain a deployment's private names. Per-request
`pronunciations` and `voice` still sit on top of the composed result.

Both files are reloaded on mtime/size change, so an edit needs no restart. A
file-level problem ignores that file; an entry-level problem skips that entry; a
missing file (either one) is not an error. The service always starts.

### Sense is a tag, not a bare marker

The first implementation wrote the sense inline as `bass|music`. It worked, but
it was the only signal in the grammar that was not a tag - `[voice:]`,
`[emotion:]`, `[rate:]` and `[pause:]` all are. It also read as prose, so it was
easy to mistype and impossible to validate without a lexicon lookup.

The sense is now a tag, desugared by the parser to the internal rule key:

```text
He plays [sense:bass=music] in the band.   → He plays base in the band.
A [sense:bass=fish] swam past.             → A bas swam past.
```

An unknown sense is a `400` naming the tag, never text read aloud: a caller who
wrote a tag meant to say something, and guessing over it would be worse than
failing.

### Context windows are asymmetric and never cross a sentence

A `before` cue looks back **two words** by default: syntax is local (`the record`
vs `to record`). An `after` cue looks to the **end of the sentence**: lexical
cues travel (`bass` is settled by `band` or `swam` several words later). Neither
side crosses a sentence boundary. An explicit `within` overrides both sides and
remains an escape hatch; the shipped data does not use it.

The look-back is fixed at two words. The rule exists so a context rule can be
written without counting the window - the common mistake is to set a window too
small and get a silent miss.

### Ambiguity: guess, report, or stop

| `ambiguity` | Behaviour |
|---|---|
| `guess` (default) | substitute the first candidate, render, set `X-Kokoro-Tone-Guessed` |
| `ignore` | leave the engine's own default, no header |
| `error` | `400`, `code: ambiguous_heteronym`, nothing rendered |

A guess is the first candidate in rule order: most-used word first (Zipf score
from `wordfreq`), and within a word the more specific authored sense. The header
carries the compact `word=sense` choices, deduplicated and capped, so a pipeline
can log them or rewrite the script with tags and re-render. `error` still exists
for a pipeline that would rather stop than ship a guess, and its body hands back
the exact tags to paste.

`stress` rows (`digest`, `increase`, `transfer`, …) are marker-only: no
respelling can split them, so they are never guessed or reported. A word only
counts as ambiguous when its senses have **different respellings**.

A guess never fires inside a marker. `bass|jazz` is a sense the lexicon lacks, so
it fails loudly rather than being rewritten to `base|jazz`.

### Only deployment choices are environment variables

The render bounds keep their env overrides, because they are the service's
safety envelope. The pronunciation surface is deliberately small:

| Variable | Purpose |
|---|---|
| `PRONUNCIATION_LEXICON` | the shipped rules |
| `PRONUNCIATION_OVERRIDES` | the user overlay |
| `LOCALE` | the fallback locale when the lexicon sets none |
| `NORMALISE_DEFAULT` | what an absent `normalise` field means |

Everything else - digit words, year range, number bound, protect list, letter
names, the context look-back, the rule/alias/voice-set caps - is a constant or
comes from the locale table and the lexicon. A deployment that needs a different
number word edits the locale table in code; one that needs a different proper
noun adds an overlay rule. `DEFAULT_VOICE` and `NORMALISE_DEFAULT` are deployment
choices; neither is baked into the defaults.

## Considered options

**Keep pronunciation in the caller.** Rejected: the data existed in an agent's
scratch skill, but the *scan* is not, and every caller would reimplement the same
one-pass matcher, the same precedence and the same sentence window. Two callers
would drift.

**A second service for text preparation.** Rejected: it would need the same
request context (voice, alias, locale) and would add a network hop and a failure
mode for something that is pure string work. The gateway already parses tags and
already knows the resolved voice.

**Regex rules.** Rejected: a caller-supplied regex over untrusted prose is a
ReDoS surface and a footgun, and the shipped data does not need it. Literal
rules plus context cues cover the real cases.

**Merge all rule sets into one map.** Rejected: a deployment default would
silently beat a request rule for the same word. Specificity-first resolution
makes the winner predictable and keeps a caller's own rules authoritative.

**Keep `400` as the default for an unsettled heteronym.** Rejected: the common
case, not the rare one. A batch render should not fail because one word is
genuinely ambiguous; the caller can see the guess in a header and decide.

**Guess silently.** Rejected: a wrong word with no signal is the worst outcome.
The header is cheap and makes the guess auditable.

**A bare `|` marker.** Rejected in favour of a tag, for grammar consistency and
because it can be validated at parse time.

## Consequences

- The gateway now owns text policy as well as prosody. That is more code and
  more tests, and a bug in the scanner changes what every caller hears.
- The 462 rules are explicit in one file. Shared cue lists are repeated per
  rule, so changing a cue set means editing several rules; that is the trade for
  having no build step and no second schema.
- The defaults/overlay split keeps the shipped file free of deployment names
  while still allowing a user to add rules without forking it.
- Guesses are visible, not silent, but they are still guesses. A caller that
  cares must read `X-Kokoro-Tone-Guessed` or set `ambiguity: "error"`.
- The scanner is bounded: rules are bucketed by folded first character and words
  are grouped by sentence, so a 4 KB script passes in ~19 ms rather than ~400 ms.
- ADR-0009 is not retro-edited. It still names the pre-rename paths; this ADR
  records the feature that grew out of it.

## Verification

- `make check` (format + lint) and `uv run pytest -q` - 296 tests.
- `lexicon/lexicon.json` loads with no problems: 462 rules, 200 marker rules,
  100 heteronym words.
- End-to-end against the real lexicon (`rules: 462`, 100 words):
  a script containing a year, a percentage, a thousands separator, two acronyms,
  two heteronyms settled by context, and three unsettled ones renders `200`, with
  `X-Kokoro-Tone-Guessed: read=present-verb,close=verb` and the expected spoken
  text.
- `ambiguity: "error"` on the same ambiguous word returns `400` with
  `code: ambiguous_heteronym` and the pasteable tags.
- `[sense:bass=fish]` renders `bas`; `[sense:bass=jazz]` returns `400` naming the
  unknown tag.
- `GET /health` reports the active normalisation layers and rule count.
