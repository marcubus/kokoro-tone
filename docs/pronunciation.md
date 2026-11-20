# Pronunciation and normalisation

How a word in the script becomes a sound the engine can say: request rules,
the deployment lexicon, heteronym senses, and the opt-in digit and acronym
layers. Both `pronunciations` and `normalise` are optional; a request without
them behaves exactly as before.

This is the stable part of the gateway. It is deterministic, engine-independent
and usable on its own:

```bash
printf 'The bass player read HTML at 15%%.\n' \
  | python -m kokoro_tone.script_cli --lexicon lexicon/lexicon.json --normalise --json
```

The HTTP endpoint and the CLI share `script.compile_script`; neither adds a
second interpretation of the text.

![Pronunciation resolution: a specificity ladder picks the rule set, one scan applies it, and an uncued heteronym becomes a reported guess](diagrams/pronunciation-resolution.png)

*Diagram: [source](diagrams/pronunciation-resolution.excalidraw). The ladder
decides which rule wins; the scan decides what the listener hears. A bare
heteronym is guessed and reported rather than silently passed through.*

## Word mapping and normalisation

A script is easier to keep readable when the engine-specific spellings live in
data rather than in the prose. `pronunciations` maps words to the form the
engine should say; `normalise` turns on the built-in digit and acronym layers.

### `pronunciations`

A map is the shorthand:

```json
{"input": "The bass player.", "pronunciations": {"bass": "base"}}
```

A list adds context, so words spelt the same can be told apart:

```json
"pronunciations": [
  {"match": "bass", "say": "base", "when": {"after": ["band", "music", "plays"]}},
  {"match": "bass", "say": "bass"}
]
```

Each rule takes `match`, `say`, and optionally `when`, `case_sensitive`
(default false) and `whole_word` (default true). `when` accepts `before`,
`after` and `within`: the listed words are literal, case-insensitive and
whole-word, `before`/`after` are OR within a list and AND across lists. An empty
`say` deletes the match.

The window needs no configuration. A `before` cue looks back two words, because
it is a syntactic signal: "they record" is a verb, but "I think the record" is a
noun. An `after` cue looks to the end of the sentence, because it is a lexical
signal: "bass in the band" is music wherever "band" falls. A cue never crosses a
sentence boundary. `within` overrides both sides with an explicit count.

Rules are tried longest-first at each position, ties go to the earlier rule,
and an emitted replacement is never re-scanned. `[` and `]` are rejected, so a
replacement cannot inject markup. `pronunciations` applies even when
`normalise` is off.

### Heteronyms: forcing a sense

A word spelt the same but spoken differently needs its sense named, because the
engine's own tagger is unreliable on them. Name it with a tag, the same grammar
as `[voice:]` and `[emotion:]`:

```text
He plays [sense:bass=music] in the band. A [sense:bass=fish] swam past.
Two [sense:does=noun] grazed. It [sense:does=verb] work.
```

The tag is replaced by the checked respelling before synthesis, so the tag
itself is never spoken:

```text
He plays base in the band. A bas swam past.
Two dohz grazed. It duz work.
```

An unknown sense is a `400`, not text read aloud: `[sense:bass=jazz]` has no
rule, so the gateway says so instead of reading "bass jazz".

The 100 heteronyms live in `lexicon/lexicon.json` as rules, most used word
first: two marker rules (`word|sense`) and, where context can decide it, one or
more bare rules. There is no separate table and no build step - the file the
gateway loads is the file you edit.

#### Deciding a bare word

A script does not have to mark every occurrence. Where the surrounding words are
a reliable signal, a bare word is voiced from its context rules:

| Word | Cue | Chosen |
|---|---|---|
| `record` | `the` before | noun, `rek-awd` |
| `record` | `finance` before | noun, `rek-awd` |
| `record` | `to` before | verb, `rih-kawd` |
| `separate` | a number or determiner before | adjective, passed through as `separate` |
| `live` | a determiner before | adjective, `lyve` |
| `bass` | `band`, `music` after | music, `base` |
| `bass` | `fish`, `swam` after | fish, `bas` |
| `minute` | `detail` after | adjective, `my-newt` |
| `minute` | `the` before | noun, `min-it` |

