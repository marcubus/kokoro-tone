# Prosody and mastering

Kokoro's glottal source is fixed, so emotion here is measurable prosody rather
than acting: rate, pitch, gain and spectral tilt, mastered once per file.

## `instructions` → prosody

Free text is matched against a keyword table. `[emotion:x]` overrides it per
turn; `[emotion:neutral]` clears that override again. Rate and pitch are clamped
to the configured bounds.

| Preset | rate | pitch | gain | EQ | Δ tilt | Δ HF |
|---|---|---|---|---|---|---|
| warm | ×0.95 | −1.2 st | +1 dB | low-shelf +1 dB @ 250 Hz, high-shelf −1 dB @ 6 kHz | −0.31 dB/oct | −1.6 dB |
| bright | ×1.036 | +1.1 st | +1 dB | high-shelf +5 dB @ 6 kHz, low-shelf −1 dB @ 600 Hz | +0.36 dB/oct | +1.3 dB |
| sad | ×0.88 | −2 st | −3 dB | lowpass 6 kHz | −0.73 dB/oct | −3.1 dB |
| tired | ×0.90 | −1 st | −2 dB | lowpass 8 kHz | −0.35 dB/oct | −1.5 dB |
| angry | ×1.15 | +1 st | +4 dB | treble +2 dB, compression 3:1 | +0.32 dB/oct | +1.3 dB |
| whisper | ×0.85 | 0 | −8 dB | highpass 300 Hz, lowpass 8 kHz | +0.47 dB/oct | +2.4 dB |
| excited | ×1.12 | +2 st | +2 dB | high-shelf +9 dB @ 6 kHz, low-shelf −1 dB @ 600 Hz | +0.72 dB/oct | +2.9 dB |

Δ tilt and Δ HF are measured against neutral for the same voice and prose, after
the master, so they are what a listener actually hears rather than the nominal
filter gain. A shelf's gain is asymptotic: `+5 dB @ 6 kHz` arrives as +2.4 dB in
the 8 kHz octave band.

> **Calibration status.** Only `bright` has been calibrated against a measured
> post-master tilt (the `bm_lewis(4)+bm_fable(1)` blend). The other rows are
> provisional: their EQ constants are reasoned from the filter response, not
> validated by a full measurement pass, and their Δ tilt and Δ HF figures are
> estimates. `sad` and `whisper` are the weakest. `sad`'s 6 kHz lowpass plus
> −3 dB reads as muffled rather than sad, and `whisper`'s 8 kHz lowpass removes
> the air that would make it read as a whisper. Expect these presets to change.

> **Gain is nominal, not delivered.** The `gain` column is applied before the
> preset EQ and before the tone chain's compressor and limiter. Positive gains
> are partly absorbed; negative gains mostly survive. Measured on `af_heart`,
> same sentence, after the master: warm +0.3 dB, bright +0.4 dB, sad −2.9 dB,
> tired −1.7 dB, angry +0.3 dB, whisper −8.7 dB, excited +0.7 dB, against
> nominal +1, +1, −3, −2, +4, −8, +2. Do not treat the gain column as a
> promise.

Tilt is moved from **both ends** - a treble shelf up and a low-mid shelf down.
The bass cut is about a third of the effect (the `+5` shelf alone gives
+0.38 dB/oct, the `−1 @ 600` adds +0.27), which lets the treble shelf stay
2.5 dB smaller for the same brightness. That matters because a large treble
boost is what makes sibilants spit.

The `bright` constants are calibrated against measurement, not derived; the
rest are provisional (above). A linear shelf's gain is voice-independent, so one
constant per preset is correct: the same
`+5 dB` shelf moves 13 male voices by +0.40…+0.67 dB/oct (mean +0.52,
sd 0.09). Per-voice preset tables were measured and rejected - closing the
2.6 dB/oct spread between the darkest and brightest voice would take roughly
32 dB of treble, which is a repair, not a preset. Per-voice correction belongs
in the master's de-harshing, if it is added at all.

Pitch uses `rubberband` with formant preservation. If ffmpeg lacks it, the chain
falls back to `asetrate` + `atempo`, which shifts formants and is audible.
Measured delivery is within ±0.26 st of the request in both directions.

### Listen to the presets

Seven voices, each reading the same English script: a neutral label naming the
emotion, a one-second pause, then the same two sentences with that emotion
applied. Five are English voices; the last two are non-English voices reading
English. Every file is mastered once at the default −16 LUFS target. The blend
weights two of the English voices 2:1.

