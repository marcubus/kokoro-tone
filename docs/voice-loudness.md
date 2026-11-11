# Voice loudness

Why the same request produces quiet speakers and loud speakers, measured per
voice, and the static per-voice gain that fixes it.

**Short answer:** Kokoro does no loudness normalisation. Raw output across the 28
English voices spans **9.2 dB** (−30.7 LUFS for `af_nova` to −21.5 LUFS for
`am_adam`); across all 72 engine voices it spans **16.0 dB**. The gateway's
−16 LUFS master is a single linear gain over the whole file (ADR-0009), so it can
only hit the average - it cannot equalise two speakers 9 dB apart. The fix is a
**static per-voice casting gain**, applied to all 72 measured voices (with blends
interpolated), not per-segment `loudnorm`.

Related: [`adr/0009-openai-compatible-kokoro-gateway.md`](adr/0009-openai-compatible-kokoro-gateway.md)
(the tone chain and why per-segment loudness is banned), [`audio.md`](audio.md)
(mastering and the passthrough/rendered gain paths).

---

## 1. The finding

Measured with ITU-R BS.1770 integrated loudness (`ffmpeg loudnorm`) over a common
~40 s paragraph, all voices at `speed: 1.0`, raw engine output, no mastering.

| Group | n | quietest | loudest | spread |
|---|---|---|---|---|
| **English v1.0 (af/am/bf/bm)** | 33 | −30.7 (`af_nova`) | −21.4 (`bf_isabella`) | **9.3 dB** |
| en-US | 24 | −30.7 | −21.5 | 9.2 dB |
| en-GB | 9 | −27.6 | −21.4 | 6.2 dB |
| es | 3 | −26.8 | −25.8 | 0.9 dB |
| ja | 5 | −23.9 | −23.0 | 0.9 dB |
| pt-BR | 3 | −26.9 | −26.0 | 0.9 dB |
| hi | 4 | −22.9 | −20.1 | 2.8 dB |
| it | 2 | −23.7 | −18.4 | 5.3 dB |
| zh | 8 | −27.3 | −20.6 | 6.7 dB |
| legacy `*_v0` aliases | 13 | −34.4 | −22.7 | 11.7 dB |
| **all voices** | 72 | −34.4 | −18.4 | **16.0 dB** |

The English median of the 28-voice sample is **−25.2 LUFS**. A common deployment
default, `bm_lewis`, sits at −26.8 - 1.6 dB below median, so a default-voice
render is already on the quiet side before any mastering.

## 2. Why it happens

Three separate mechanisms, and it matters which one a given file hit:

1. **The engine does not normalise.** Kokoro-FastAPI's `AudioNormalizer.normalize()`
   is `np.clip(audio * 32767)` - a float→int16 scale, not a loudness pass. Its
   `DEFAULT_VOLUME_MULTIPLIER` is a raw linear gain (default `1.0`), and the
   per-request `volume_multiplier` is the same. There is no LUFS target anywhere
   upstream.
2. **The master is one gain for the whole file.** ADR-0009 is explicit: loudness
   must not be normalised per segment, because that would boost a
   `[emotion:whisper]` line back to normal and erase the performance. So the
   gateway measures the finished file and applies a single linear gain to −16 LUFS.
   That makes the *file* correct and leaves the *casting* bias intact - a speaker
   9 dB below another stays 9 dB below.
3. **Passthrough is unmastered entirely.** A request with no tags and no
   `instructions` is forwarded whole to preserve streaming. That path never
   reaches the tone chain, so it gets neither the master nor the limiter. This
   is the "wasn't normalised at all" case.

Mechanism 2 is the one that produces "quiet speakers and loud speakers" in a
tagged multi-speaker file; mechanism 3 is the one that produces it in a plain
single-voice file.

## 3. English v1.0 voices, measured