Three rules govern the decision:

1. **Context first.** When a bare rule's cue fires, it decides. When none does,
   the word is reported as a guess (see below) rather than passed through
   silently.
2. **Most used word first.** Rules are ordered by frequency, because the scanner
   takes the first rule whose context is satisfied when two rules cover the same
   span. Within a word, the sense with the more specific rule is tested first,
   and the first candidate is what a guess picks.
3. **Some words are marker-only.** `stress` pairs (`digest`, `increase`,
   `transfer`, …) differ only in stress, which no respelling can split, so they
   have marker rules and no bare rules. They are never guessed or reported.

A bare rule's `when` is `before` / `after` word lists, and each side becomes its
own rule: `when` is AND across lists, so a noun cue before and a verb cue after
in one rule would require both. No window is written: a `before` cue looks back
two words and an `after` cue looks to the end of the sentence.

The decision covers the base form only. `records`, `recorded` and `recording`
need a tag or a per-request rule, because appending an inflection to a phonetic
respelling is not reliable.

A per-alias map keys rules by the voice or alias token, with `*` for the
global part:

```json
"voice": {"id": "narrator", "narrator": "bm_lewis", "guest": "am_michael"},
"pronunciations": {
  "*": {"Lachie": "Locky"},
  "narrator": [{"match": "bass", "say": "base"}],
  "guest": [{"match": "bass", "say": "fish"}]
}
```

#### When the context cannot tell

Some sentences give the context rules nothing to work with - *The bass was loud* is
music or fish, and no nearby word says which. The gateway does not fail and does
not stay silent: it picks the first candidate and says what it assumed.

```bash
curl -sSD - http://kokoro-tone:8000/v1/audio/speech \
  -H "Authorization: Bearer $KOKORO_TONE_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"input":"The bass was loud."}' -o out.mp3
# HTTP/1.1 200 OK
# x-kokoro-tone-guessed: bass=music
```

The audio says "base", and the header names the word and the sense chosen. A
pipeline can log it, or rewrite the script with a tag and re-render. The guess
is the first candidate in the rule order, which is the most-used word first and
the authored sense order within a word.

| `ambiguity` | Behaviour |
|---|---|
| `guess` (default) | first candidate, render, `X-Kokoro-Tone-Guessed` header |
| `ignore` | leave the engine's own default, no header |
| `error` | `400` with the tags to choose from; nothing rendered |

`error` is for a pipeline that would rather stop than ship a guess. The body
carries the exact tag to paste in:

```json
{
  "error": {
    "message": "1 word needs a sense tag: bass ([sense:bass=music], [sense:bass=fish]). Write the tag in the text, or add a pronunciation rule for it.",
    "type": "invalid_request_error",
    "param": "input",
    "code": "ambiguous_heteronym",
    "ambiguities": [
      {"word": "bass", "offset": 4,
       "options": ["[sense:bass=music]", "[sense:bass=fish]"],
       "says": ["base", "bas"],
       "chosen": "bass=music"}
    ]
  }
}
```

Every occurrence is reported with its own offset - within the turn that contains
it, so for an array `input` the offset is relative to that turn's text.

A word only counts as ambiguous when its senses have different respellings. The
`stress` rows are never guessed or reported, because a tag cannot change them
either.

### `normalise`

`normalise: true` enables the built-in layers; an object selects them:

```json
"normalise": {"lexicon": true, "acronyms": true, "numbers": true}
```

- `numbers` - Australian English digits: `3,913` → "three thousand nine hundred
  and thirteen", `0.43` → "nought point four three", `15%` → "fifteen per
  cent", `10-15` → "ten to fifteen", `21st` → "twenty-first". Years stay as
  digits. Model names, versions, dates, IPs and compound tokens (`MP3`, `H264`,
  `v2`, `Kokoro-82M`, `2.1.0`, `2026-11-11`) are left alone.
