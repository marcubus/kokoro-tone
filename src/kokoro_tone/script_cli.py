"""Command-line access to the script layer, with no server and no engine.

    python -m kokoro_tone.script_cli script.txt
    python -m kokoro_tone.script_cli --normalise --ambiguity ignore < script.txt

The output is the text the engine would be asked to say, plus any heteronym the
rules had to guess. That is the useful part of the gateway on its own: a script
compiler you can run in a pipeline, a test, or an editor hook.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace

from . import __version__, filters, lexicon, script
from .errors import AmbiguityError, RequestError
from .settings import load_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kokoro-tone-script",
        description="Resolve a script into the spoken text the engine would receive.",
    )
    parser.add_argument("file", nargs="?", help="script file; read stdin when omitted")
    parser.add_argument("--lexicon", help="deployment lexicon JSON (default: from env)")
    parser.add_argument("--overrides", help="user overlay JSON (default: from env)")
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.list_filters:
        json.dump(filters.catalogue(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    settings = load_settings()
    if args.lexicon is not None:
        settings = replace(settings, lexicon_path=args.lexicon)
    if args.overrides is not None:
        settings = replace(settings, lexicon_overlay_path=args.overrides)

    data = lexicon.Lexicon(
        settings.lexicon_path,
        settings.limits,
        settings.lexicon_overlay_path,
    ).current()

    try:
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
    except AmbiguityError as exc:
        print(f"kokoro-tone-script: {exc}", file=sys.stderr)
        return 4
    except RequestError as exc:
        print(f"kokoro-tone-script: {exc}", file=sys.stderr)
        return 3

    if args.json:
        sys.stdout.write(compiled.to_json())
        sys.stdout.write("\n")
    else:
        print(compiled.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
