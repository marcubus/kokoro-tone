# Architecture decisions

Decision records for `kokoro-tone`. Each one captures a choice that was
expensive to reverse, the options that lost, and what the choice costs.

| ADR | Title | Status |
|---|---|---|
| [0009](0009-openai-compatible-kokoro-gateway.md) | Serve Kokoro behind an OpenAI-compatible warmth gateway | accepted (implemented) |
| [0012](0012-pronunciation-lexicon-and-ambiguity-policy.md) | Pronunciation belongs to the gateway, and a guess is better than a failed render | accepted |

## Why the numbers start at 0009

These records were written in the deployment repo (`selfhosted/`) before this
project was split out, so their numbers are inherited rather than reassigned.
Keeping the original numbers means every existing cross-reference - in
`tests/test_audio.py`, in the deployment's `AGENTS.md`, and in the sibling
records - still resolves. A tidy `0001` would have silently broken all of them.

The gap is not a missing history: 0001-0008 are decisions about a home lab
(website hosting, uptime probes, boot ordering, an OS upgrade) that do not belong
to this project and are not reproduced here.

## Reading these

An ADR records the decision **at the time it was made**. Two of them name paths
and environment variables that have since changed (`tts/` became
`src/kokoro_tone/`, `TTS_API_KEY` became `KOKORO_TONE_API_KEY`). They are not
retro-edited - that would erase the record of what was actually decided. The
current contract is [`../../README.md`](../../README.md); where an ADR and the
README disagree, the README is right. The current module map is in
[`../development.md`](../development.md#layout).

Where a record depends on a measurement rather than a preference, the evidence
lives beside it: [`../voice-loudness.md`](../voice-loudness.md) is the
per-voice loudness review behind the mastering and casting decisions.

## Adding one

Append the next number in sequence. Status is one of `proposed`, `accepted`,
`superseded` (name the replacement), or `rejected`. Never rewrite an accepted
record to match a later change - write a new one and link back.
