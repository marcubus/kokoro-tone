# Level the voice before applying emotion

**Status:** accepted (implemented)
**Date:** 2026-11-11
**Author:** sys-admin
**Refs:** ADR-0009, [`../voice-loudness.md`](../voice-loudness.md),
[`../audio.md`](../audio.md), [`../../src/kokoro_tone/levelling.py`](../../src/kokoro_tone/levelling.py)

## Context

ADR-0009 fixed the master as one linear gain over the whole file. That makes the
file deliverable, but it cannot equalise speakers: the measured raw spread is
9.3 dB across the English voices and 16 dB across all 72 engine voices. The fix
is a static per-voice casting gain, measured in
[`../voice-loudness.md`](../voice-loudness.md) and implemented in
[`../../src/kokoro_tone/levelling.py`](../../src/kokoro_tone/levelling.py).

Emotion is prosody: a preset changes rate, pitch, gain and EQ. Its gain is
deliberately relative - `whisper` is −8 dB, `angry` is +4 dB. That makes the
order of the two corrections a design decision, not an implementation detail. If
emotion were applied before levelling, or if levelling were measured after
emotion, the two would fight: levelling would cancel the preset gain, or the
preset would make the voice's correction depend on the line. Either way a
whisper would be normalised back to normal, which is exactly what ADR-0009
forbids.

## Decision

The audio pipeline applies, in this order:

1. Engine synthesis for one turn, at the turn's voice or blend.
2. **Static per-voice casting gain** (`volume_multiplier`), constant for the
   voice across the whole file.
3. **Per-turn emotion/prosody**: rate, pitch, preset gain and preset EQ.
4. Whole-file tone chain.
5. One linear gain to `LOUDNESS_LUFS`.
6. −1 dBTP limiter, then encode.

**Levelling comes first; emotion comes second.** Emotion gain is always relative
to an already-levelled voice.

The rules that follow from the order:

- The casting gain is a property of the voice, not the turn. It does not change
  with `[emotion:]`, `instructions`, `rate`, `speed` or text.
- The emotion preset gain is applied after levelling and is never normalised per
  segment.
- The master remains one gain for the whole file, after both. It fixes the
  file's average, not the speakers and not the performance.
- On the passthrough path there is no prosody and no master, but the casting gain
  still applies because it is sent to the engine as `volume_multiplier`. There
  is no emotion on that path (no tags, no `instructions`), so the ordering is
  trivially preserved.
- A blend's gain is the weighted average of its member gains, and that blend
  gain is the one applied before emotion.

## Considered options

**Apply emotion first, then level the result.** Rejected: levelling would cancel
the preset's gain, so `whisper` and `angry` would come out at the same level. It
would turn emotion into rate/pitch/EQ only.

**Include emotion in the levelling measurement.** Rejected: it makes a voice's
gain depend on the line. That is per-segment normalisation under another name,
and it violates ADR-0009.

**Rely on the whole-file master alone.** Rejected: that is the status quo that
produced the 9.3 dB spread. One gain cannot equalise two speakers.

**Apply levelling in the gateway after prosody.** Rejected: it puts the
correction after the stage it must precede, and it would require buffering or
measuring per turn. The engine already applies `volume_multiplier` in the float
domain and on the passthrough path.

**Per-segment `loudnorm`.** Rejected by ADR-0009: it erases deliberate dynamics.

## Consequences

- A whispered line stays quiet relative to the same voice speaking normally, and
  an angry line stays hot, because the preset gain is applied to the levelled
  signal.
- The table is calibrated pre-emotion and pre-master. Changing `LOUDNESS_LUFS`
  does not require recalibrating it; the master re-normalises the file.
- `VOICE_LEVELLING=false` removes step 2 only. Emotion and the master keep their
  order; it is a comparison switch, not a different pipeline.
- A future per-turn gain or performance layer must be inserted **after** step 2,
  not before it. If a stage changes level, it is an emotion/performance change
  and belongs on the emotion side of the line.
- The order is structural: `routes.render_turn` sends the casting gain with the
  synthesis request, and `audio.render` applies prosody to the returned PCM.
  `tests/test_api.py` pins the multiplier on both paths;
  `tests/test_audio.py` pins the emotion range and the whisper/normal gap.

## Verification

- 313 tests pass; `ruff check` and `ruff format --check` are clean.
- Live engine check: `af_nova` at `volume_multiplier=2.1627` measured 6.71 dB
  louder than unity, matching the table's +6.7 dB.
- `tests/test_audio.py::test_render_keeps_a_whispered_turn_quieter_than_a_normal_one`
  and `test_emotion_range_survives_the_hottest_allowed_target` pin the
  post-levelling emotion range.
- `tests/test_api.py` pins `volume_multiplier` on rendered turns, passthrough,
  SSE, blends and the `VOICE_LEVELLING=false` switch.
