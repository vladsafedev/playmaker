# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-06-10

### Removed

- ACP/Zed integration layer — project is now a dispatch-only core with no Zed or ACP dependencies.

### Changed

- `claude`: headless dispatch and resume now pass `--dangerously-skip-permissions` so unattended runs are not blocked by permission prompts.
- `quotas`: CLI renders the Extra usage pool (metered overage) alongside standard quota display.
- `gemini`: falls back to session-file parse when the stream or JSON response body is empty, preventing silent failures on partial responses.