The samples are illustrative, not a quality claim; see the calibration status
above. GitHub strips `<audio>` from Markdown, so the players live on the
[sample page](samples/index.md): one row per voice, each with an inline player
and the original MP3. The script is quoted
[there too](samples/index.md#script).

### How a preset lands

A preset is applied to that voice's audio, before the whole-file tone chain. It
is a delta on top of two things that are already there, so the same preset is
not the same sound everywhere:

- **The voice's own timbre.** `am_onyx` and `bm_fable` are 2.6 dB/oct apart
  before any preset runs, and `+5 dB @ 6 kHz` adds +0.40 to one and +0.66 to the
  other. The preset moves each voice by a similar amount; it does not equalise
  them.
- **The tone chain.** The chain's shelves stack with the preset's, because the
  preset runs first. `warm`'s `+1 dB @ 250` sits under the chain's `+2 dB @ 250`
  for +3 dB of bass in total, and the chain's `−1.5 dB @ 6 kHz` takes back part
  of `bright`'s `+5`. That tax is why `bright` needs a shelf that large.

`[rate:]` is a multiplier on the preset rate, not a replacement, and it is
sticky: `[rate:0.9]` set once slows every later speaker until `[rate:1.0]`.

### `instructions` as the file-wide default

Request-level `instructions` select one preset for every turn that does not name
its own. `[emotion:x]` overrides that choice for the turn, and because the tag is
sticky it keeps applying until changed. `[emotion:neutral]` removes the override,
so the request default applies again - which is how a file keeps a baseline tone
and punctuates it:

```json
{
  "instructions": "warm",
  "input": "The rain had stopped by morning.\n[emotion:excited] \"It's cleared!\" she said.\n[emotion:neutral] He looked at the sky again."
}
```

The three turns are `warm`, `excited`, `warm`. There is no blending *within* a
turn: a turn's `[emotion:]` replaces the request-level choice rather than
combining with it. `[voice:]` resets neither the emotion nor the rate, so a tag
set for one speaker follows the next until it is changed.

## Tone chain and mastering

Applied once, to the whole file:

```
low-shelf  +2.0 dB @ 250 Hz
high-shelf −1.5 dB @ 6 kHz
acompressor ratio 2.5, attack 15 ms, release 200 ms
tanh saturation at low drive
single linear gain → −16 LUFS (podcast default; `LOUDNESS_LUFS`)
alimiter −1 dBFS sample peak (≈ −0.5 dBTP)
```

Loudness is measured *after* the tone chain and corrected with one linear
gain. It is never normalised per segment: that would boost a whispered line
back to normal and erase the performance.

The consequence is that the master cannot equalise **voices**: it makes the file
correct and leaves the casting bias intact. Kokoro's raw output spans 9.2 dB
across the English voices alone, so a multi-speaker script keeps its quiet and
loud speakers. That is a separate, static correction - measured per voice in
[`voice-loudness.md`](voice-loudness.md).

## Voice levelling

Before the master, every turn gets a **static casting gain** for its voice: a
constant dB offset to a common −24 LUFS pre-master level, shipped for all 72
measured voices in [`../src/kokoro_tone/levelling.py`](../src/kokoro_tone/levelling.py).
The gain is passed to the engine as `volume_multiplier`, so it applies in the
float domain and reaches the passthrough path too. Blends interpolate their
members' gains by weight; an unmeasured voice stays at unity.

It is not per-segment `loudnorm`: the gain is fixed per voice, so a whisper turn
stays quieter than the same voice speaking normally. `VOICE_LEVELLING=false`
disables it for comparison. Levelling is applied **before** the per-turn emotion
preset, so a preset's relative gain survives; see
[ADR-0013](adr/0013-level-voice-before-emotion.md).

## Passthrough, rendering and gain

Three paths, and they do not gain the same way:

| Request | Gain |
|---|---|
| **Passthrough + `stream: true`** (plain string, no tags, no `instructions`) | casting gain only - the engine's bytes are piped through unmastered |
| **Passthrough + `stream_format: "sse"`** | casting gain only - the engine's chunks are wrapped as deltas |
| **Rendered + `stream_format: "sse"`** (tags or `instructions`) | casting gain, full chain, buffered; the first delta arrives after mastering |
| **Rendered + `stream: true`** | casting gain, full chain; `stream` is ignored and the body is a complete file |

Preset gain is relative (whisper is −8 dB before the master), so it survives the
master's single absolute gain. The limiter is last, so a hot preset cannot push
past −1 dBFS sample peak, which measures around −0.5 dBTP.

The honest limit: **mastering and incremental streaming cannot both happen.**
Real-time gain would mean measuring per chunk, which is the per-segment
normalisation the design forbids. If a client needs mastering, it gets the file
after rendering; if it needs bytes as they are produced, it gets the engine's
unmastered output.

