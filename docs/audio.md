# Prosody and mastering

Kokoro's glottal source is fixed, so emotion here is measurable prosody rather
than acting: rate, pitch, gain and spectral tilt, mastered once per file.

## `instructions` → prosody

Free text is matched against a keyword table. `[emotion:x]` overrides it per
turn. Rate and pitch are clamped to the configured bounds.

| Preset | rate | pitch | gain | EQ |
|---|---|---|---|---|
| warm | ×0.95 | −1 st | +1 dB | low-shelf +1.5 dB @ 250 Hz |
| bright | ×1.05 | +1 st | +1 dB | high-shelf +1.5 dB @ 6 kHz |
| sad | ×0.88 | −2 st | −3 dB | lowpass 6 kHz |
| tired | ×0.90 | −1 st | −2 dB | lowpass 8 kHz |
| angry | ×1.15 | +1 st | +4 dB | treble +2 dB, compression 3:1 |
| whisper | ×0.85 | 0 | −8 dB | highpass 300 Hz, lowpass 8 kHz |
| excited | ×1.12 | +2 st | +2 dB | treble +3 dB |

Pitch uses `rubberband` with formant preservation. If ffmpeg lacks it, the chain
falls back to `asetrate` + `atempo`, which shifts formants and is audible.

## Mastering

Applied once, to the whole file:

```
low-shelf  +2.0 dB @ 250 Hz
high-shelf −1.5 dB @ 6 kHz
acompressor ratio 2.5, attack 15 ms, release 200 ms
tanh saturation at low drive
single linear gain → −16 LUFS (podcast default; `LOUDNESS_LUFS`)
alimiter −1 dBTP
```

Loudness is measured *after* the warmth chain and corrected with one linear
gain. It is never normalised per segment: that would boost a whispered line
back to normal and erase the performance.

The consequence is that the master cannot equalise **voices**: it makes the file
correct and leaves the casting bias intact. Kokoro's raw output spans 9.2 dB
across the English voices alone, so a multi-speaker script keeps its quiet and
loud speakers. That is a separate, static correction - measured per voice in
[`voice-loudness.md`](voice-loudness.md).

## Passthrough, rendering and gain

Three paths, and they do not gain the same way:

| Request | Gain |
|---|---|
| **Passthrough + `stream: true`** (plain string, no tags, no `instructions`) | none - the engine's bytes are piped through unmastered |
| **Passthrough + `stream_format: "sse"`** | none - the engine's chunks are wrapped as deltas |
| **Rendered + `stream_format: "sse"`** (tags or `instructions`) | full chain, buffered; the first delta arrives after mastering |
| **Rendered + `stream: true`** | `stream` is ignored; the body is a complete file |

Preset gain is relative (whisper is −8 dB before the master), so it survives the
master's single absolute gain. The limiter is last, so a hot preset cannot push
past −1 dBTP.

The honest limit: **mastering and incremental streaming cannot both happen.**
Real-time gain would mean measuring per chunk, which is the per-segment
normalisation the design forbids. If a client needs mastering, it gets the file
after rendering; if it needs bytes as they are produced, it gets the engine's
unmastered output.

