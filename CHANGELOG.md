# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-07-27

First release.

### Added

- **Read-only Gmail access.** A single OAuth scope, `gmail.readonly`, requested
  and enforced by Google itself — send, drafts, `messages.modify`, label
  creation, trash/delete, settings and `users.watch` are permanently out of
  scope.
- **Three login flows.** A local-redirect listener (default), `--manual`
  (one process, prompts on stdin) and `--print-url` → `--code` (two processes,
  agent-friendly, no listener required). PKCE state is persisted at mode 0600
  before prompting, so an interrupted login is resumable. `--code` accepts
  either the bare authorization code or the whole redirect URL, and surfaces
  `?error=` with its description.
- **Commands:** `configure`, `login`, `logout`, `profile`, `labels`, `list`,
  `search`, `read`, `thread`, `attachment`, `history`, `install-skill`.
- **Filters** shared by `list` and `search`: `--limit`, `--page-token`,
  `--unread`, `--has-attachments`, `--from`, `--to`, `--label`, `--after`,
  `--before`, `--include-spam-trash`. `--label` takes a name and resolves it to
  an id (unknown names come back with `didYouMean`); dates are validated rather
  than passed through.
- **JSON on stdout, everything else on stderr**, so output pipes cleanly into
  `jq`. `--output text` renders a human-readable view instead.
- **MIME handling:** depth-first body walk with text/HTML fallback that reports
  what was actually returned, attachment parts excluded from the body,
  padding-tolerant base64url decoding, and `email.utils.getaddresses()` address
  parsing so `"Doe, Jane" <j@x.com>` stays one entry.
- **Retry** with exponential backoff and jitter (~5 attempts) on 429, 5xx and
  403 `rateLimitExceeded`/`userRateLimitExceeded`. Every other failure is a
  `{"error", "status"}` object and exit 1 — never a traceback.
- **Bundled Claude Code skill**, shipped inside the wheel and written out by
  `gmail-cli install-skill` at project, user or explicit scope.
- Offline test suite covering path resolution, the scope, base64url decoding,
  header and address parsing, attachment detection and body selection.

[0.1.0]: https://github.com/dovidgefen/gmail-cli/releases/tag/v0.1.0
