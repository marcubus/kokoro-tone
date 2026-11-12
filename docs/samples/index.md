# kokoro-tone emotion samples

Each file uses the same script: the tone name, a three-second pause, then the
same two sentences. Every file is mastered once at the default −16 LUFS target.

The French and Japanese voices read the same English script, so their
pronunciation carries an accent.

GitHub strips `<audio>` and `<video>` tags from Markdown, so a bare `.mp3` link
cannot play inline. Each player below is the same audio in a small MP4 (a
waveform over the audio track), uploaded as a GitHub user attachment. The
`mp3` link is the gateway's original output, unchanged.

| Voice | Character | Player | Download |
|---|---|---|---|
| `af_heart` | US English, female | https://github.com/user-attachments/assets/63ecfb4b-bd5f-4854-b6b2-be697526cab7 | [mp3](af_heart.mp3) |
| `af_bella` | US English, female, brighter timbre | https://github.com/user-attachments/assets/56c0cc9c-16c5-48c1-b5f1-9c38a1087e2e | [mp3](af_bella.mp3) |
| `am_michael` | US English, male | https://github.com/user-attachments/assets/2358e4a5-7890-4bf4-95e4-9da48d37edb4 | [mp3](am_michael.mp3) |
| `bm_george` | British English, male | https://github.com/user-attachments/assets/bf5476bd-8234-4944-8857-b917e526fa64 | [mp3](bm_george.mp3) |
| `bm_lewis` | British English, male, deeper timbre | https://github.com/user-attachments/assets/fead0a6b-3c94-4db4-a3ca-bd0d0ec3112a | [mp3](bm_lewis.mp3) |
| `ff_siwis` | French voice, reading English | https://github.com/user-attachments/assets/ad0fee2a-1cf8-41b9-9d76-ee5d725e0ce5 | [mp3](ff_siwis.mp3) |
| `jm_kumo` | Japanese voice, reading English | https://github.com/user-attachments/assets/a2951f69-4e19-494c-982e-520c44e2930a | [mp3](jm_kumo.mp3) |
| `am_michael(2)+bm_george(1)` | Blend, weighted 2:1 | https://github.com/user-attachments/assets/63608341-0509-4f45-895b-5446d984867e | [mp3](blend-am_michael-bm_george.mp3) |

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

## How the players are made

Rendering used `scripts/narrate.py` against a running gateway. Each MP3 was
then wrapped in an MP4 with its own waveform:

```bash
ffmpeg -i voice.mp3 \
  -filter_complex "[0:a]showwaves=s=960x240:mode=cline:rate=10:colors=0x58a6ff,format=yuv420p[v]" \
  -map "[v]" -map 0:a -c:v libx264 -preset veryfast -crf 32 \
  -c:a aac -b:a 96k -movflags +faststart -shortest voice.mp4
```

The MP4s are not committed. They are uploaded as user attachments, because
that is the only form GitHub renders as a player in Markdown.
