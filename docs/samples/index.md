# kokoro-tone emotion samples

Each file uses the same script: the tone name, a three-second pause, then the
same two sentences. Every file is mastered once at the default −16 LUFS target.

**Script:** `Neutral. [3 s] The morning light came through the window. It settled on the table.`
repeated for neutral, warm, bright, sad, tired, angry, whisper and excited.

The French and Japanese voices read the same English script, so their
pronunciation carries an accent.

## af_heart

US English, female.

<audio controls preload="none" src="af_heart.mp3"></audio>

## af_bella

US English, female, brighter timbre.

<audio controls preload="none" src="af_bella.mp3"></audio>

## am_michael

US English, male.

<audio controls preload="none" src="am_michael.mp3"></audio>

## bm_george

British English, male.

<audio controls preload="none" src="bm_george.mp3"></audio>

## bm_lewis

British English, male, deeper timbre.

<audio controls preload="none" src="bm_lewis.mp3"></audio>

## ff_siwis

French voice, reading the English script.

<audio controls preload="none" src="ff_siwis.mp3"></audio>

## jm_kumo

Japanese voice, reading the English script.

<audio controls preload="none" src="jm_kumo.mp3"></audio>

## Blend: am_michael(2) + bm_george(1)

Two of the voices above, weighted 2:1.

<audio controls preload="none" src="blend-am_michael-bm_george.mp3"></audio>

The source script is [`emotions.txt`](emotions.txt). Rendering used
`scripts/narrate.py` against a running gateway.
