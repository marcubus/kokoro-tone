# kokoro-tone emotion samples

Each file uses the same script: a neutral label naming the emotion, a one-second
pause, then the same two sentences with that emotion applied. Every file is
mastered once at the default −16 LUFS target.

The samples are illustrative, not a quality claim. Every preset except `neutral`
is experimental; see the [to-do](../../README.md#to-do).

The French and Japanese voices read the same English script, so their
pronunciation carries an accent.

GitHub strips `<audio>` and `<video>` tags from Markdown, so a bare `.mp3` link
cannot play inline. Each player below is the same audio in a small MP4 (a
waveform over the audio track), uploaded as a GitHub user attachment. The
`mp3` link is the gateway's original output, unchanged.

| Voice | Character | Player | Download |
|---|---|---|---|
| `af_heart` | US English, female | https://github.com/user-attachments/assets/35ea6f92-1686-47cb-a89d-e80cd48f3b27 | [mp3](af_heart.mp3) |
| `af_bella` | US English, female, brighter timbre | https://github.com/user-attachments/assets/1eb75903-b6fc-4d43-8621-b50c37061a1e | [mp3](af_bella.mp3) |
| `am_michael` | US English, male | https://github.com/user-attachments/assets/06e9f1e9-b7a9-4825-b058-fa76b3c0cbde | [mp3](am_michael.mp3) |
| `bm_george` | British English, male | https://github.com/user-attachments/assets/aaf03172-e3eb-4ccd-8613-03720ae6a5b4 | [mp3](bm_george.mp3) |
| `bm_lewis` | British English, male, deeper timbre | https://github.com/user-attachments/assets/2caef2b5-c831-4369-a551-64c68b68203e | [mp3](bm_lewis.mp3) |
| `ff_siwis` | French voice, reading English | https://github.com/user-attachments/assets/33a5d5fe-3d67-4b7b-ae64-10378e83e433 | [mp3](ff_siwis.mp3) |
| `jm_kumo` | Japanese voice, reading English | https://github.com/user-attachments/assets/42cb4e41-4f4e-4245-8c22-567ae7b193ef | [mp3](jm_kumo.mp3) |
| `am_michael(2)+bm_george(1)` | Blend, weighted 2:1 | https://github.com/user-attachments/assets/584ca0c9-31f9-464d-8306-810462fc4e85 | [mp3](blend-am_michael-bm_george.mp3) |

## Script

```text
[emotion:neutral] Neutral. [pause:1s] [emotion:neutral] The morning light came through the window. It settled on the table.
[emotion:neutral] Warm. [pause:1s] [emotion:warm] The morning light came through the window. It settled on the table.
[emotion:neutral] Bright. [pause:1s] [emotion:bright] The morning light came through the window. It settled on the table.
[emotion:neutral] Sad. [pause:1s] [emotion:sad] The morning light came through the window. It settled on the table.
[emotion:neutral] Tired. [pause:1s] [emotion:tired] The morning light came through the window. It settled on the table.
[emotion:neutral] Angry. [pause:1s] [emotion:angry] The morning light came through the window. It settled on the table.
[emotion:neutral] Whisper. [pause:1s] [emotion:whisper] The morning light came through the window. It settled on the table.
[emotion:neutral] Excited. [pause:1s] [emotion:excited] The morning light came through the window. It settled on the table.
```

The label is always `[emotion:neutral]`; only the sentence after the pause
carries the emotion.

## How the players are made

Every file was rendered by the live `kokoro-tone` container, not on the host:

```bash
docker exec -i kokoro-tone python scripts/narrate.py - \
  --out - --url http://127.0.0.1:8000 --voice af_heart --format mp3
```

Each MP3 was then wrapped in an MP4 with its own waveform:

```bash
ffmpeg -i voice.mp3 \
  -filter_complex "[0:a]showwaves=s=960x240:mode=cline:rate=10:colors=0x58a6ff,format=yuv420p[v]" \
  -map "[v]" -map 0:a -c:v libx264 -preset veryfast -crf 32 \
  -c:a aac -b:a 96k -movflags +faststart -shortest voice.mp4
```

The MP4s are not committed. They are uploaded as user attachments, because
that is the only form GitHub renders as a player in Markdown.
