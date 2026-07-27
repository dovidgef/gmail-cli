# gmail-cli

[![CI](https://github.com/dovidgef/gmail-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/dovidgef/gmail-cli/actions/workflows/ci.yml)

A strictly read-only, JSON-first command-line client for Gmail.

It exists so an AI agent (or a shell script, or you) can search, read and inspect a Gmail account without any ability to change it — and so the one interactive login stays a human's job.

```bash
gmail-cli search 'from:stripe has:attachment newer_than:30d' --limit 5
gmail-cli read 18f3c2a9b7d41e05 --body-type text
gmail-cli attachment 18f3c2a9b7d41e05 ANGjdJ8x... --save ./receipts/
```

## Read-only, and not on the honour system

`gmail-cli` requests exactly one OAuth scope:

```
https://www.googleapis.com/auth/gmail.readonly
```

That is the enforcement boundary. Google rejects mutating endpoints at the API layer for a token with only this scope, so no flag, no subcommand, and no future edit to this code can send mail, mark something read, apply a label, archive, trash or delete. Sending, drafts, `messages.modify`, label creation, settings and `users.watch` are permanently out of scope.

To revoke access entirely, remove the app at <https://myaccount.google.com/permissions>. (`gmail-cli logout` only deletes the local token cache — it does not revoke server-side.)

## Install

```bash
uv tool install git+https://github.com/dovidgef/gmail-cli.git
```

That lands `gmail-cli` in `~/.local/bin` — no sudo, no venv juggling — and the
wheel carries the Claude Code skill with it, so there is nothing to clone.
Python 3.10+. `pipx install git+https://github.com/dovidgef/gmail-cli.git` and
the `pip install git+…` equivalent work the same way.

> ### Do not `uv tool install gmail-cli`
>
> This project is **not on PyPI**. The `gmail-cli` name there is already taken by
> an unrelated project that *sends* mail through the Gmail API. It installs
> cleanly and puts a `gmail-cli` binary on your PATH, so getting the wrong one is
> silent — you would end up with a tool that can send mail while every read-only
> guarantee on this page still appears to apply. Always install from the git URL.

Contributors, from a clone:

```bash
uv tool install .
```

## One-time Google Cloud setup

Google requires your own OAuth client — there is no shared one to borrow.

1. Open <https://console.cloud.google.com/> and create (or pick) a project.
2. **APIs & Services → Library** → enable **Gmail API**.
3. **Google Auth Platform → Audience** → *External*. While publishing status is **Testing** you must add your own address as a test user; **In production** needs no allowlist and any Google account can consent.
4. **Google Auth Platform → Clients → Create client** → application type **Desktop app** → **Download JSON**.
5. Save it as `~/.gmail-cli/credentials.json`, or point at it:

```bash
gmail-cli configure --credentials ~/Downloads/client_secret_....json
```

Application type matters: a **Web application** client pins fixed redirect URIs and
will fail this tool's loopback flow with `redirect_uri_mismatch`. Desktop clients
accept any `localhost` port.

`gmail.readonly` is a **restricted** scope, so an unverified consent screen shows
an "unverified app" interstitial (*Advanced → Go to … (unsafe)*) and caps the
project at 100 consenting users for its lifetime. Both are fine for personal use.

## Log in

```bash
gmail-cli login          # default: a throwaway local server catches the redirect
gmail-cli profile        # confirm which account you authorized
```

The consent URL is printed to **stderr** — open it, approve, and the local listener completes the exchange. `--port N` pins the port (`0`, the default, auto-assigns).

### Headless or remote machines

If there's no browser on the machine, either tunnel the redirect port back to your laptop:

```bash
ssh -L 8765:localhost:8765 user@host
gmail-cli login --port 8765
```

…or use the two-phase flow, which needs no listener at all:

```bash
gmail-cli login --print-url       # prints {"status":"url_ready","auth_url":...}
# open auth_url in any browser, approve, then copy the localhost URL the browser
# fails to load — that failure is expected, the code is in the address bar
gmail-cli login --code 'http://localhost:8765/?code=4/0AVMB...&scope=...'
```

`--code` takes either the whole redirect URL or the bare code. The PKCE verifier is saved to `~/.gmail-cli/manual-flow-state.json` (mode 0600) between the two steps and deleted once the exchange succeeds.

`gmail-cli login --manual` is the same thing in one process: it prints the URL and waits on stdin. It persists the PKCE state *before* prompting, so an interrupted run is still resumable with `--code`.

Subsequent runs refresh the token silently. `gmail-cli login` on a valid token reports `already_logged_in` and does nothing; use `--force` to re-authenticate.

## Commands

| Command | What it does |
|---|---|
| `configure` | Set `credentials_path` and `default_format` |
| `login` / `logout` | Authorize this machine / delete the local token cache |
| `profile` | Authorized address, message and thread totals, current `historyId` |
| `labels` | Every label with its id |
| `list` | List messages, newest first, with filter flags |
| `search QUERY` | Raw Gmail query plus the same filter flags |
| `read MESSAGE_ID` | Headers plus the decoded body |
| `thread THREAD_ID` | Every message in a thread, summaries only |
| `attachment MSG_ID ATT_ID` | Attachment size, or `--save PATH` to download |
| `history --start-history-id N` | Mailbox changes since a sync point |
| `install-skill` | Write the bundled Claude Code skill into a skills directory |

Global: `--output/-o {json,text}`, `--version`. Bare `gmail-cli` prints help and exits 0.

### Filters (`list` and `search`)

`--limit` (25) · `--page-token` · `--unread` · `--has-attachments` · `--from ADDR` · `--to ADDR` · `--label NAME` · `--after YYYY-MM-DD` · `--before YYYY-MM-DD` · `--include-spam-trash`

They all AND together. `--label` takes a label **name**, not an id, and resolves it via `labels.list` — so `--label 'Work/Invoices'` needs no escaping, and an unknown name errors with `didYouMean` suggestions. Dates are validated: `--after 2024-13-99` is an error, not a silent pass-through.

`search` also takes `--subject-only`, which wraps the query as `subject:(QUERY)` before the flag sugar is appended.

### Gmail query operators

`search` passes `QUERY` to Gmail verbatim, so the whole operator set is available:

```
from:alice@x.com   to:me   cc:   bcc:   subject:invoice   "exact phrase"
after:2024/01/01   before:2024/03/31   newer_than:7d   older_than:1y
has:attachment   filename:pdf   larger:5M
label:work   in:anywhere   in:spam   is:unread   is:starred
from:a OR from:b   {from:a from:b}   -promotions   (a OR b) subject:x
```

### `read`

`--body-type {text,html}` picks the preferred flavour and falls back to the other when it's absent; `bodyContentType` in the response reports what you actually got. `--headers` adds every header verbatim (a list, since `Received` repeats). `--raw` returns the RFC822 source; `--save-eml PATH` writes it to disk instead.

## Output

Everything on stdout is JSON (`indent=2`, UTF-8 preserved). OAuth chatter, prompts and setup guidance go to **stderr**, so stdout stays pipeable into `jq`. `--output text` swaps in a human-readable rendering.

`list` / `search` / `thread` return message summaries:

```json
{
  "messages": [
    {
      "id": "18f3c2a9b7d41e05",
      "threadId": "18f3c2a9b7d41e05",
      "subject": "Invoice 4021",
      "from": "Alice <alice@example.com>",
      "fromName": "Alice",
      "fromEmail": "alice@example.com",
      "to": ["me@example.com"],
      "cc": [],
      "date": "2024-03-11T09:02:41+00:00",
      "labelIds": ["INBOX", "UNREAD"],
      "hasAttachments": true,
      "snippet": "Please find attached…"
    }
  ],
  "count": 1,
  "query": "from:alice",
  "nextPageToken": "09876543210"
}
```

`read` adds `bcc`, `replyTo`, `messageId`, `bodyContentType`, `body`, and `attachments[]` of `{id, name, contentType, size, isInline}` — the `attachments` key is omitted entirely when there are none.

Address lists are parsed with `email.utils.getaddresses()`, so `"Doe, Jane" <j@x.com>` stays a single entry instead of being torn in half at the comma.

Errors are `{"error": "…"}` on stdout with exit 1 — never a traceback.

| Exit | Condition |
|---|---|
| 0 | success (including bare invocation, which prints help) |
| 1 | missing libraries · no OAuth client JSON · not logged in or unrefreshable · empty search query · invalid date · unknown label · 404 · manual-flow error · API error after retries |
| 2 | usage error (unknown flag or subcommand) |

## State on disk (`~/.gmail-cli/`)

| File | Written by | Mode |
|---|---|---|
| `credentials.json` | you (downloaded from Google Cloud) | — |
| `token_cache.json` | `login` | 0600 |
| `config.json` | `configure` | default |
| `manual-flow-state.json` | `login --print-url` / `--manual` | 0600, deleted after exchange |

Credentials resolution: `$GMAIL_CLI_CREDENTIALS` → `config.json:credentials_path` → `~/.gmail-cli/credentials.json`. That environment variable is the only one consulted. No secret is ever written to stdout.

## API cost

The Gmail API has no bulk metadata fetch, so a listing costs roughly **one request per message returned**, on top of `ceil(limit / 500)` list calls:

- `--limit 25` → ~26 requests. Fine.
- `--limit 500` → ~501 requests against a budget of 250 quota units per user per second (list and get are 5 units each). Expect rate limiting.

Narrow with a query rather than raising `--limit`. Requests are retried with exponential backoff and jitter (~5 attempts) on 429, 5xx, and 403 with reason `rateLimitExceeded`/`userRateLimitExceeded`; anything else fails immediately with a clean error.

## Known caveats

- **Timezones.** Emitted `date` fields are ISO-8601 **UTC**, but Gmail's `after:`/`before:` filter against its own internal date in the *account's* timezone. A message near midnight can therefore look a day off relative to the filter that matched it.
- **Snippets are HTML-escaped**, exactly as Gmail returns them (`&#39;`).
- **Attachment ids are ephemeral.** Gmail mints a fresh `attachmentId` on every `messages.get`, so an id from an old `read` may not appear in a later fetch. Take the id from `read` and use it promptly; `attachment --save` into a directory still recovers the correct filename by falling back to byte size when the id has rotated.
- **Summaries cost a masked `format=full` fetch, not `format=metadata`.** Gmail returns no `parts` array whatsoever at `format=metadata`, which makes attachment detection impossible there. Listings therefore request `format=full` behind a `fields` mask that omits `body/data` — same one request per message, ~8 KB each, and `hasAttachments` agrees exactly with `read`.
- **`history`** only reaches back about a week; older sync points return an error telling you to re-list.

## Claude Code skill

A ready-made agent guide ships inside the wheel:

```bash
gmail-cli install-skill                 # ./.claude/skills/gmail-cli/SKILL.md
gmail-cli install-skill --scope user    # ~/.claude/skills/gmail-cli/SKILL.md
gmail-cli install-skill --target DIR --force
```

It covers when to reach for the tool, the login hand-off (an agent can't complete OAuth alone), the query cheatsheet, the JSON shapes, and the read-only boundary — so an agent never tries to send or label.

## Development

```bash
uv sync --group dev
uv run pytest -q
uv run python -m unittest tests.test_auth_paths -v
uv run ruff check . && uv run ruff format --check . && uv run mypy src/gmail_cli
uv run pre-commit install
```

Tests are fully offline and need no credentials. The network paths, the three OAuth flows, pagination, `attachment --save` and the text renderer are verified by hand.

CI runs the same four checks on Python 3.10–3.13, plus a packaging job that
builds both artifacts, asserts `SKILL.md` is inside each, installs the wheel and
writes the skill back out — the `force-include` that bundles it is invisible to
the test suite otherwise.

## License

MIT — see [LICENSE](LICENSE).
