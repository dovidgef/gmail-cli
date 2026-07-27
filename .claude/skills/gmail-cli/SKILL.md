---
name: gmail-cli
description: Search and read a real Gmail mailbox from the shell via `gmail-cli` — find an email, check the inbox, look up what someone sent, pull a thread, download an attachment, dig out a receipt/invoice/booking/confirmation code. Use this whenever `gmail-cli` is installed and the task involves the user's own email. Triggers on phrases like "check my email", "search my inbox", "did X email me", "find the email about Y", "what did <person> say", "any unread mail", "pull up that receipt", "forward me the details from", "when did <company> email me", "get the attachment from that email", "read message <id>", "show the thread". It is strictly READ-ONLY — it holds only the gmail.readonly scope, so it can never send, reply, label, archive, trash or delete; say so plainly rather than attempting a workaround. If `gmail-cli` is not installed, tell the user `uv tool install gmail-cli` (or `pipx install gmail-cli` / `pip install gmail-cli`) and carry on with this skill once it is.
---

# gmail-cli

A strictly read-only Gmail client with a stable JSON-on-stdout contract. Built so an agent can query a mailbox the same way it would use `jq` or `gh`.

**Check it's installed:** `command -v gmail-cli` — if absent, ask the user to `uv tool install gmail-cli`.

## The read-only boundary

`gmail-cli` requests exactly one OAuth scope: `https://www.googleapis.com/auth/gmail.readonly`. Google itself rejects every mutating call, so there is no flag, no subcommand and no workaround that sends mail, marks something read, applies a label, or deletes anything. If the user asks for one of those, say the tool can't do it — don't reach for `curl`, a Python script, or another mail path unless they explicitly ask you to.

## Bootstrap (once per machine, needs a human)

Login requires a browser and Google consent, so **the user runs it, not you**. If any command returns `{"error": "Not logged in. Run: gmail-cli login"}`, stop and hand these steps over:

```bash
gmail-cli login                 # opens a local redirect listener; prints the URL to stderr
gmail-cli login --print-url     # headless/remote: prints the consent URL as JSON, exits
gmail-cli login --code '<url>'  # then paste back the localhost URL the browser failed to load
```

The redirect page failing to load is expected — nothing is listening on that port. The authorization code is in the address bar; passing the whole URL to `--code` works.

If they have no OAuth client yet, `gmail-cli login` prints a five-step Google Cloud setup guide to stderr. `gmail-cli profile` confirms who is authorized.

## Commands

```bash
gmail-cli profile                                  # which account, message/thread totals, historyId
gmail-cli labels                                   # every label with its id
gmail-cli list --limit 10 --unread                 # newest first
gmail-cli search 'from:stripe has:attachment'      # raw Gmail query
gmail-cli read <MESSAGE_ID>                        # headers + decoded body
gmail-cli thread <THREAD_ID>                       # every message in the thread, summaries only
gmail-cli attachment <MSG_ID> <ATT_ID> --save ./out/
gmail-cli history --start-history-id <N>           # changes since a sync point
```

Shared filters on `list` and `search` (all AND together): `--limit`, `--page-token`, `--unread`, `--has-attachments`, `--from ADDR`, `--to ADDR`, `--label NAME`, `--after YYYY-MM-DD`, `--before YYYY-MM-DD`, `--include-spam-trash`.

`--label` takes a label **name** ("Work/Invoices"), not an id, and resolves it for you — no quoting or escaping needed for nested or spaced names.

`read` extras: `--body-type {text,html}`, `--headers` (full header dump), `--raw` (RFC822 source), `--save-eml PATH`.

## Gmail query operators (`search`)

The `QUERY` argument is passed to Gmail verbatim, so the full operator set works:

| Operator | Example |
|---|---|
| sender / recipients | `from:alice@x.com`, `to:me`, `cc:`, `bcc:` |
| subject | `subject:invoice` |
| exact phrase | `"quarterly report"` |
| date range | `after:2024/01/01`, `before:2024/03/31` |
| relative age | `newer_than:7d`, `older_than:1y` (`d`/`m`/`y`) |
| attachments | `has:attachment`, `filename:pdf`, `filename:report.xlsx` |
| location | `label:work`, `in:anywhere`, `in:spam`, `in:trash` |
| state | `is:unread`, `is:read`, `is:starred`, `is:important` |
| size | `larger:5M`, `smaller:1M` |
| boolean | `from:a OR from:b`, `{from:a from:b}`, `-promotions`, `(a OR b) subject:x` |

`in:anywhere` is the one that reaches Spam and Trash inside a query; the `--include-spam-trash` flag does the same thing via the API.

## Output shapes

Everything on stdout is JSON (`--output text` gives a human-readable rendering instead). All prompts, OAuth chatter and setup guidance go to stderr, so stdout stays pipeable into `jq`.

`list` / `search` / `thread` return message summaries:

```json
{ "messages": [ {
    "id": "18f...", "threadId": "18f...",
    "subject": "Invoice 4021", "from": "Alice <alice@x.com>",
    "fromName": "Alice", "fromEmail": "alice@x.com",
    "to": ["me@x.com"], "cc": [],
    "date": "2024-03-11T09:02:41+00:00",
    "labelIds": ["INBOX", "UNREAD"], "hasAttachments": true, "snippet": "…" } ],
  "count": 1, "query": "from:alice", "nextPageToken": "…" }
```

`read` adds `bcc`, `replyTo`, `messageId`, `bodyContentType` (`text` or `html` — what you *got*, which may not be what you asked for), `body`, and `attachments[]` of `{id, name, contentType, size, isInline}`. The `attachments` key is absent entirely when there are none.

`nextPageToken` is present only when more results exist; feed it back with `--page-token`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | an error — stdout holds `{"error": "…"}`; read it, don't retry blindly |
| 2 | usage error (bad flag or subcommand) |

Every failure is a JSON object with an `error` key, never a traceback. Common ones: `Not logged in. Run: gmail-cli login` (hand off to the user), `Unknown label: X` (comes with `didYouMean`), `No such message: <id>`.

## Working notes

- **Two-step reads.** `list`/`search` give ids and snippets; `read <id>` gives the body. Search first, then read only what matters — each message costs its own API call.
- **Cost.** A listing is roughly one request per message returned. `--limit 25` is cheap; `--limit 500` is ~500 requests and will hit rate limits. Narrow with a query rather than raising the limit.
- **Dates are UTC.** Emitted `date` fields are ISO-8601 UTC, but Gmail's `after:`/`before:` filter on its own internal date in the account's timezone — a message right at midnight can look a day off.
- **Snippets are HTML-escaped** as Gmail returns them (`&#39;`), so unescape before quoting them back to the user.
- **Attachment ids expire.** Gmail regenerates `attachmentId` on each `read`, so fetch the id and download in the same breath rather than reusing one from earlier in the conversation.
- **Don't dump whole mailboxes into context.** Pull the summary, answer the question, and fetch bodies on demand.