`gain` is the dB needed to bring each voice to a common **−24 LUFS pre-master**
target. The median is −25.2, so this barely touches the middle of the set and
moves only the outliers - the service's overall character does not shift. The
five extra English voices measured on this deployment are in
[Appendix A](#appendix-a-all-72-voices-measured).

| voice | I (LUFS) | TP (dBTP) | LRA (LU) | gain | centroid (Hz) | crest (dB) |
|---|---|---|---|---|---|---|
| `af_nova` | −30.7 | −10.3 | 4.9 | **+6.7** | 2248 | 10.0 |
| `af_jadzia` | −29.4 | −10.3 | 4.9 | **+5.4** | 2349 | 8.3 |
| `am_onyx` | −27.9 | −4.5 | 5.9 | +3.9 ⚠ | 1546 | 13.6 |
| `af_sky` | −27.9 | −7.7 | 4.0 | +3.9 | 2312 | 9.7 |
| `bm_fable` | −27.6 | −8.7 | 3.8 | +3.6 | 2262 | 9.1 |
| `am_michael` | −27.5 | −4.4 | 3.9 | +3.5 ⚠ | 2539 | 14.2 |
| `af_alloy` | −26.8 | −5.5 | 4.5 | +2.8 | 2104 | 11.2 |
| `bm_lewis` | −26.8 | −4.8 | 4.4 | +2.8 | 2342 | 12.4 |
| `am_echo` | −26.6 | −7.5 | 3.5 | +2.6 | 2163 | 8.7 |
| `am_santa` | −25.9 | −5.7 | 2.6 | +1.9 | 2340 | 10.1 |
| `af_heart` | −25.6 | −6.5 | 3.1 | +1.6 | 2732 | 8.8 |
| `am_eric` | −25.4 | −8.8 | 2.4 | +1.4 | 2083 | 6.3 |
| `af_jessica` | −25.3 | −5.6 | 2.0 | +1.3 | 2592 | 9.6 |
| `af_river` | −25.2 | −4.5 | 2.5 | +1.2 | 2653 | 11.1 |
| `bm_george` | −25.2 | −8.8 | 1.9 | +1.2 | 2577 | 6.1 |
| `am_liam` | −25.1 | −5.4 | 1.6 | +1.1 | 2513 | 9.7 |
| `bf_lily` | −25.0 | −6.5 | 2.9 | +1.0 | 2658 | 7.9 |
| `af_nicole` | −25.0 | −5.9 | 4.1 | +1.0 | 3207 | 9.7 |
| `bm_daniel` | −24.7 | −6.9 | 1.6 | +0.7 | 2405 | 7.3 |
| `af_bella` | −24.5 | −4.5 | 4.9 | +0.5 | 3014 | 9.6 |
| `bf_alice` | −24.2 | −5.2 | 1.5 | +0.2 | 2661 | 8.8 |
| `af_sarah` | −24.1 | −3.0 | 1.3 | +0.1 | 2989 | 10.8 |
| `bf_emma` | −23.9 | −4.7 | 3.0 | −0.1 | 3265 | 9.5 |
| `am_puck` | −22.9 | −3.5 | 5.0 | −1.1 | 2329 | 8.8 |
| `am_fenrir` | −22.4 | −1.6 | 4.0 | −1.6 | 2592 | 11.0 |
| `af_kore` | −22.1 | −4.7 | 4.8 | −1.9 | 2514 | 6.9 |
| `af_aoede` | −21.7 | −2.5 | 4.8 | −2.3 | 2333 | 8.5 |
| `am_adam` | −21.5 | −2.7 | 1.9 | −2.5 | 2437 | 8.8 |

⚠ = the boosted true peak lands above −1 dBTP, so the master limiter shaves
<0.5 dB. Inaudible, and exactly what the limiter is for. On the passthrough path
there is no limiter, but −0.6 dBTP still does not clip.

The full 72-voice table, including the non-English voices and the `*_v0` aliases,
is in [Appendix A](#appendix-a-all-72-voices-measured).

## 4. Clarity

Level is most of it: at −30 LUFS a voice reads as dull and distant, at −24 it
reads as present. Beyond level, two measurable timbre gaps:

- **Spectral centroid spans 1.5–3.3 kHz** - a 2.1× range. `am_onyx` (1546 Hz) and
  `am_eric` (2083 Hz) are genuinely dark; `bf_emma` (3265), `af_nicole` (3207) and
  `af_bella` (3014) are bright. That is the voice's character, not a defect - do
  not EQ it away. Treat the ordering as approximate: the centroid is sensitive to
  frame alignment, and two files with identical octave bands to 0.1 dB can differ
  by 9%. Where the exact figure matters, use the spectral tilt (dB/oct across the
  250 Hz–8 kHz octave bands) instead. Measured on 13 male voices, `bm_george` has
  the highest centroid (2404 Hz) but only −2.98 dB/oct of tilt - it reads bright
  mostly because of its pitch, not its spectrum.
- **Crest factor spans 6–14 dB.** `am_michael` (14.2) and `am_onyx` (13.6) are
  *quiet but spiky*: low RMS, high peaks. Gain alone makes them hot without making
  them sound louder. Those two are the only voices that want a touch of
  compression (~2:1, 3–4 dB gain reduction) rather than pure gain; `bm_lewis`
  (12.4) and `af_alloy` (11.2) are borderline.

## 5. The fix

A **static per-voice casting gain**, applied per turn before the tone chain,
targeting a common −24 LUFS pre-master level. The shipped table is
[`../src/kokoro_tone/levelling.py`](../src/kokoro_tone/levelling.py)
(`VOICE_GAIN_DB`, all 72 measured voices); the gateway passes it to the engine as
`volume_multiplier = 10 ** (dB / 20)`.

```python
from kokoro_tone import levelling

levelling.gain_db("af_nova")                # +6.7
levelling.multiplier("af_nova")             # 2.1627
levelling.gain_db("af_bella(2)+af_sky(1)")  # +1.6333
```

Why this shape, and why it does not violate ADR-0009:

1. **It is not per-segment loudnorm.** The gain is constant for a voice across the
   whole file, so a `[emotion:whisper]` line stays quiet. Only the *casting* bias
   is removed - which is what the ADR actually wants.
2. **It applies on the passthrough path too.** A constant gain is just a volume
   filter, so streaming is preserved. This fixes mechanism 3 with no architectural
   change.
3. **The engine already accepts it.** `volume_multiplier` is a per-request field
   (0–10), applied in the float domain before the int16 conversion, so the gateway
   can pass it per turn without touching PCM or reimplementing the chain.
4. **Blends interpolate.** For `af_bella(2)+af_sky(1)`, weight the member gains:
   (2 × 0.5 + 1 × 3.9) / 3 ≈ **+1.6 dB**. Subtractive blends use the same
   absolute-weight normalisation. OpenAI voice names resolve to the measured
   Kokoro voice (`alloy` → `am_adam`); an unmeasured voice stays at unity rather
   than guessing.

Target choice matters: **−24 LUFS pre-master** keeps most voices within ±2 dB and
moves only the outliers. The master re-normalises the file to `LOUDNESS_LUFS`, so
this pre-master target does not need to move with it.

Set `VOICE_LEVELLING=false` to hear the raw casting bias; `GET /health` reports
whether levelling is on and how many voices the table covers. The ordering
against emotion is recorded in
[ADR-0013](adr/0013-level-voice-before-emotion.md): levelling first, emotion
second.

## 6. What not to do

- **Peak normalisation to 0 dBFS** - a pattern used by several community
  generators. It equalises peaks, not loudness, so `am_michael` (crest 14 dB)
  stays perceptually quiet while `am_eric` (crest 6 dB) gets loud. Same mistake
  in a different direction.
- **A flat multiplier for all voices** (`DEFAULT_VOLUME_MULTIPLIER`) - it
  preserves the 9 dB spread, just louder.
- **Per-segment `loudnorm`** - banned by ADR-0009; it erases deliberate dynamics.

## 7. Prior art, and why it was not enough

The problem is well known; a usable table was not published.

- **remSky/Kokoro-FastAPI #110** - the closest thing: an anecdotal per-voice
  multiplier of base `1.6`, `af_sky` `2.5`, `af_bella` `2.4`. Measurement
  disagrees: `af_sky` really is quiet (+3.9 dB ≈ ×1.57, close to their
  2.5/1.6 ratio), but **`af_bella` is only 0.5 dB below median**, not "almost as
  low". Their numbers are peak-based and clip, not loudness-based.
- **remSky/Kokoro-FastAPI #409** - a user reporting `af_heart` as "super quiet";
  measured, it is 0.5 dB below median. Perception at low playback level, not the
  model.
- **hexgrad/kokoro #99** - the maintainer's answer to "can we change the volume"
  is "ask an LLM how to do this". No upstream plan to fix it.
- **audio.cpp** docs report a 7.23 dB per-window loudness spread inside a single
  utterance on RK3588 - a different (hardware windowing) problem, not this one.

## 8. Method and provenance

- **Source:** [`KingRabbiTV/Kokoro-82M-samples`](https://github.com/KingRabbiTV/Kokoro-82M-samples)
  - 67 MP3s generated by the same engine this project wraps
  (`POST http://localhost:8880/v1/audio/speech`, `speed: 1.0`,
  `response_format: mp3`), all from **identical text** (a ~530-character
  paragraph, ≈40 s), so integrated loudness is directly comparable. The
  generation script is committed in that repo (`main.py`).
- **Five extra voices:** `af_amelia_inno`, `af_goodall_inno`, `am_price_inno`,
  `bf_isabella` and `bm_atten_inno` are in the shipped engine but not in that
  sample set. They were measured on this deployment with the same method, anchored
  to `af_heart`; the anchor matched the sample set within 0.3 dB.
- **Loudness:** `ffmpeg -af loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json` →
  `input_i`, `input_tp`, `input_lra`.
- **Clarity:** `ffmpeg -af astats` → peak/RMS (crest factor);
  `ffmpeg -af aspectralstats=measure=centroid` → mean spectral centroid.
- **Caveat:** the samples come from a community repo on the same engine, not from
  this project's own instance. Relative differences between voices are a property
  of the style vectors and should not drift, but absolute levels may move a point
  or two with the engine tag. Verify with a calibration run (§9) before changing
  the shipped table in [`../src/kokoro_tone/levelling.py`](../src/kokoro_tone/levelling.py).

## 9. Reproducing

The measurement is one fixed sentence per voice:

```bash
for v in af_nova af_jadzia am_onyx af_sky bm_fable am_michael am_echo am_santa \
         af_heart am_eric af_jessica af_river bm_george am_liam bf_lily af_nicole \
         bm_daniel af_bella bf_alice af_sarah bf_emma am_puck am_fenrir af_kore \
         af_aoede am_adam af_alloy bm_lewis; do
  curl -s -X POST "$KOKORO_TONE_URL/v1/audio/speech" \
    -H "Authorization: Bearer $KOKORO_TONE_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"input\":\"$SENTENCE\",\"voice\":\"$v\",\"response_format\":\"wav\"}" -o "$v.wav"
  ffmpeg -hide_banner -nostats -i "$v.wav" \
    -af loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json -f null - 2>&1 |
    sed -n '/^{/,/^}/p' | jq -r '"\(.input_i) \(.input_tp)"' | sed "s/^/$v /"
done
```

Two notes when running it:

- Ask for **`wav`**, not `mp3` - MP3 encoding is lossy and the engine's chunking
  makes bitrate vary; WAV removes one variable.
- Send **untagged, uninstructed text**. Anything that trips the tone chain gets
  mastered to −16 LUFS and the per-voice difference is partly hidden by the master
  gain. The numbers above are pre-master by design, because that is the level the
  casting gain has to correct.

## Appendix A: all 72 voices measured

`gain` is to a −24 LUFS pre-master target. Centroid and crest were measured for
the original 28-voice sample only.

| voice | I (LUFS) | TP (dBTP) | LRA (LU) | gain | centroid (Hz) | crest (dB) |
|---|---|---|---|---|---|---|
| `af_alloy` | −26.8 | −5.5 | 4.5 | +2.8 | 2104 | 11.2 |
| `af_amelia_inno` | −23.2 | −5.8 | 2.6 | −0.5 | - | - |
| `af_aoede` | −21.7 | −2.5 | 4.8 | −2.3 | 2333 | 8.5 |
| `af_bella` | −24.5 | −4.5 | 4.9 | +0.5 | 3014 | 9.6 |
| `af_goodall_inno` | −24.7 | −5.8 | 3.4 | +1.0 | - | - |
| `af_heart` | −25.6 | −6.5 | 3.1 | +1.6 | 2732 | 8.8 |
| `af_jadzia` | −29.4 | −10.3 | 4.9 | +5.4 | 2349 | 8.3 |
| `af_jessica` | −25.3 | −5.6 | 2.0 | +1.3 | 2592 | 9.6 |
| `af_kore` | −22.1 | −4.7 | 4.8 | −1.9 | 2514 | 6.9 |
| `af_nicole` | −25.0 | −5.9 | 4.1 | +1.0 | 3207 | 9.7 |
| `af_nova` | −30.7 | −10.3 | 4.9 | +6.7 | 2248 | 10.0 |
| `af_river` | −25.2 | −4.5 | 2.5 | +1.2 | 2653 | 11.1 |
| `af_sarah` | −24.1 | −3.0 | 1.3 | +0.1 | 2989 | 10.8 |
| `af_sky` | −27.9 | −7.7 | 4.0 | +3.9 | 2312 | 9.7 |
| `af_v0` | −26.3 | −5.6 | 2.7 | +2.3 | - | - |
| `af_v0bella` | −26.5 | −5.6 | 4.2 | +2.5 | - | - |
| `af_v0irulan` | −29.1 | −9.0 | 2.8 | +5.1 | - | - |
| `af_v0nicole` | −26.2 | −6.7 | 4.6 | +2.2 | - | - |
| `af_v0sarah` | −25.6 | −4.1 | 1.1 | +1.6 | - | - |
| `af_v0sky` | −31.9 | −12.2 | 3.6 | +7.9 | - | - |
| `am_adam` | −21.5 | −2.7 | 1.9 | −2.5 | 2437 | 8.8 |
| `am_echo` | −26.6 | −7.5 | 3.5 | +2.6 | 2163 | 8.7 |
| `am_eric` | −25.4 | −8.8 | 2.4 | +1.4 | 2083 | 6.3 |
| `am_fenrir` | −22.4 | −1.6 | 4.0 | −1.6 | 2592 | 11.0 |
| `am_liam` | −25.1 | −5.4 | 1.6 | +1.1 | 2513 | 9.7 |
| `am_michael` | −27.5 | −4.4 | 3.9 | +3.5 ⚠ | 2539 | 14.2 |
| `am_onyx` | −27.9 | −4.5 | 5.9 | +3.9 ⚠ | 1546 | 13.6 |
| `am_price_inno` | −24.4 | −5.2 | 3.5 | +0.6 | - | - |
| `am_puck` | −22.9 | −3.5 | 5.0 | −1.1 | 2329 | 8.8 |
| `am_santa` | −25.9 | −5.7 | 2.6 | +1.9 | 2340 | 10.1 |
| `am_v0adam` | −22.7 | −2.7 | 2.4 | −1.3 | - | - |
| `am_v0gurney` | −34.4 | −10.3 | 4.1 | +10.4 ⚠ | - | - |
| `am_v0michael` | −30.3 | −6.9 | 3.6 | +6.3 ⚠ | - | - |
| `bf_alice` | −24.2 | −5.2 | 1.5 | +0.2 | 2661 | 8.8 |
| `bf_emma` | −23.9 | −4.7 | 3.0 | −0.1 | 3265 | 9.5 |
| `bf_isabella` | −21.4 | −3.9 | 2.8 | −2.3 | - | - |
| `bf_lily` | −25.0 | −6.5 | 2.9 | +1.0 | 2658 | 7.9 |
| `bf_v0emma` | −25.3 | −5.2 | 2.7 | +1.3 | - | - |
| `bf_v0isabella` | −23.9 | −5.5 | 3.6 | −0.1 | - | - |
| `bm_atten_inno` | −24.0 | −7.2 | 2.5 | +0.3 | - | - |
| `bm_daniel` | −24.7 | −6.9 | 1.6 | +0.7 | 2405 | 7.3 |
| `bm_fable` | −27.6 | −8.7 | 3.8 | +3.6 | 2262 | 9.1 |
| `bm_george` | −25.2 | −8.8 | 1.9 | +1.2 | 2577 | 6.1 |
| `bm_lewis` | −26.8 | −4.8 | 4.4 | +2.8 | 2342 | 12.4 |
| `bm_v0george` | −29.2 | −11.8 | 2.6 | +5.2 | - | - |
| `bm_v0lewis` | −28.9 | −5.1 | 6.5 | +4.9 ⚠ | - | - |
| `ef_dora` | −26.8 | −6.2 | 3.0 | +2.8 | - | - |
| `em_alex` | −26.1 | −7.0 | 2.3 | +2.1 | - | - |
| `em_santa` | −25.8 | −4.7 | 3.1 | +1.8 | - | - |
| `ff_siwis` | −23.8 | −4.1 | 2.6 | −0.2 | - | - |
| `hf_alpha` | −20.1 | −4.4 | 2.7 | −3.9 | - | - |
| `hf_beta` | −21.7 | −2.2 | 1.8 | −2.3 | - | - |
| `hm_omega` | −21.0 | −3.4 | 2.2 | −3.0 | - | - |
| `hm_psi` | −22.9 | −4.7 | 2.5 | −1.1 | - | - |
| `if_sara` | −18.4 | −0.3 | 3.1 | −5.6 | - | - |
| `im_nicola` | −23.7 | −5.8 | 2.7 | −0.3 | - | - |
| `jf_alpha` | −23.0 | −6.8 | 3.0 | −1.0 | - | - |
| `jf_gongitsune` | −23.9 | −6.4 | 5.0 | −0.1 | - | - |
| `jf_nezumi` | −23.0 | −3.6 | 3.1 | −1.0 | - | - |
| `jf_tebukuro` | −23.8 | −4.5 | 2.8 | −0.2 | - | - |
| `jm_kumo` | −23.6 | −1.9 | 3.5 | −0.4 | - | - |
| `pf_dora` | −26.9 | −5.0 | 3.7 | +2.9 | - | - |
| `pm_alex` | −26.4 | −7.2 | 2.5 | +2.4 | - | - |
| `pm_santa` | −26.0 | −4.7 | 2.7 | +2.0 | - | - |
| `zf_xiaobei` | −23.7 | −3.8 | 4.9 | −0.3 | - | - |
| `zf_xiaoni` | −22.2 | −4.3 | 2.2 | −1.8 | - | - |
| `zf_xiaoxiao` | −20.6 | −3.7 | 2.6 | −3.4 | - | - |
| `zf_xiaoyi` | −23.2 | −6.7 | 1.9 | −0.8 | - | - |
| `zm_yunjian` | −24.6 | −5.7 | 4.3 | +0.6 | - | - |
| `zm_yunxi` | −26.1 | −3.9 | 7.4 | +2.1 | - | - |
| `zm_yunxia` | −23.9 | −5.1 | 4.8 | −0.1 | - | - |
| `zm_yunyang` | −27.3 | −2.4 | 6.5 | +3.3 ⚠ | - | - |
