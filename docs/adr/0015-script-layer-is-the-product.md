# ADR-0015: The script layer is the product; tone is an experimental lab

## Status

accepted

## Context

The project began as an OpenAI-compatible wrapper that added prosody and
mastering to Kokoro-FastAPI. That framing made "tone" the headline, but the
parts that are actually stable, deterministic and independently useful are the
text ones:

- ordered pronunciation rules with `before` / `after` / `within` context
- heteronym senses, a specificity ladder, and a *reported* guess
- an ambiguity policy a pipeline can act on
- Australian number, range, percent and acronym expansion with a protect list
- a deployment lexicon with per-voice sets, overlays and hot reload

None of that depends on Kokoro, on ffmpeg, or on the OpenAI wire shape. The
prosody presets, by contrast, are uncalibrated (see the README to-do) and the
model cannot take direction. Selling the second as the point of the project
undersells the first and overstates the second.

A second question follows: if the gateway should be easy to experiment with,
how much of ffmpeg should a client be able to reach? The existing invariant is
that filter expressions are built from constants only (`audio._filters`,
`make security-check`). That invariant is load-bearing and must survive.

## Decision

1. **The script layer is the hero.** The README, the package description and
   `AGENTS.md` lead with script interpretation - pronunciation, heteronyms,
   normalisation and markup - and present it as usable on its own, including a
   CLI that needs no server and no engine.
2. **Tone is an experimental lab.** The prosody presets remain, clearly labelled
   as experimental, with the calibration to-do kept in view. They are a place to
   try measurable DSP, not a claim of acting.
3. **Extensibility is declarative.** New DSP blocks are data: a constant
   template plus typed, bounded numeric parameters, validated against a filter
   allowlist and the existing filter grammar. Clients select a block with
   `[filter:name key=value ...]`; they never supply filter syntax.
4. **Plugins are manifests, not code paths.** An installed distribution may
   advertise a manifest through an entry point. The payload is validated by the
   same schema as the built-in blocks; it cannot introduce a new execution sink.
   A bad manifest is skipped with a warning.

## Options considered

- **Keep tone as the headline.** Rejected: it is the part with the weakest
  evidence and the least independent value.
- **Expose arbitrary ffmpeg strings through a tag.** Rejected: it removes the
  constants-only invariant, turns every request into an injection surface, and
  makes the pinned ffmpeg build impossible to reason about.
- **A Python plugin API for filters.** Rejected for audio: a plugin that can
  emit arbitrary filters is equivalent to arbitrary ffmpeg. Text-transform
  plugins may still be code because they run in the same trust domain as the
  gateway, but they are a separate seam and not part of this decision.
- **A sandboxed worker for untrusted chains.** Deferred: the most flexible
  option, but a much larger project than the lab needs.

## Consequences

- The script layer gets its own facade (`script.py`), its own CLI
  (`script_cli.py`) and its own tests, so it can be used and versioned without
  the HTTP service.
- `pipeline.prepare` becomes a thin adapter over the script facade.
- The filter registry (`filters.py`) owns the block schema. `audio.py` keeps its
  own final grammar check as defence in depth.
- Adding a filter block now means adding a constant template and typed params,
  plus the matching `--enable-filter` in the Dockerfile if the binary lacks it.
- The docs must keep the experimental label on every preset. Repositioning is
  not a calibration claim.

## Links

- [ADR-0009](0009-openai-compatible-kokoro-gateway.md) - the original framing
  this record re-centres.
- [ADR-0012](0012-pronunciation-lexicon-and-ambiguity-policy.md) - the
  pronunciation decision the script layer is built on.
- [`../pronunciation.md`](../pronunciation.md) - the script-layer contract.
- [`../audio.md`](../audio.md) - the experimental tone layer.
