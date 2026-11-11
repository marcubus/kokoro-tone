#!/usr/bin/env python3
"""Measure an audio file for A/B of emotion presets.

Reports mean F0, syllables/second, spectral centroid, integrated loudness and
true peak. Standard library + ffmpeg only.

    python scripts/analyse.py out.mp3
"""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
import subprocess
import sys

F0_RATE = 8000  # F0 search runs at 8 kHz: enough for 60-500 Hz, cheap in Python
F0_MIN_HZ = 60.0
F0_MAX_HZ = 500.0


def ffmpeg_pcm(path: str, rate: int) -> bytes:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            path,
            "-ac",
            "1",
            "-ar",
            str(rate),
            "-f",
            "s16le",
            "pipe:1",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout


def samples(pcm: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2]))


def rms(frame: list[int]) -> float:
    if not frame:
        return 0.0
    return math.sqrt(sum(value * value for value in frame) / len(frame))


def mean_f0(pcm: bytes, rate: int = F0_RATE) -> tuple[float, float]:
    data = samples(pcm)
    frame_size = int(rate * 0.04)
    hop = int(rate * 0.02)
    if len(data) < frame_size:
        return 0.0, 0.0

    energies = [rms(data[i : i + frame_size]) for i in range(0, len(data) - frame_size, hop)]
    threshold = max(energies, default=0.0) * 0.15
    min_lag = int(rate / F0_MAX_HZ)
    max_lag = int(rate / F0_MIN_HZ)

    voiced: list[float] = []
    for index, start in enumerate(range(0, len(data) - frame_size, hop)):
        if energies[index] < threshold or energies[index] < 50:
            continue
        frame = data[start : start + frame_size]
        best_lag = 0
        best_score = 0.0
        for lag in range(min_lag, max_lag + 1):
            limit = frame_size - lag
            if limit <= 0:
                break
            score = sum(frame[i] * frame[i + lag] for i in range(limit))
            if score > best_score:
                best_score = score
                best_lag = lag
        if best_lag:
            voiced.append(rate / best_lag)

    if not voiced:
        return 0.0, 0.0
    voiced.sort()
    median = voiced[len(voiced) // 2]
    mean = sum(voiced) / len(voiced)
    return mean, median


def syllables_per_second(pcm: bytes, rate: int = F0_RATE) -> float:
    data = samples(pcm)
    frame_size = int(rate * 0.01)
    envelope = [rms(data[i : i + frame_size]) for i in range(0, len(data) - frame_size, frame_size)]
    if not envelope:
        return 0.0
    peak = max(envelope)
    if peak <= 0:
        return 0.0
    floor = peak * 0.2
    min_gap = 12  # frames; 120 ms between syllable nuclei
    count = 0
    last = -min_gap
    for index in range(1, len(envelope) - 1):
        value = envelope[index]
        if value < floor:
            continue
        if value >= envelope[index - 1] and value > envelope[index + 1]:
            if index - last >= min_gap:
                count += 1
                last = index
    duration = len(data) / rate
    return count / duration if duration else 0.0


def loudness(path: str) -> tuple[float, float]:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            path,
            "-af",
            "loudnorm=I=-16:TP=-1:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    stderr = proc.stderr.decode("utf-8", "replace")
    blocks = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.S)
    if proc.returncode != 0 or not blocks:
        raise SystemExit(stderr.strip()[-800:] or "loudnorm produced no measurement")
    data = json.loads(blocks[-1])
    return float(data["input_i"]), float(data["input_tp"])


def spectral_centroid(path: str) -> float:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            path,
            "-af",
            "aspectralstats=measure=centroid,ametadata=print:file=-",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    values = [
        float(match.group(1))
        for match in re.finditer(
            r"lavfi\.aspectralstats(?:\.\d+)?\.centroid=([0-9.]+)", proc.stdout
        )
    ]
    return sum(values) / len(values) if values else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", help="audio file to analyse")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    pcm = ffmpeg_pcm(args.audio, F0_RATE)
    duration = len(pcm) / 2 / F0_RATE
    f0_mean, f0_median = mean_f0(pcm)
    lufs, true_peak = loudness(args.audio)
    report = {
        "file": args.audio,
        "duration_s": round(duration, 3),
        "mean_f0_hz": round(f0_mean, 1),
        "median_f0_hz": round(f0_median, 1),
        "syllables_per_s": round(syllables_per_second(pcm), 2),
        "spectral_centroid_hz": round(spectral_centroid(args.audio), 1),
        "lufs": lufs,
        "true_peak_db": true_peak,
    }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for key, value in report.items():
            print(f"{key:22} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
