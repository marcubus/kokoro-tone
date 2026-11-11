# AGENTS.md

Guidance for coding agents working in this repository.

## What this is

An OpenAI-compatible speech gateway in front of Kokoro-FastAPI. The model is
Kokoro-82M and cannot take direction, so "emotion" here is measurable prosody -
rate, pitch, gain and spectral tilt - applied with ffmpeg, then mastered once
per file. Nothing in this repo claims to add acting.

The wire format is the contract. `instructions`, `[emotion:…]` and the turn
array are OpenAI-shaped inputs that this gateway translates into DSP.

## Ground rules

- **Filter strings are built from constants only.** `audio._filters` refuses
  anything outside `_FILTER_SAFE`. Never interpolate client text into a filter;
  a new filter must be a constant or a formatted float.
- **Presets are pinned by tests.** `tests/test_audio.py` holds `ADR_PRESETS`
  (rate, pitch, gain) and `ADR_FILTERS` (the exact filter tuples). Changing a
  preset means updating those, `docs/audio.md`, ADR-0009 and the changelog in
  the same commit.
- **No per-segment normalisation.** Loudness is one linear gain for the whole
  file, measured after the tone chain. Per-segment `loudnorm` would boost a
  whispered turn back to normal and erase the performance.
- **Pipeline order is fixed:** static voice levelling → per-turn prosody →
  tone chain → single gain → limiter. Levelling first is ADR-0013; swapping it
  makes levelling cancel the preset gain.
- **Tags are independent slots.** `[voice:]`, `[emotion:]` and `[rate:]` do not
  reset each other, and `[emotion:neutral]` clears only the emotion. The turn
  array has no sticky state at all.
- **`instructions` are not parsed semantically.** Word-boundary keyword match,
  longest keyword wins, no negation: "don't whisper" selects `whisper`. Do not
  describe them as understanding the request.

## Commands

```bash
make test      # pytest
make check     # ruff format --check, ruff lint, security-check, pin-check
make fmt       # rewrite formatting
make narrate FILE=script.txt OUT=out.mp3
make analyse FILE=out.mp3
```

`ffmpeg` must be on `PATH`; the audio tests shell out to it. `make check` is not
optional - CI runs it, and `security-check` fails on `eval`, `pickle`,
`shell=True` and similar in `src/`.

## Where the contract lives

| File | Owns |
|---|---|
| `README.md` | what differs from OpenAI, each row pointing at its test |
| `docs/usage.md` | request shapes, markup, tag interactions |
| `docs/audio.md` | presets, tone chain, how a preset lands, passthrough vs rendered |
| `docs/configuration.md` | environment variables and defaults |
| `docs/voice-loudness.md` | the per-voice measurements behind `levelling.py` |
| `docs/adr/` | decisions and their rejected alternatives |
| `CHANGELOG.md` | user-visible changes; CalVer `YYYY.MM.MICRO`, tag `v…` |

Version lives in exactly one place, `src/kokoro_tone/__init__.py`. The engine
tag lives only in `engine.version`; `make pin-check` enforces both.

## Measuring audio

These were learned the hard way. Following them avoids re-deriving them.

- **Tilt is the reliable brightness metric**: slope in dB/oct across the 250 Hz
  to 8 kHz octave bands. The `aspectralstats` centroid is frame-alignment
  sensitive - two files with identical octave bands to 0.1 dB can differ 9% -
  so never rank voices or presets by centroid.
- **Compare like with like.** Measure a preset against neutral for the *same
  voice and prose*, after the master. A preset is a delta on the voice's own
  timbre, not an absolute target.
- **A plain string with no tags and no `instructions` is passthrough** and comes
  back unmastered. To get a mastered neutral baseline, send
  `[emotion:neutral] <prose>`.
- **Shelf gain is asymptotic.** `treble=g=5:f=6000` arrives as +2.4 dB in the
  8 kHz octave band, not +5. Roughly 0.07 dB/oct of tilt per dB of shelf.
- **Tilt moves from both ends.** A low-mid cut is about a third of the effect
  and lets the treble shelf stay smaller, which is what keeps sibilants in
  check.
- **Pitch: measure with a whole-file LTAS cepstrum**, not a per-frame median -
  the per-frame estimator is biased low and once produced a phantom 0.5 st
  shortfall. `rubberband` is exact on a pure tone.

## Settled questions - do not reopen without new measurements

- **Pitch delivery is correct** (within ±0.26 st, no directional bias). The
  reported shortfall was an estimator artefact; there is no pitch bug to fix.
- **No per-voice preset table.** The same +5 dB shelf moves 13 male voices by
  +0.40…+0.67 dB/oct. Equalising them would take ~32 dB of treble, which is a
  repair, not a preset. Per-voice correction, if ever wanted, belongs in the
  master's de-harshing and must stay bounded.
- **A voice's spectral character is not a defect.** Do not EQ a dark voice
  bright in the master; `docs/voice-loudness.md` §4 argues this at length.
- **`sad`, `tired`, `angry` and `whisper` are not broken.** They use filters,
  which the tone chain does not cancel, and already deliver their intent.

## Deployment

This repo is the source; a separate deployment checkout owns the compose file,
the environment and the start script. Deploy from there:

```bash
./start.sh        # stop, pull the engine, up --build
```

Then check `/health` on the gateway: it reports `version` and the enabled
features. A stale image reports an older `version` and omits newer blocks, so
`/health` is the fastest way to tell whether the build you just started is the
one you meant to.
