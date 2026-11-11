# The lexicon

The gateway reads the shipped defaults at `PRONUNCIATION_LEXICON`
(`/etc/kokoro-tone/lexicon.json` in the container), plus an optional user
overlay at `PRONUNCIATION_OVERRIDES` (`/etc/kokoro-tone/custom.json`). This
directory is mounted read-only by the compose file.

`lexicon.json` is the shipped defaults: what you edit is what the gateway
loads. There is no build step. The file is reloaded when its mtime or size
changes, so a fix needs no restart.

## Shape

```json
{
  "defaults": {
    "rules": [
      {"match": "Lachie", "say": "Locky"},
      {"match": "bass|music", "say": "base"},
      {"match": "bass", "say": "base", "when": {"after": ["band", "music"]}}
    ],
    "acronyms": {"SQL": "sequel", "AWS": "Aigh Double You Ess"},
    "protect": ["IT", "NO", "YES", "END"],
    "letters": {"A": "Aigh"},
    "locale": "en-AU"
  },
  "voices": {
    "bm_lewis": {"letters": {"A": "Aigh"}, "locale": "en-GB"},
    "am_michael": {"locale": "en-US"}
  },
  "aliases": {"id": "bm_lewis", "narrator": "bm_lewis"}
}
```

`defaults` and each `voices.<name>` are the same **set** shape:

| Key | Meaning |
|---|---|
| `rules` | a `{match: say}` map, or a list of rule objects |
| `acronyms` | exact uppercase token to spoken form |
| `protect` | uppercase tokens that are never letter-spelled |
| `letters` | letter-name overrides for the acronym fallback |
| `locale` | `en-AU`, `en-GB` or `en-US` for this set |

`voices` is keyed by the voice or alias token **as written in the request**, so
`[voice:bm_lewis]` and `[voice:narrator]` can carry different sets. `aliases`
accepts a voice name or `{"id": ...}`; an `id` entry sets the server default
voice. It ships empty.

## A rule

```json
{
  "match": "bass",
  "say": "base",
  "when": {"after": ["band", "music"]},
  "case_sensitive": false,
  "whole_word": true
}
```

| Key | Default | Meaning |
|---|---|---|
| `match` | required | literal, never regex; keeps its spacing; `[`/`]` rejected |
| `say` | required | stripped; `""` deletes the match |
| `when.before` / `when.after` | `[]` | cue words, OR within a list, AND across lists |
| `when.within` | unset | explicit window; overrides both sides |
| `case_sensitive` | `false` | |
| `whole_word` | `true` | |

With no `within`, a `before` cue looks back two words (syntax is local) and an
`after` cue looks to the end of the sentence (lexical cues travel). Neither
crosses a sentence boundary.

## Defaults and the user overlay

`lexicon.json` is the shipped defaults. A deployment can add `custom.json`
(`PRONUNCIATION_OVERRIDES`) beside it; the gateway composes the two **in
memory** when it loads them:

| Key | Composition |
|---|---|
| `rules` | overlay rules are tried first, then the defaults |
| `acronyms`, `letters` | overlay keys win |
| `protect` | union of both lists |
| `locale` | overlay wins if it sets one |
| `voices` | per token, the overlay set is composed over the default set |
| `aliases`, default voice | overlay wins |

The overlay never has to repeat the defaults, and the defaults never contain a
deployment's private names. A missing overlay is normal. Per-request
`pronunciations` and `voice` still sit on top of the composed result.

`custom.example.json` is the empty shape to copy. `custom.json` is gitignored:
it is the user's file, not the project's.

## Verification

Both files are checked at load. A file-level problem (unreadable, not an object,
over `MAX_LEXICON_BYTES`, over `MAX_LEXICON_RULES`, too many sets or aliases)
ignores the whole file. An entry-level problem (bad rule, unknown key, invalid
alias name, alias cycle, unsupported locale) skips that entry and keeps the
rest. Problems are logged to stderr; the service always starts.

A missing file is not a problem: the gateway runs with the built-in layers
only.

## Heteronyms

A word spelt the same but spoken differently gets two **marker rules**, one per
sense, plus optional **bare rules** that decide an unmarked occurrence from its
context.

```json
{"match": "bass|music", "say": "base"},
{"match": "bass|fish", "say": "bas"},
{"match": "bass", "say": "base", "when": {"after": ["band", "music"]}},
{"match": "bass", "say": "bas", "when": {"after": ["fish", "swam"]}}
```

The marker is internal; a script author writes the tag, and the parser desugars
it before the scan:

```text
He plays [sense:bass=music] in the band.   -> He plays base in the band.
A [sense:bass=fish] swam past.             -> A bas swam past.
```

The tag is `[sense:<word>=<sense>]`, where `<sense>` is the part after the `|`
in the marker rule. An unknown sense is a `400`, never text read aloud.

### Deciding a bare word

Where the surrounding words are a reliable signal, a bare occurrence is voiced
from its bare rules:

```text
The record was a hit.        -> The rek-awd was a hit.
They record every week.      -> They rih-kawd every week.
```

The policy, in the order the file applies it:

1. **Hints first, then a guess.** A bare rule that fires decides. When none
   does, the word is a guess: the first candidate is substituted and reported in
   the `X-Kokoro-Tone-Guessed` response header, so the caller can fix the script.
2. **Most used word first.** Rules are ordered by frequency, because the scanner
   takes the first rule whose context is satisfied when two rules cover the same
   span. Within a word, the sense with the more specific rules is tested first.
3. **Some words are marker-only.** `stress` pairs (`digest`, `increase`,
   `transfer`, …) differ only in stress, which no respelling can split, so they
   have marker rules and no bare rules. They are never guessed or reported.

The decision covers the base form only. `records`, `recorded` and `recording`
need a tag or a per-request rule, because appending an inflection to a phonetic
respelling is not reliable.

### Adding a word

1. Add the two marker rules (`word|sense`) with their respellings.
2. If the context can decide a bare occurrence, add one bare rule per cue side.
3. Keep the rules most-used first: a later rule is shadowed by an earlier one
   whose context also fires.
4. Give the senses readable keys. `bow` uses `ribbon` / `ship`, and `sewer`
   uses `one-who-sews` / `the-drain`, rather than two senses both called
   `noun`.

The host-specific defaults (voice, normalise-by-default, protect list) live in
`compose.override.yml` next to the compose file.
