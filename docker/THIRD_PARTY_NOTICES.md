# Third-party notices

`kokoro-tone` itself is MIT-licensed; see [`LICENSE`](LICENSE).

The published container image bundles third-party binaries under their own
licences. This file is copied into the image at
`/usr/share/doc/kokoro-tone/THIRD_PARTY_NOTICES.md`.

## FFmpeg

The image builds FFmpeg 7.0.2 from source with `--enable-gpl` and
`--enable-librubberband`, so the resulting `ffmpeg` binary is licensed under the
GNU General Public License, version 2 or later.

- Source: <https://ffmpeg.org/releases/ffmpeg-7.0.2.tar.xz>
- Licence: <https://ffmpeg.org/legal.html> (GPLv2+)
- The exact configure flags and source checksum are in the repository
  `Dockerfile`.

## Rubber Band Library

`librubberband2` (and the `librubberband-dev` used at build time) is the Rubber
Band Library, licensed under the GNU General Public License, version 2 or later.

- Source: <https://breakfastquay.com/rubberband/>
- Debian source: <https://packages.debian.org/bookworm/librubberband2>

## Other components

The image also contains Debian packages and the Python packages pinned in
`uv.lock`; their licences are those of the upstream projects.
