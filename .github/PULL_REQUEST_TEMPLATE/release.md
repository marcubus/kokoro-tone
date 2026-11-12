## Release

- Version: `YYYY.MM.MICRO`
- Tag: `vYYYY.MM.MICRO`

## Checklist

- [ ] `__version__` in `src/kokoro_tone/__init__.py` matches the release version
- [ ] `CHANGELOG.md` has an entry for this version
- [ ] `engine.version` unchanged, or the engine contract re-checked
- [ ] `make check`
- [ ] `make test`
- [ ] `make version`
- [ ] `make build`
- [ ] Tag created after merge and pushed
- [ ] `release` workflow published, scanned and signed the image
- [ ] `make up-image KOKORO_TONE_VERSION=YYYY.MM.MICRO` starts it