- `acronyms` - a table entry wins (`SQL` → "sequel"); otherwise a 2–6 letter
  uppercase token is spelled letter by letter (`HTML` → "Aitch Tee Emm Ell").
  Single letters, tokens touching digits, and the protect list (`IT`, `NO`,
  `YES` by default) are skipped.
- `lexicon` - the deployment file's rules, per-voice sets, letters and locale.

`normalise: true` uses the `NORMALISE_*` layer defaults; an object turns on
exactly its keys.

### Per-voice sets and server aliases

The deployment file is a collection of sets plus server aliases:

```json
{
  "defaults": {"rules": {"Lachie": "Locky"}, "protect": ["IT"]},
  "voices": {
    "bm_lewis": {"letters": {"A": "Aigh"}, "locale": "en-GB"},
    "am_michael": {"locale": "en-US"}
  },
  "aliases": {"id": "bm_lewis", "narrator": "bm_lewis"}
}
```

`defaults` and each `voices.<name>` share one shape: `rules`, `acronyms`,
`protect`, `letters`, `locale`. `aliases` ships empty; an `id` entry sets the
server default voice, and the request `voice` map sits on top of it.

The shipped file is the defaults. A deployment can add a user overlay
(`PRONUNCIATION_OVERRIDES`, `custom.json` beside it) and a learned overlay
(`PRONUNCIATION_LEARNED`, `learned/learned.json` beside it); the gateway
composes the three in memory. Priority is user overlay, then learned overlay,
then the shipped defaults, so a deliberate `custom.json` rule always wins.
Overlay rules are tried before default rules, overlay acronyms and letters win,
`protect` is the union, and an overlay `locale` wins. A missing overlay is
normal. Request rules still sit on top of the composed result.

The lookup for a turn is: request set for the token as written → deployment set
for that token → request set for the resolved voice → deployment set for the
resolved voice → request global (`*`) → deployment `defaults` → built-in. The
"deployment" set is the shipped defaults with the learned and user overlays
already composed in. So `[voice:bm_lewis]` mid-script uses that voice's set, and
an alias inherits the underlying voice's set.

### The learned overlay and the pronunciation API

The learned overlay is for readings a caller discovers at runtime. It is the
only lexicon file the gateway writes. In `force` mode (the default) the API is
available when `PRONUNCIATION_WRITE=true`; in `review` mode a proposal is
queued and an admin key decides (ADR-0017).

The pronunciation resource is nouns, not verbs, and the operation id is the
permission:

| Operation | Route | Who |
|---|---|---|
| `listPronunciations` | `GET /v1/pronunciations` | any key |
| `createPronunciation` | `POST /v1/pronunciations` | any key |
| `updatePronunciation` | `PATCH /v1/pronunciations/{id}` | admin |
| `deletePronunciation` | `DELETE /v1/pronunciations/{id}` | admin |
| `retrievePronunciationAudio` | `GET /v1/pronunciations/{id}/audio` | admin |

- `GET /v1/pronunciations?word=read&word=Banyule` returns every deployment rule
  for those words across layers, each row naming its `source` (`custom`,
  `learned` or `defaults`), its `id`, and the word's `senses`. A per-voice rule
  carries its `voice`.
- `GET /v1/pronunciations?status=pending` is the proposal queue, oldest first,
  with `limit` and `after` for paging. With no filter it lists every proposal.
- `POST /v1/pronunciations` with `{"word": "Aurec", "say": "AW-rek"}` applies a
  bare rule and answers `201` in `force` mode, or records a proposal and answers
  `202` in `review`. Add `"sense": "present-verb"` to write `word|sense`
  instead, and `"context"` for the sentence the audition renders.
- `PATCH /v1/pronunciations/{id}` with `{"status": "approved"}` applies a
  proposal; `{"status": "rejected", "reason": "..."}` refuses it and keeps the
  row, so the same suggestion is not made again in ignorance.
- `DELETE /v1/pronunciations/{id}` removes a proposal row, or deletes one
  learned rule. A shipped default or a `custom.json` rule is `409
  immutable_rule`; an unknown id is `404`.
