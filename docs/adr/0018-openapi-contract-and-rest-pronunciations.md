# The OpenAPI document is the contract, and the pronunciation surface becomes a REST resource

**Status:** accepted (implemented)
**Date:** 2026-11-19
**Author:** marcus
**Deciders:** marcus
**Refs:** ADR-0016, ADR-0017, ADR-0009, [`../pronunciation.md`](../pronunciation.md),
[`../../src/kokoro_tone/routes.py`](../../src/kokoro_tone/routes.py),
[`../../src/kokoro_tone/openapi.py`](../../src/kokoro_tone/openapi.py),
[`../../src/kokoro_tone/errors.py`](../../src/kokoro_tone/errors.py),
[`../../CHANGELOG.md`](../../CHANGELOG.md)

**Implemented:** the pronunciation resource (rules 3-6, 8-10) shipped with
ADR-0017, because a proposal needs a resource to live at. The dry run now lives
on `POST /v1/audio/speech?dry_run=1` (rule 7), `POST /v1/script/compile` is gone,
and `openapi.py` generates the document and adds the role and error-status
extensions. `tests/test_openapi.py` compares the document with the route table
in both directions (rules 1-2).

## Context

The pronunciation API grew by accretion in one release: `lookup` (a `POST` that
only reads), `learn` and `forget` (verbs in the path, with request bodies the
verbs imply), and `script/compile` (the render pipeline with the engine left
out). Each was the shortest path to a working caller. Together they no longer
describe one resource: there are three ways to name a pronunciation and no way
to list, page or filter them, and `forget` is the only deletion in the service
and takes a word rather than a thing.

ADR-0016 and ADR-0017 add two things the current surface cannot carry: a role
per operation, and a proposal that exists as a row with a status and a
lifecycle. That needs resources with identities, not verbs with payloads.

Meanwhile the base contract does hold: `createSpeech` and `listModels` are
OpenAI's, an SDK written against that shape works today, and nothing about this
change should make it stop working. The question is how far the non-base
surface may move, and how a reader learns what the contract is.

The deployment has no external subscribers. The only callers are the Morning
Glory episode renderer in this monorepo, `script_cli`, and an operator with
`curl`. That is what makes a clean break cheaper than a compatibility shim.

## Decision

1. **The OpenAPI document is the source of truth.** A route that is not in it
   does not exist, and every route in it names its role (`x-kokoro-tone-roles`),
   its parameters, its body, every status it can return, and the error envelope
   for each. The prose documents describe it; they do not define it.
2. **The document honours the base OpenAI spec and adds only optional
   extensions.** `createSpeech`, `listModels` and `listVoices` keep OpenAI's
   field names, request shape and response shape. Additions are new paths under
   `/v1/pronunciations`, new *optional* fields, and `x-kokoro-tone-*`
   extensions. Where this record breaks the non-base surface, it says so
   explicitly below rather than bending the base shape.
3. **Resources are nouns, and the operation id is the RBAC slug** (ADR-0016).
   The surface is:

   | Slug | Method and path |
   |---|---|
   | `listModels` | `GET /v1/models` |
   | `createSpeech` | `POST /v1/audio/speech` |
   | `listVoices` | `GET /v1/audio/voices` |
   | `listPronunciations` | `GET /v1/pronunciations` |
   | `createPronunciation` | `POST /v1/pronunciations` |
   | `updatePronunciation` | `PATCH /v1/pronunciations/{id}` (admin) |
   | `deletePronunciation` | `DELETE /v1/pronunciations/{id}` (admin) |
   | `retrievePronunciationAudio` | `GET /v1/pronunciations/{id}/audio` (admin) |
   | `getHealth` | `GET /health` |

4. **`lookup` becomes a filtered list.** `GET /v1/pronunciations?word=read&word=Banyule`
   returns the rules for those words across every layer, each row naming its
   `source` (`custom`, `learned`, `defaults`), plus `status`, `limit` and
   `after` for paging the learned rows.
5. **`learn` becomes create.** `POST /v1/pronunciations` with
   `{word, say, sense, context}` answers **201** when the rule is applied and
   **202** when it is queued for review (ADR-0017). Same body, different status:
   the mode is deployment config, and the status is how the caller learns which
   one it is in.
6. **`forget` becomes delete by id.** `DELETE /v1/pronunciations/{id}` deletes
   one row. An id is unambiguous where `{word, sense}` was not, and it is the
   only deletion that can be audited against the proposal that created it.
7. **`script/compile` becomes a dry run of the render.**
   `POST /v1/audio/speech?dry_run=1` returns the render's own prepared view -
   `text`, `turns`, `guesses`, `ambiguities`, `normalise` - with no audio, so a
   caller that already holds a `SpeechRequest` does not translate it into a
   second shape to preview the first.
8. **No aliases, no redirects, no deprecation window.** A caller using the old
   paths gets 404. A quiet alias would let two shapes coexist and the second one
   would be maintained forever; a clean 404 is the honest answer for a service
   with no external consumers.
9. **The status contract is fixed per operation.** 201 created and applied, 202
   accepted and pending, 400 for a bad payload (including an uppercase `say` in
   review mode), 401 uniform for every authentication failure, 403
   `insufficient_scope` for a recognised key on a slug it may not use, 404 for a
   missing row, 409 for a state conflict (`immutable_rule`, `invalid_transition`,
   `not_pending`). No 404 disguises an authorisation failure.
