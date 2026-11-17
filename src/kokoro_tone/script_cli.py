"""Command-line access to the script layer, with no server and no engine.

    python -m kokoro_tone.script_cli script.txt
    python -m kokoro_tone.script_cli --normalise --ambiguity ignore < script.txt
    python -m kokoro_tone.script_cli --request request.json --env-file .env

The output is the text the engine would be asked to say, plus any heteronym the
rules had to guess. That is the useful part of the gateway on its own: a script
compiler you can run in a pipeline, a test, or an editor hook.

`--request` compiles a whole speech request (input, voice, pronunciations,
normalise, ambiguity) exactly as `POST /v1/audio/speech` and
`POST /v1/script/compile` do, so a pre-flight and a render cannot disagree.

`--env-file` reads the deployment's `.env`. The limits and the lexicon paths are
deployment configuration, not code defaults: the code default of 4096 input
characters rejects a request the deployed gateway accepts at 60000.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace

from . import __version__, filters, lexicon, request, script
from .errors import AmbiguityError, RequestError
from .settings import Settings, load_settings

# Only the deployment keys this tool needs. The same file holds secrets, and
# nothing here should widen the process environment beyond pronunciation.
ENV_PREFIXES = ("MAX_", "PRONUNCIATION_", "NORMALISE_", "LOCALE")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kokoro-tone-script",
        description="Resolve a script into the spoken text the engine would receive.",
    )
    parser.add_argument("file", nargs="?", help="script file; read stdin when omitted")
    parser.add_argument(
        "--request",
        metavar="FILE",
        help="compile a full speech request JSON (input, voice, pronunciations, "
        "normalise, ambiguity) instead of a bare script",
    )
    parser.add_argument("--lexicon", help="deployment lexicon JSON (default: from env)")
    parser.add_argument("--overrides", help="user overlay JSON (default: from env)")
    parser.add_argument("--learned", help="learned overlay JSON (default: from env)")
    parser.add_argument(
        "--env-file",
        metavar="FILE",
        help="read the deployment .env for MAX_*, PRONUNCIATION_*, NORMALISE_* and LOCALE",
    )
    parser.add_argument("--voice", help="default voice, which selects its rule set")
    parser.add_argument(
        "--normalise",
        action="store_true",
        help="enable the built-in number and acronym layers",
    )
    parser.add_argument(
        "--ambiguity",
        choices=("guess", "ignore", "error"),
        default="guess",
        help="what to do with an unsettled heteronym",
    )
    parser.add_argument("--json", action="store_true", help="emit the full compiled result")
    parser.add_argument(
        "--list-filters",
        action="store_true",
        help="list the available [filter:] blocks and exit",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def _read(path: str | None) -> str:
    if path is None or path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _load_env_file(path: str) -> None:
    """Read KEY=VALUE lines into os.environ, never overriding what is already set."""
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key.startswith(ENV_PREFIXES):
                continue
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def _compile_request(path: str, data: lexicon.LexiconData, settings: Settings) -> script.Compiled:
    """Compile a full speech request, the same shape the HTTP routes accept."""
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    body = request.SpeechRequest.model_validate(doc)
    request.check_model(body.model)
    return script.compile_script(
        script.ScriptInput(
            input=body.input,
            voice=body.voice,
            instructions=body.instructions,
            pronunciations=body.pronunciations,
            normalise=body.normalise,
            ambiguity=body.ambiguity,
        ),
        lexicon_data=data,
        settings=settings,
    )


def _check_filters(compiled: script.Compiled) -> None:
    """Validate every [filter:] block, so a one-shot compile fails before a render."""
    for turn in compiled.turns:
        if not turn.filter:
            continue
        try:
            filters.render_spec(turn.filter)
        except filters.FilterError as exc:
            raise RequestError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.list_filters:
        json.dump(filters.catalogue(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if args.env_file:
        _load_env_file(args.env_file)

    settings = load_settings()
    if args.lexicon is not None:
        settings = replace(settings, lexicon_path=args.lexicon)
    if args.overrides is not None:
        settings = replace(settings, lexicon_overlay_path=args.overrides)
    if args.learned is not None:
        settings = replace(settings, lexicon_learned_path=args.learned)

    data = lexicon.Lexicon(
        settings.lexicon_path,
        settings.limits,
        settings.lexicon_overlay_path,
        settings.lexicon_learned_path,
    ).current()

    try:
        if args.request:
            compiled = _compile_request(args.request, data, settings)
        else:
            compiled = script.compile_script(
                script.ScriptInput(
                    input=_read(args.file),
                    voice=args.voice,
                    normalise=True if args.normalise else None,
                    ambiguity=args.ambiguity,
                ),
                lexicon_data=data,
                settings=settings,
            )
        _check_filters(compiled)
    except AmbiguityError as exc:
        print(f"kokoro-tone-script: {exc}", file=sys.stderr)
        return 4
    except RequestError as exc:
        print(f"kokoro-tone-script: {exc}", file=sys.stderr)
        return 3

    if args.json or args.request:
        sys.stdout.write(compiled.to_json())
        sys.stdout.write("\n")
    else:
        print(compiled.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