- `GET /v1/pronunciations/{id}/audio?variant=as-is|proposed` renders the
  proposal's own context sentence with and without the reading. Approving from
  spelling is what produced the uppercase stress markers, so the decision is
  made on two takes of the same sentence.

A list is `{"object": "list", "data": [...]}`; a single row is
`{"object": "pronunciation", ...}` or `{"object": "proposal", ...}`. State
conflicts are `409` with a code: `invalid_transition` for an already decided
row, `not_pending` when a decision arrives in `force` mode, `immutable_rule` for
a shipped or user rule.

#### What the queue promises, and what it does not

- **A pending row never decides itself.** There is no timeout, no auto-approval
  and no background task; ADR-0017 rejected approve-by-timeout because it turns
  inattention into policy. An undecided row stays pending indefinitely, and the
  engine keeps its own reading while it does.
- **Pending work is never pruned.** At `MAX_PROPOSALS` (1024) the oldest
  *decided* rows are dropped first; if the *pending* rows alone exceed the cap,
  `POST /v1/pronunciations` answers `400` instead of discarding a proposal
  nobody has judged. The cap is checked on write, so a file that grew past it is
  still readable and listable.
- **Deleting a proposal is not undoing it.** `DELETE /v1/pronunciations/{id}`
  removes the row; a rule an approval already applied lives in `learned.json`
  and is deleted by its own `learned-…` id from `GET
  /v1/pronunciations?word=` (that route is the only place the id appears).
- **An approval is two writes, not a transaction.** The rule is applied first,
  the decision recorded second, so a crash between them leaves an applied rule
  with a still-pending row; re-approving is safe because the overlay replaces by
  `match`. A decided row is immutable - a second decision is `409
  invalid_transition`, which also covers two reviewers racing the same row.
- **The store is one file, one process.** `proposals.json` is loaded on every
  call and written atomically under a process lock. There is no index and no
  cross-process coordination, so two gateway processes sharing the path can lose
  a write. It is a single-service store by design.
- **Visibility is pull-only.** Reading the queue is the only way to see it;
  `/health` publishes `normalise.pending_proposals`, and nothing pushes,
  notifies or alerts on a growing queue. Watching that count is an operator
  habit, not a guarantee.
- **Corruption is loud.** A malformed file, an unknown version or a non-list
  `proposals` key is a `500` naming the file, never a silently empty queue.

In `review` mode an uppercase run in `say` (`MEL-bun`, `NISSUH`) is a `400`: it
is a proposer's stress guess, and the guess is the thing under review. In
`force` mode the same value is logged as a warning and applied, because the
caller is the operator.

Writes are atomic and reload the lexicon on the next request. A missing learned
file is normal; the first applied rule creates it. The learned file is not a
secret and is written readable to the host operator. The proposal store
(`PRONUNCIATION_PROPOSALS`, `proposals.json` beside `learned.json`) has the same
atomic write, byte cap and symlink refusal, and a corrupt queue is a `500` that
names the file rather than a silent empty list.

The write endpoints treat their input as data, never as markup. A `word` may
contain only letters, digits, spaces, apostrophes, hyphens, periods, ampersands,
plus signs and hashes; a `say` may contain only letters, digits, spaces,
apostrophes, hyphens, periods, commas and colons; a `sense` is letters, digits,
underscore or hyphen, at most 64 characters. Brackets, pipes, control
characters, path separators and quotes are rejected with a 400, so a request
cannot inject markup into the lexicon or reach another file. The learned file
must not be a symlink, and writes are refused once the file would exceed
`lexicon_rules` or `lexicon_bytes`.

The routes are ordinary operation ids and are granted by the role allowlist; see
[Configuration](configuration.md#roles-and-the-allowlist). `PRONUNCIATION_WRITE`
still decides whether a force-mode write is allowed to reach the file at all,
and `PRONUNCIATION_APPROVAL=review` needs an admin key to start.

The file is verified at load: file-level problems ignore the file, entry-level
problems skip that entry and keep the rest, and everything is logged to stderr.
It is reloaded when its mtime or size changes, so a word can be fixed without a
restart. A missing file is not a problem. See
[`lexicon/README.md`](../lexicon/README.md) for the schema.

