# kokoro-tone emotion samples

Each file uses the same script: the tone name, a three-second pause, then the
same two sentences. Every file is mastered once at the default −16 LUFS target.

The French and Japanese voices read the same English script, so their
pronunciation carries an accent.

| Voice | Character | Player |
|---|---|---|
| `af_heart` | US English, female | <audio controls preload="none" src="af_heart.mp3"></audio> [mp3](af_heart.mp3) |
| `af_bella` | US English, female, brighter timbre | <audio controls preload="none" src="af_bella.mp3"></audio> [mp3](af_bella.mp3) |
| `am_michael` | US English, male | <audio controls preload="none" src="am_michael.mp3"></audio> [mp3](am_michael.mp3) |
| `bm_george` | British English, male | <audio controls preload="none" src="bm_george.mp3"></audio> [mp3](bm_george.mp3) |
| `bm_lewis` | British English, male, deeper timbre | <audio controls preload="none" src="bm_lewis.mp3"></audio> [mp3](bm_lewis.mp3) |
| `ff_siwis` | French voice, reading English | <audio controls preload="none" src="ff_siwis.mp3"></audio> [mp3](ff_siwis.mp3) |
| `jm_kumo` | Japanese voice, reading English | <audio controls preload="none" src="jm_kumo.mp3"></audio> [mp3](jm_kumo.mp3) |
| `am_michael(2)+bm_george(1)` | Blend, weighted 2:1 | <audio controls preload="none" src="blend-am_michael-bm_george.mp3"></audio> [mp3](blend-am_michael-bm_george.mp3) |

## Script

```text
[emotion:neutral] Neutral. [pause:3s] The morning light came through the window. It settled on the table.
[emotion:warm] Warm. [pause:3s] The morning light came through the window. It settled on the table.
[emotion:bright] Bright. [pause:3s] The morning light came through the window. It settled on the table.
[emotion:sad] Sad. [pause:3s] The morning light came through the window. It settled on the table.
[emotion:tired] Tired. [pause:3s] The morning light came through the window. It settled on the table.
[emotion:angry] Angry. [pause:3s] The morning light came through the window. It settled on the table.
[emotion:whisper] Whisper. [pause:3s] The morning light came through the window. It settled on the table.
[emotion:excited] Excited. [pause:3s] The morning light came through the window. It settled on the table.
```

Rendering used `scripts/narrate.py` against a running gateway.