10. **One envelope for lists, one for objects.** A list is
    `{"object": "list", "data": [...]}`; a single pronunciation is
    `{"object": "pronunciation", ...}`. Errors keep the existing OpenAI-shaped
    body from `errors.py`, unchanged.
11. **`PRONUNCIATION_WRITE_KEY` retires.** Once writes are granted by slug
    (ADR-0016), the second secret has nothing left to distinguish. Its removal
    is part of this break, not a later cleanup.

### The breaking change

| Old | New | Note |
|---|---|---|
| `POST /v1/pronunciations/lookup` `{"words": [...]}` | `GET /v1/pronunciations?word=a&word=b` | read becomes read |
| `POST /v1/pronunciations/learn` `{word, say, sense}` → `200` | `POST /v1/pronunciations` `{word, say, sense, context}` → `201` or `202` | status now says whether it applied |
| `POST /v1/pronunciations/forget` `{word, sense}` | `DELETE /v1/pronunciations/{id}` | needs the id from the create response or the list |
| `POST /v1/script/compile` `SpeechRequest` | `POST /v1/audio/speech?dry_run=1` | same body, no audio |
| `X-Pronunciation-Key: <secret>` on the two write routes | an admin key on the bearer | `PRONUNCIATION_WRITE_KEY` is removed |
| `{"results": [...]}` from lookup | `{"object": "list", "data": [...]}` | one envelope for every list |
| ad-hoc write body (`{"word": "Aurec", "say": "AW-rek"}`) | `{"object": "pronunciation", "id": "...", "status": "applied"}` | the write now has an identifier |
| operation ids absent | `listPronunciations`, `createPronunciation`, `updatePronunciation`, `deletePronunciation`, `retrievePronunciationAudio` | ids are the RBAC slugs, so a rename is also a config change |

The base surface is untouched: a client that only calls `createSpeech` or
`listModels` sees no change. The CHANGELOG records this under the release that
carries it; there are no external subscribers, so the break ships whole.

## Considered options

**Version the break as `/v2`.** Rejected: with no external consumers, a second
prefix means two surfaces to maintain, two specs and a permanent question about
which one new work targets. The version number would buy compatibility nobody
needs.

**Keep the verbs and add REST beside them.** Rejected: two ways to write a
pronunciation, one of which has no role model and no proposal row, is the state
that produced the problem.

**Keep `POST /v1/script/compile`.** Rejected: it is the render with the engine
removed, and a second entry point to the same preparation drifts as soon as one
of them gains a field. `dry_run` cannot drift because it is the same handler.

**Keep `forget` keyed by word.** Rejected: two learned rules for one word with
different senses cannot be told apart by `{word, sense}` alone once a proposal
carries a sense, and deletion of a specific row should name that row.

**Write the spec by hand as a static file.** Rejected: it would be a fourth
description of the routes after the code, the models and the prose, and it would
be wrong the first time someone adds a field. The spec is generated from the
route models and asserted against the route table.

**Leave the operation ids alone and introduce new ones only for new routes.**
Rejected: the ids are what the allowlist names (ADR-0016), so half the surface
would be unnameable in policy.

## Consequences

- Every existing caller breaks in one upgrade: the Morning Glory pronunciation
  block, `script_cli`'s HTTP-shaped requests, and any operator habit. The change
  is atomic on purpose, and the CHANGELOG is the only warning anyone gets.
- `PRONUNCIATION_WRITE_KEY` disappears from configuration. A deployment that set
  it will start with a warning-free service and an admin key that now governs
  writes; that mismatch has to be documented in `configuration.md`, not left to
  the reader.
- A generated spec is only as good as its honesty test. The route table and the
  spec must be compared in a test, or the contract becomes a claim rather than a
  description.
- Operation ids become load-bearing configuration. Renaming `createPronunciation`
  is now an API break, a policy break and a CHANGELOG note, where before it was
  a rename.
- Dry run moves onto the speech route, so `createSpeech` gains a query parameter
  the base spec does not have. It is additive and optional, and the response is
  JSON where audio was expected, which is exactly the kind of asymmetry a client
  SDK will not expect.
- The proposal row's id is now part of the API surface: a caller that deletes a
  pronunciation has to keep the id from the create response or list it again.

## Verification

These hold:

- `GET /openapi.json` contains every slug in the table above and no other
  `/v1` path.
- A test compares the app's route table against the spec's paths and operation
  ids in both directions, and fails on either extra.
- An OpenAI SDK's `listModels` and `createSpeech` calls are unchanged.
- `POST /v1/pronunciations/learn`, `/lookup`, `/forget` and
  `POST /v1/script/compile` all answer 404. The three verb paths now collide
  with `/v1/pronunciations/{id}`, so FastAPI answers 405; either way no old
  handler runs.
- `POST /v1/audio/speech?dry_run=1` returns the same `guesses` and `text` the
  removed `script/compile` returned for the same body.
- `GET /v1/pronunciations?word=read` returns one row per layer with `source`
  filled in.
- Every error in `errors.py`'s map has a matching `responses` entry on the
  operation that can produce it.
