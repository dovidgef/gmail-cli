"""gmail-cli — a strictly read-only, JSON-first command-line client for Gmail.

Everything lives in this one module on purpose: the tool is small enough that a
reader (human or agent) can hold it in their head, and a single file keeps the
read-only posture auditable at a glance.

The enforcement boundary is the OAuth scope. ``SCOPES`` is exactly
``gmail.readonly``; Google rejects every mutating endpoint at the API layer, so
no code path here — present or future — can send, label, trash or delete.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import email.utils
import json
import os
import random
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, NoReturn

from gmail_cli import __version__

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build as build_service
    from googleapiclient.errors import HttpError

    _IMPORT_ERROR: str | None = None
except ImportError as exc:  # pragma: no cover - exercised only on a broken install
    _IMPORT_ERROR = str(exc)

# The single scope this tool ever requests. Read-only is enforced by Google,
# not merely by the absence of mutating subcommands.
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

EXIT_OK = 0
EXIT_ERROR = 1

_STATE_DIR_NAME = '.gmail-cli'
_SKILL_NAME = 'gmail-cli'
_INSTALL_HINT = (
    'Missing dependencies. Install with: '
    'uv tool install gmail-cli  (or: pip install google-auth google-auth-oauthlib '
    'google-api-python-client)'
)

_SETUP_GUIDE = """\
No OAuth client found. One-time Google Cloud setup:

  1. Open https://console.cloud.google.com/ and create (or pick) a project.
  2. APIs & Services -> Library -> enable "Gmail API".
  3. Google Auth Platform -> Audience -> External. In "Testing" status add
     yourself as a test user; "In production" needs no allowlist.
  4. Google Auth Platform -> Clients -> Create client ->
     Application type: "Desktop app" -> Download JSON.
     Save it now: Google never shows the client secret again. If you lose it,
     "Add secret" on the client page is the only recovery.
  5. Save it as {path}
     (or point at it with: gmail-cli configure --credentials /path/to/client.json)

Then run: gmail-cli login\
"""


# --------------------------------------------------------------------------
# Paths and configuration
# --------------------------------------------------------------------------


def state_dir() -> Path:
    """Return ``~/.gmail-cli``. Created lazily by the writers below."""
    return Path.home() / _STATE_DIR_NAME


def token_path() -> Path:
    return state_dir() / 'token_cache.json'


def config_path() -> Path:
    return state_dir() / 'config.json'


def manual_state_path() -> Path:
    return state_dir() / 'manual-flow-state.json'


def ensure_state_dir() -> Path:
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_config() -> dict[str, Any]:
    """Read ``config.json``; a missing or corrupt file is simply an empty config."""
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(config: dict[str, Any]) -> Path:
    ensure_state_dir()
    path = config_path()
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return path


def resolve_credentials_path() -> Path:
    """Locate the OAuth client JSON.

    Precedence: ``$GMAIL_CLI_CREDENTIALS`` -> ``config.json:credentials_path`` ->
    ``~/.gmail-cli/credentials.json``. That env var is the only one consulted.
    """
    env = os.environ.get('GMAIL_CLI_CREDENTIALS')
    if env:
        return Path(env).expanduser()
    configured = load_config().get('credentials_path')
    if configured:
        return Path(str(configured)).expanduser()
    return state_dir() / 'credentials.json'


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with 0600 permissions, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    # Best effort — some filesystems (e.g. a mounted FAT volume) can't chmod.
    with contextlib.suppress(OSError):
        path.chmod(0o600)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def resolve_format(explicit: str | None) -> str:
    """explicit flag -> ``config.default_format`` -> ``json``."""
    if explicit:
        return explicit
    configured = load_config().get('default_format')
    if configured in ('json', 'text'):
        return str(configured)
    return 'json'


def emit(data: Any, fmt: str) -> None:
    """Print a successful result to stdout in the requested format."""
    if fmt == 'text':
        sys.stdout.write(render_text(data))
    else:
        sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + '\n')


def fail(message: str, fmt: str = 'json', **extra: Any) -> NoReturn:
    """Emit an error payload on stdout and exit 1. Never raises a traceback."""
    payload: dict[str, Any] = {'error': message}
    payload.update(extra)
    if fmt == 'text':
        sys.stdout.write(f'error: {message}\n')
        for key, value in extra.items():
            sys.stdout.write(f'  {key}: {value}\n')
    else:
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + '\n')
    raise SystemExit(EXIT_ERROR)


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value.ljust(width)
    return value[: width - 1] + '…'


def _format_date(iso: str) -> str:
    """``2023-11-14T22:13:20+00:00`` -> ``2023-11-14 22:13:20``."""
    if not iso:
        return ' ' * 19
    try:
        return datetime.fromisoformat(iso).strftime('%Y-%m-%d %H:%M:%S')
    except ValueError:
        return iso[:19].ljust(19)


def render_text(data: Any) -> str:
    """Render a result dict as human-readable text, dispatching on its shape."""
    if not isinstance(data, dict):
        return f'{data}\n'

    if 'messages' in data and isinstance(data['messages'], list):
        return _render_messages(data)
    if 'body' in data and 'subject' in data:
        return _render_message_full(data)
    if 'labels' in data and isinstance(data['labels'], list):
        return _render_labels(data)
    if 'emailAddress' in data:
        return _render_profile(data)
    return _render_kv(data)


def _render_messages(data: dict[str, Any]) -> str:
    messages = data['messages']
    lines = [f'--- {len(messages)} message(s) ---']
    for msg in messages:
        marker = '[+]' if msg.get('hasAttachments') else '   '
        sender = msg.get('fromName') or msg.get('fromEmail') or msg.get('from') or ''
        lines.append(
            (
                f'{marker} {_format_date(msg.get("date", ""))}  '
                f'{_truncate(str(sender), 30)}  {_truncate(str(msg.get("subject", "")), 60)}'
            ).rstrip()
        )
        lines.append(f'      id: {msg.get("id", "")}')
    if data.get('nextPageToken'):
        lines.append('')
        lines.append(f'More results: --page-token {data["nextPageToken"]}')
    return '\n'.join(lines) + '\n'


def _render_message_full(data: dict[str, Any]) -> str:
    lines = []
    for label, key in (
        ('From', 'from'),
        ('To', 'to'),
        ('Cc', 'cc'),
        ('Bcc', 'bcc'),
        ('Date', 'date'),
        ('Subject', 'subject'),
    ):
        value = data.get(key)
        if not value:
            continue
        if isinstance(value, list):
            value = ', '.join(str(v) for v in value)
        lines.append(f'{label}: {value}')
    attachments = data.get('attachments') or []
    if attachments:
        lines.append(f'Attachments: {len(attachments)}')
        for att in attachments:
            lines.append(
                f'  - {att.get("name", "")} ({att.get("contentType", "")}, '
                f'{att.get("size", 0)} bytes) id={att.get("id", "")}'
            )
    lines.append('=' * 60)
    lines.append(str(data.get('body', '')))
    return '\n'.join(lines) + '\n'


def _render_labels(data: dict[str, Any]) -> str:
    names = [str(label.get('name', '')) for label in data['labels']]
    lines = [f'--- {len(names)} label(s) ---']
    width = max((len(n) for n in names), default=0) + 2
    for i in range(0, len(names), 3):
        lines.append(''.join(n.ljust(width) for n in names[i : i + 3]).rstrip())
    return '\n'.join(lines) + '\n'


def _render_profile(data: dict[str, Any]) -> str:
    return (
        f'Email:    {data.get("emailAddress", "")}\n'
        f'Messages: {data.get("messagesTotal", "")}\n'
        f'Threads:  {data.get("threadsTotal", "")}\n'
    )


def _render_kv(data: dict[str, Any]) -> str:
    lines = []
    for key, value in data.items():
        shown = (
            json.dumps(value, ensure_ascii=False, default=str)
            if isinstance(value, (dict, list))
            else value
        )
        lines.append(f'{key}: {shown}')
    return '\n'.join(lines) + '\n' if lines else ''


# --------------------------------------------------------------------------
# OAuth
# --------------------------------------------------------------------------

_MANUAL_DEFAULT_PORT = 8765


def require_google() -> None:
    """Bail out with an install hint if the Google client libraries are absent."""
    if _IMPORT_ERROR is not None:
        print(f'{_INSTALL_HINT} [{_IMPORT_ERROR}]', file=sys.stderr)
        raise SystemExit(EXIT_ERROR)


def require_client_secrets(fmt: str) -> Path:
    """Return the OAuth client JSON path, or print the setup guide and exit 1."""
    path = resolve_credentials_path()
    if not path.is_file():
        print(_SETUP_GUIDE.format(path=path), file=sys.stderr)
        fail(f'OAuth client JSON not found: {path}', fmt)
    return path


def load_cached_credentials() -> Credentials | None:
    path = token_path()
    if not path.is_file():
        return None
    try:
        return Credentials.from_authorized_user_file(str(path), SCOPES)
    except (OSError, ValueError):
        return None


def save_credentials(creds: Credentials) -> Path:
    path = token_path()
    write_private(path, creds.to_json())
    return path


def get_credentials(fmt: str) -> Credentials:
    """Load cached credentials, refreshing silently when they have expired."""
    require_google()
    creds = load_cached_credentials()
    if creds is None:
        fail('Not logged in. Run: gmail-cli login', fmt)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            fail(f'Could not refresh the cached token: {exc}. Run: gmail-cli login --force', fmt)
        save_credentials(creds)
        return creds
    fail('Cached credentials are unusable. Run: gmail-cli login --force', fmt)


def _new_flow(cred_path: Path, redirect_uri: str | None = None, state: str | None = None) -> Any:
    kwargs: dict[str, Any] = {}
    if state:
        kwargs['state'] = state
    flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), SCOPES, **kwargs)
    if redirect_uri:
        flow.redirect_uri = redirect_uri
    return flow


def _authorization_url(flow: Any) -> tuple[str, str]:
    """Build the consent URL. ``prompt=consent`` is what makes Google hand back a
    refresh token, which is what keeps subsequent runs non-interactive."""
    auth_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        include_granted_scopes='true',
    )
    return str(auth_url), str(state)


def _save_manual_state(flow: Any, cred_path: Path, state: str) -> Path:
    """Persist the PKCE verifier so a second process can finish the exchange."""
    path = manual_state_path()
    write_private(
        path,
        json.dumps(
            {
                'code_verifier': flow.code_verifier,
                'state': state,
                'redirect_uri': flow.redirect_uri,
                'credentials_path': str(cred_path),
            },
            indent=2,
        )
        + '\n',
    )
    return path


def _load_manual_state(fmt: str) -> dict[str, Any]:
    path = manual_state_path()
    if not path.is_file():
        fail(
            'No pending login found. Start one with: gmail-cli login --print-url',
            fmt,
            state_path=str(path),
        )
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        fail(f'Pending-login state is unreadable ({exc}). Re-run: gmail-cli login --print-url', fmt)
    if not isinstance(data, dict) or not data.get('redirect_uri'):
        fail('Pending-login state is incomplete. Re-run: gmail-cli login --print-url', fmt)
    return data


def _extract_code(value: str, fmt: str) -> str:
    """Accept either a bare authorization code or the whole redirect URL.

    The redirect URL is what a user actually has to hand: the browser fails to
    load ``http://localhost:<port>/?code=...`` because nothing is listening
    there, but the address bar still holds the code.
    """
    value = value.strip()
    if '://' in value or value.startswith('?'):
        query = urllib.parse.urlparse(value).query or value.lstrip('?')
        params = urllib.parse.parse_qs(query)
        if 'error' in params:
            description = params.get('error_description', [''])[0]
            detail = f' — {description}' if description else ''
            fail(f'Authorization was refused: {params["error"][0]}{detail}', fmt)
        codes = params.get('code')
        if not codes or not codes[0]:
            fail('No ?code= parameter in that URL. Copy the full redirect URL.', fmt)
        return codes[0]
    return value


def _login_result(creds: Credentials, mode: str) -> dict[str, Any]:
    return {
        'status': 'logged_in',
        'mode': mode,
        'token_path': str(token_path()),
        'scopes': SCOPES,
        'expiry': creds.expiry,
        'has_refresh_token': bool(creds.refresh_token),
    }


def _finish_with_code(value: str, fmt: str) -> None:
    """Phase 2 of the two-process flow: exchange the code against saved PKCE state."""
    state = _load_manual_state(fmt)
    code = _extract_code(value, fmt)
    cred_path = Path(str(state.get('credentials_path') or resolve_credentials_path()))
    if not cred_path.is_file():
        cred_path = require_client_secrets(fmt)

    flow = _new_flow(cred_path, redirect_uri=str(state['redirect_uri']), state=state.get('state'))
    flow.code_verifier = state.get('code_verifier')
    try:
        # Anything the OAuth stack prints belongs on stderr; stdout is JSON only.
        with contextlib.redirect_stdout(sys.stderr):
            flow.fetch_token(code=code)
    except Exception as exc:
        fail(f'Token exchange failed: {exc}', fmt)

    creds = flow.credentials
    save_credentials(creds)
    manual_state_path().unlink(missing_ok=True)
    emit(_login_result(creds, 'code'), fmt)


def cmd_login(args: argparse.Namespace, fmt: str) -> None:
    require_google()

    if args.code:
        _finish_with_code(args.code, fmt)
        return

    if not args.force:
        cached = load_cached_credentials()
        if cached is not None and cached.valid:
            emit(
                {
                    'status': 'already_logged_in',
                    'token_path': str(token_path()),
                    'scopes': SCOPES,
                    'expiry': cached.expiry,
                    'hint': 'Re-authenticate with: gmail-cli login --force',
                },
                fmt,
            )
            return

    cred_path = require_client_secrets(fmt)

    if args.print_url:
        port = args.port or _MANUAL_DEFAULT_PORT
        flow = _new_flow(cred_path, redirect_uri=f'http://localhost:{port}')
        auth_url, state = _authorization_url(flow)
        state_file = _save_manual_state(flow, cred_path, state)
        emit(
            {
                'status': 'url_ready',
                'auth_url': auth_url,
                'next': (
                    'Open the URL, approve access, then copy the localhost URL the '
                    "browser fails to load (it won't load — nothing is listening there) "
                    "and run: gmail-cli login --code '<url>'"
                ),
                'state_path': str(state_file),
            },
            fmt,
        )
        return

    if args.manual:
        port = args.port or _MANUAL_DEFAULT_PORT
        flow = _new_flow(cred_path, redirect_uri=f'http://localhost:{port}')
        auth_url, state = _authorization_url(flow)
        # Persist before prompting: an interrupted run stays resumable with --code.
        _save_manual_state(flow, cred_path, state)
        print(f'Open this URL and approve access:\n\n{auth_url}\n', file=sys.stderr)
        print(
            'The browser will then fail to load a localhost URL — that is expected.\n'
            'Paste that whole URL (or just the code) here.',
            file=sys.stderr,
        )
        try:
            entered = input('code or redirect URL: ')
        except (EOFError, KeyboardInterrupt):
            print('', file=sys.stderr)
            fail(
                'Login interrupted. Resume with: '
                "gmail-cli login --code '<url>'  (PKCE state was saved)",
                fmt,
            )
        _finish_with_code(entered, fmt)
        return

    # Default: spin up a throwaway local server to catch the redirect.
    flow = _new_flow(cred_path)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            creds = flow.run_local_server(
                port=args.port,
                open_browser=False,
                bind_addr='localhost',
                access_type='offline',
                prompt='consent',
                include_granted_scopes='true',
            )
    except Exception as exc:
        fail(f'Local-redirect login failed: {exc}', fmt)

    save_credentials(creds)
    emit(_login_result(creds, 'local_server'), fmt)


def cmd_logout(args: argparse.Namespace, fmt: str) -> None:
    """Delete the cached token. Config, client secrets and PKCE state are left alone.

    This is local-only — it does not revoke the grant server-side. Do that at
    https://myaccount.google.com/permissions
    """
    path = token_path()
    removed = path.is_file()
    if removed:
        path.unlink()
    emit({'status': 'logged_out', 'cache_removed': removed, 'token_path': str(path)}, fmt)


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------

_RETRY_ATTEMPTS = 5
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRY_REASONS = frozenset({'rateLimitExceeded', 'userRateLimitExceeded'})


def gmail_service(fmt: str) -> Any:
    creds = get_credentials(fmt)
    return build_service('gmail', 'v1', credentials=creds, cache_discovery=False)


def _error_body(exc: HttpError) -> dict[str, Any]:
    try:
        body = json.loads(exc.content.decode('utf-8', errors='replace'))
    except (AttributeError, ValueError):
        return {}
    error = body.get('error') if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def _error_reason(exc: HttpError) -> str:
    errors = _error_body(exc).get('errors') or []
    if errors and isinstance(errors[0], dict):
        return str(errors[0].get('reason', ''))
    return str(_error_body(exc).get('status', ''))


def _error_message(exc: HttpError) -> str:
    message = _error_body(exc).get('message')
    return str(message) if message else str(exc)


def _is_retryable(exc: HttpError, status: int | None) -> bool:
    if status in _RETRY_STATUSES:
        return True
    # Gmail signals per-user rate limiting as a 403 with a specific reason;
    # every other 403 (scope, disabled API) is permanent and must not be retried.
    return status == 403 and _error_reason(exc) in _RETRY_REASONS


def execute(request: Any, fmt: str, *, not_found: str | None = None) -> Any:
    """Run an API request with exponential backoff, and never raise a traceback."""
    delay = 1.0
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return request.execute()
        except HttpError as exc:
            status = exc.resp.status if exc.resp is not None else None
            if status == 404 and not_found is not None:
                fail(not_found, fmt, status=404)
            if attempt == _RETRY_ATTEMPTS - 1 or not _is_retryable(exc, status):
                fail(_error_message(exc), fmt, status=status)
            time.sleep(delay + random.uniform(0, delay / 2))
            delay *= 2
        except Exception as exc:
            # Transport-level failure (DNS, TLS, connection reset). Not retried —
            # report it cleanly instead of dumping a stack trace into the JSON stream.
            fail(f'Request failed: {exc}', fmt)
    raise AssertionError('unreachable')  # pragma: no cover


def resolve_label_id(service: Any, name: str, fmt: str) -> str:
    """Map a label *name* to its id (case-insensitive exact match).

    Querying by id rather than ``label:`` in the search string means nested
    ("Work/Invoices") and spaced label names need no quoting or escaping.
    """
    labels = execute(service.users().labels().list(userId='me'), fmt).get('labels', [])
    wanted = name.strip().lower()
    for label in labels:
        if str(label.get('name', '')).lower() == wanted:
            return str(label.get('id'))
    near = [
        str(label.get('name')) for label in labels if wanted in str(label.get('name', '')).lower()
    ]
    fail(f'Unknown label: {name}', fmt, didYouMean=near[:5])


# --------------------------------------------------------------------------
# Message shaping
# --------------------------------------------------------------------------

# Enough for a summary line plus attachment detection at format=metadata.
_SUMMARY_HEADERS = ['From', 'To', 'Cc', 'Subject', 'Date', 'Content-Disposition']


def headers_map(payload: dict[str, Any]) -> dict[str, str]:
    """Lower-cased header name -> value for one MIME part."""
    return {
        str(h.get('name', '')).lower(): str(h.get('value', ''))
        for h in payload.get('headers', []) or []
    }


def split_addresses(raw: str) -> list[str]:
    """Split an address header into entries.

    ``email.utils.getaddresses`` is what keeps ``"Doe, Jane" <j@x.com>`` a single
    entry — a naive ``split(',')`` would tear it in half.
    """
    if not raw:
        return []
    return [email.utils.formataddr(pair) for pair in email.utils.getaddresses([raw]) if any(pair)]


def parse_address(raw: str) -> tuple[str, str]:
    """Return ``(display_name, email)`` for the first address in a header."""
    pairs = email.utils.getaddresses([raw]) if raw else []
    if not pairs:
        return '', ''
    name, addr = pairs[0]
    return name, addr


def iso_from_internal_date(value: Any) -> str:
    """Gmail's ``internalDate`` (ms since epoch) -> ISO-8601 UTC, ``''`` if unusable."""
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return ''
    try:
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ''


def metadata_has_attachments(payload: dict[str, Any]) -> bool:
    """Walk a ``format=metadata`` MIME tree looking for an attachment part.

    Metadata format omits ``body.attachmentId``, so the only signals available
    are a non-empty ``filename`` and a ``Content-Disposition: attachment``
    header. ``read`` at ``format=full`` remains authoritative.
    """
    if not isinstance(payload, dict):
        return False
    if payload.get('filename'):
        return True
    disposition = headers_map(payload).get('content-disposition', '')
    if disposition.strip().lower().startswith('attachment'):
        return True
    return any(metadata_has_attachments(part) for part in payload.get('parts') or [])


def message_summary(msg: dict[str, Any]) -> dict[str, Any]:
    payload = msg.get('payload') or {}
    headers = headers_map(payload)
    raw_from = headers.get('from', '')
    from_name, from_email = parse_address(raw_from)
    return {
        'id': msg.get('id'),
        'threadId': msg.get('threadId'),
        'subject': headers.get('subject') or '(no subject)',
        'from': raw_from,
        'fromName': from_name,
        'fromEmail': from_email,
        'to': split_addresses(headers.get('to', '')),
        'cc': split_addresses(headers.get('cc', '')),
        'date': iso_from_internal_date(msg.get('internalDate')),
        'labelIds': msg.get('labelIds', []),
        'hasAttachments': metadata_has_attachments(payload),
        'snippet': msg.get('snippet', ''),
    }


def b64url_decode(data: str) -> bytes:
    """Decode Gmail's base64url payloads, which arrive without padding."""
    if not data:
        return b''
    padded = data + '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def decode_text(data: str) -> str:
    """Decode a base64url body part to text, never raising on bad input."""
    try:
        return b64url_decode(data).decode('utf-8', errors='replace')
    except (binascii.Error, ValueError):
        return ''


def collect_parts(
    payload: dict[str, Any],
    texts: list[tuple[str, str]],
    attachments: list[dict[str, Any]],
) -> None:
    """Depth-first walk of a ``format=full`` MIME tree.

    A part with both a filename and an ``attachmentId`` is an attachment, never
    body content — that is what keeps an attached .txt out of the rendered body.
    """
    if not isinstance(payload, dict):
        return
    body = payload.get('body') or {}
    filename = str(payload.get('filename') or '')
    mime = str(payload.get('mimeType') or '')

    if filename and body.get('attachmentId'):
        disposition = headers_map(payload).get('content-disposition', '').lower()
        attachments.append(
            {
                'id': body.get('attachmentId'),
                'name': filename,
                'contentType': mime,
                'size': body.get('size', 0),
                'isInline': 'inline' in disposition,
            }
        )
        return

    parts = payload.get('parts')
    if parts:
        for part in parts:
            collect_parts(part, texts, attachments)
        return

    # Anything that isn't plain text or HTML (calendar invites, signatures,
    # nested images without a filename) is not body content we can render.
    if mime in ('text/plain', 'text/html') and body.get('data'):
        texts.append((mime, decode_text(str(body['data']))))


def select_body(texts: list[tuple[str, str]], want: str) -> tuple[str, str]:
    """Pick the body to return. The reported type is what you *got*, not what you asked for."""
    plain = '\n'.join(text for mime, text in texts if mime == 'text/plain')
    html = '\n'.join(text for mime, text in texts if mime == 'text/html')
    if want == 'html':
        if html:
            return 'html', html
        return 'text', plain
    if plain:
        return 'text', plain
    if html:
        return 'html', html
    return 'text', ''


def message_full(msg: dict[str, Any], body_type: str, include_headers: bool) -> dict[str, Any]:
    payload = msg.get('payload') or {}
    headers = headers_map(payload)
    texts: list[tuple[str, str]] = []
    attachments: list[dict[str, Any]] = []
    collect_parts(payload, texts, attachments)
    content_type, body = select_body(texts, body_type)

    result = message_summary(msg)
    result.update(
        {
            'bcc': split_addresses(headers.get('bcc', '')),
            'replyTo': split_addresses(headers.get('reply-to', '')),
            'messageId': headers.get('message-id', ''),
            # At format=full the attachment parts are authoritative.
            'hasAttachments': bool(attachments),
            'bodyContentType': content_type,
            'body': body,
        }
    )
    if attachments:
        result['attachments'] = attachments
    if include_headers:
        # A list, not a dict: Received and friends legitimately repeat.
        result['headers'] = [
            {'name': h.get('name', ''), 'value': h.get('value', '')}
            for h in payload.get('headers', []) or []
        ]
    return result


# --------------------------------------------------------------------------
# Query construction and listing
# --------------------------------------------------------------------------


def gmail_date(value: str, flag: str, fmt: str) -> str:
    """Validate ``YYYY-MM-DD`` and rewrite to the ``YYYY/MM/DD`` Gmail expects."""
    try:
        return datetime.strptime(value, '%Y-%m-%d').strftime('%Y/%m/%d')
    except ValueError:
        fail(f'{flag} expects YYYY-MM-DD (got {value!r})', fmt)


def build_query(args: argparse.Namespace, fmt: str, base: str = '') -> str:
    """Assemble the Gmail ``q`` string from the flag sugar.

    Every filter is ANDed by juxtaposition, which is what Gmail's query language
    does with space-separated terms. Pure and offline on purpose: a malformed
    ``--after`` should fail before we spend a round trip on authentication.
    """
    parts: list[str] = []
    if base:
        parts.append(base)
    if getattr(args, 'unread', False):
        parts.append('is:unread')
    if getattr(args, 'has_attachments', False):
        parts.append('has:attachment')
    if getattr(args, 'sender', None):
        parts.append(f'from:{args.sender}')
    if getattr(args, 'to', None):
        parts.append(f'to:{args.to}')
    if getattr(args, 'after', None):
        parts.append(f'after:{gmail_date(args.after, "--after", fmt)}')
    if getattr(args, 'before', None):
        parts.append(f'before:{gmail_date(args.before, "--before", fmt)}')
    return ' '.join(parts).strip()


def resolve_label_filter(args: argparse.Namespace, service: Any, fmt: str) -> list[str] | None:
    if not getattr(args, 'label', None):
        return None
    return [resolve_label_id(service, args.label, fmt)]


def list_message_ids(
    service: Any,
    fmt: str,
    *,
    query: str,
    limit: int,
    page_token: str | None,
    label_ids: list[str] | None,
    include_spam_trash: bool,
) -> tuple[list[str], str | None]:
    """Page through ``messages.list`` until ``limit`` ids are collected."""
    ids: list[str] = []
    token = page_token
    while len(ids) < limit:
        response = execute(
            service.users()
            .messages()
            .list(
                userId='me',
                q=query or None,
                maxResults=min(500, limit - len(ids)),
                pageToken=token or None,
                labelIds=label_ids or None,
                includeSpamTrash=include_spam_trash,
            ),
            fmt,
        )
        ids.extend(str(m['id']) for m in response.get('messages', []) or [])
        token = response.get('nextPageToken')
        if not token:
            break
    return ids[:limit], token


def fetch_summaries(service: Any, ids: list[str], fmt: str) -> list[dict[str, Any]]:
    """One ``messages.get`` per id — the API has no bulk metadata fetch."""
    summaries = []
    for message_id in ids:
        msg = execute(
            service.users()
            .messages()
            .get(
                userId='me',
                id=message_id,
                format='metadata',
                metadataHeaders=_SUMMARY_HEADERS,
            ),
            fmt,
        )
        summaries.append(message_summary(msg))
    return summaries


def _messages_result(
    summaries: list[dict[str, Any]], query: str, token: str | None, **extra: Any
) -> dict[str, Any]:
    result: dict[str, Any] = {'messages': summaries, 'count': len(summaries), 'query': query}
    result.update(extra)
    if token:
        result['nextPageToken'] = token
    return result


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_profile(args: argparse.Namespace, fmt: str) -> None:
    service = gmail_service(fmt)
    profile = execute(service.users().getProfile(userId='me'), fmt)
    emit(
        {
            'emailAddress': profile.get('emailAddress'),
            'messagesTotal': profile.get('messagesTotal'),
            'threadsTotal': profile.get('threadsTotal'),
            'historyId': profile.get('historyId'),
        },
        fmt,
    )


def cmd_labels(args: argparse.Namespace, fmt: str) -> None:
    service = gmail_service(fmt)
    labels = execute(service.users().labels().list(userId='me'), fmt).get('labels', [])
    emit({'labels': labels, 'count': len(labels)}, fmt)


def cmd_list(args: argparse.Namespace, fmt: str) -> None:
    query = build_query(args, fmt)
    service = gmail_service(fmt)
    label_ids = resolve_label_filter(args, service, fmt)
    ids, token = list_message_ids(
        service,
        fmt,
        query=query,
        limit=args.limit,
        page_token=args.page_token,
        label_ids=label_ids,
        include_spam_trash=args.include_spam_trash,
    )
    emit(_messages_result(fetch_summaries(service, ids, fmt), query, token), fmt)


def cmd_search(args: argparse.Namespace, fmt: str) -> None:
    base = args.query or ''
    if base and args.subject_only:
        # Wrap before the flag sugar is appended, so `--subject-only --unread`
        # means subject:(term) AND is:unread, not subject:(term is:unread).
        base = f'subject:({base})'
    query = build_query(args, fmt, base=base)
    if not query and not args.label:
        fail('Provide a search query or at least one filter', fmt)
    service = gmail_service(fmt)
    label_ids = resolve_label_filter(args, service, fmt)
    ids, token = list_message_ids(
        service,
        fmt,
        query=query,
        limit=args.limit,
        page_token=args.page_token,
        label_ids=label_ids,
        include_spam_trash=args.include_spam_trash,
    )
    emit(
        _messages_result(fetch_summaries(service, ids, fmt), query, token, searchMethod='gmail_q'),
        fmt,
    )


def cmd_read(args: argparse.Namespace, fmt: str) -> None:
    service = gmail_service(fmt)
    missing = f'No such message: {args.message_id}'

    if args.raw or args.save_eml:
        msg = execute(
            service.users().messages().get(userId='me', id=args.message_id, format='raw'),
            fmt,
            not_found=missing,
        )
        try:
            raw_bytes = b64url_decode(str(msg.get('raw', '')))
        except (binascii.Error, ValueError) as exc:
            fail(f'Could not decode the raw message: {exc}', fmt)

        if args.save_eml:
            path = Path(args.save_eml).expanduser()
            if path.is_dir():
                path = path / f'{args.message_id}.eml'
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw_bytes)
            except OSError as exc:
                fail(f'Could not write {path}: {exc}', fmt)
            emit(
                {
                    'status': 'saved',
                    'path': str(path),
                    'size': len(raw_bytes),
                    'id': msg.get('id'),
                    'threadId': msg.get('threadId'),
                },
                fmt,
            )
            return

        emit(
            {
                'id': msg.get('id'),
                'threadId': msg.get('threadId'),
                'raw': raw_bytes.decode('utf-8', errors='replace'),
            },
            fmt,
        )
        return

    msg = execute(
        service.users().messages().get(userId='me', id=args.message_id, format='full'),
        fmt,
        not_found=missing,
    )
    emit(message_full(msg, args.body_type, args.headers), fmt)


def cmd_thread(args: argparse.Namespace, fmt: str) -> None:
    service = gmail_service(fmt)
    thread = execute(
        service.users()
        .threads()
        .get(
            userId='me',
            id=args.thread_id,
            format='metadata',
            metadataHeaders=_SUMMARY_HEADERS,
        ),
        fmt,
        not_found=f'No such thread: {args.thread_id}',
    )
    summaries = [message_summary(msg) for msg in thread.get('messages', []) or []]
    emit(
        {
            'id': thread.get('id'),
            'historyId': thread.get('historyId'),
            'messages': summaries,
            'count': len(summaries),
        },
        fmt,
    )


def sanitize_filename(name: str) -> str:
    """Reduce an attachment's declared filename to a safe basename.

    A hostile (or merely sloppy) sender controls this string, so strip anything
    that could escape the directory the user asked to write into.
    """
    candidate = name.replace('\\', '/').split('/')[-1].replace('\x00', '').strip()
    return '' if candidate in ('', '.', '..') else candidate


def attachment_filename(service: Any, message_id: str, attachment_id: str, fmt: str) -> str:
    """Look up the part's real filename. Costs one extra ``messages.get``."""
    msg = execute(
        service.users().messages().get(userId='me', id=message_id, format='full'),
        fmt,
        not_found=f'No such message: {message_id}',
    )
    texts: list[tuple[str, str]] = []
    attachments: list[dict[str, Any]] = []
    collect_parts(msg.get('payload') or {}, texts, attachments)
    for att in attachments:
        if str(att.get('id')) == attachment_id:
            return sanitize_filename(str(att.get('name') or ''))
    return ''


def cmd_attachment(args: argparse.Namespace, fmt: str) -> None:
    service = gmail_service(fmt)
    att = execute(
        service.users()
        .messages()
        .attachments()
        .get(userId='me', messageId=args.message_id, id=args.attachment_id),
        fmt,
        not_found=f'No such attachment {args.attachment_id} on message {args.message_id}',
    )

    if not args.save:
        emit(
            {
                'id': args.attachment_id,
                'size': att.get('size', 0),
                'hint': 'Re-run with --save PATH (a file or a directory) to write the bytes.',
            },
            fmt,
        )
        return

    try:
        payload = b64url_decode(str(att.get('data', '')))
    except (binascii.Error, ValueError) as exc:
        fail(f'Could not decode the attachment: {exc}', fmt)

    target = Path(args.save).expanduser()
    if target.is_dir() or args.save.endswith(('/', os.sep)):
        name = attachment_filename(service, args.message_id, args.attachment_id, fmt)
        target = target / (name or f'attachment_{args.attachment_id[:12]}')
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    except OSError as exc:
        fail(f'Could not write {target}: {exc}', fmt)

    emit({'status': 'saved', 'path': str(target), 'size': len(payload)}, fmt)


def cmd_history(args: argparse.Namespace, fmt: str) -> None:
    service = gmail_service(fmt)
    records: list[dict[str, Any]] = []
    token = args.page_token
    latest_history_id = None

    while len(records) < args.limit:
        response = execute(
            service.users()
            .history()
            .list(
                userId='me',
                startHistoryId=args.start_history_id,
                maxResults=min(500, args.limit - len(records)),
                pageToken=token or None,
            ),
            fmt,
            not_found=(
                f'History id {args.start_history_id} is no longer available '
                '(Gmail keeps roughly a week). Re-sync with: gmail-cli list'
            ),
        )
        latest_history_id = response.get('historyId', latest_history_id)
        records.extend(response.get('history', []) or [])
        token = response.get('nextPageToken')
        if not token:
            break

    result: dict[str, Any] = {
        'startHistoryId': args.start_history_id,
        'historyId': latest_history_id,
        'history': records[: args.limit],
        'count': len(records[: args.limit]),
    }
    if token:
        result['nextPageToken'] = token
    emit(result, fmt)


def bundled_skill_text() -> str | None:
    """Return the packaged SKILL.md, or None if this install doesn't carry one.

    The wheel build copies ``.claude/skills/gmail-cli/SKILL.md`` to
    ``gmail_cli/_skill/SKILL.md``. Editable installs and `uv run` have no such
    copy, so fall back to walking up from this file to the in-repo original.
    """
    try:
        bundled = resources.files('gmail_cli').joinpath('_skill/SKILL.md')
        if bundled.is_file():
            return bundled.read_text(encoding='utf-8')
    except (FileNotFoundError, ModuleNotFoundError, AttributeError, OSError):
        pass

    for parent in Path(__file__).resolve().parents:
        candidate = parent / '.claude' / 'skills' / _SKILL_NAME / 'SKILL.md'
        if candidate.is_file():
            return candidate.read_text(encoding='utf-8')
    return None


def cmd_install_skill(args: argparse.Namespace, fmt: str) -> None:
    text = bundled_skill_text()
    if text is None:
        fail(
            'SKILL.md is not present in this install. Reinstall with: '
            'uv tool install --force gmail-cli',
            fmt,
        )

    if args.target:
        skills_dir = Path(args.target).expanduser()
        scope = 'custom'
    elif args.scope == 'user':
        skills_dir = Path.home() / '.claude' / 'skills'
        scope = 'user'
    else:
        skills_dir = Path.cwd() / '.claude' / 'skills'
        scope = 'project'

    dest = skills_dir / _SKILL_NAME / 'SKILL.md'
    existed = dest.exists()
    if existed and not args.force:
        fail(f'{dest} already exists. Re-run with --force to overwrite.', fmt)

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding='utf-8')
    except OSError as exc:
        fail(f'Could not write {dest}: {exc}', fmt)

    emit(
        {
            'path': str(dest),
            'scope': scope,
            'overwritten': existed,
            'bytes': len(text.encode('utf-8')),
        },
        fmt,
    )


def cmd_configure(args: argparse.Namespace, fmt: str) -> None:
    config = load_config()
    if args.credentials:
        path = Path(args.credentials).expanduser()
        if not path.is_file():
            fail(f'No such credentials file: {path}', fmt)
        config['credentials_path'] = str(path.resolve())
    if args.default_format:
        config['default_format'] = args.default_format
    save_config(config)
    emit({'status': 'ok', 'config': config}, fmt)


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='gmail-cli',
        description=(
            'Strictly read-only Gmail client. Requests only the '
            'gmail.readonly scope — it cannot send, label, archive or delete.'
        ),
    )
    parser.add_argument('--version', action='version', version=f'gmail-cli {__version__}')
    parser.add_argument(
        '--output',
        '-o',
        choices=('json', 'text'),
        default=None,
        help='Output format (default: json, or config.default_format).',
    )
    # The same flag is offered again on every subcommand, so both
    # `gmail-cli -o text profile` and `gmail-cli profile -o text` work. SUPPRESS
    # is what makes that safe: when the subcommand doesn't carry the flag it
    # leaves the namespace alone instead of overwriting the global value.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        '--output',
        '-o',
        dest='output',
        choices=('json', 'text'),
        default=argparse.SUPPRESS,
        help='Output format (default: json, or config.default_format).',
    )

    sub = parser.add_subparsers(dest='command', metavar='COMMAND')
    sub_kwargs: dict[str, Any] = {'parents': [shared]}

    p_configure = sub.add_parser(
        'configure', help='Set credentials path and default output format.', **sub_kwargs
    )
    p_configure.add_argument('--credentials', metavar='PATH', help='Path to the OAuth client JSON.')
    p_configure.add_argument(
        '--default-format', choices=('json', 'text'), help='Default output format.'
    )

    p_login = sub.add_parser(
        'login', help='Authorize this machine (read-only scope).', **sub_kwargs
    )
    p_login.add_argument(
        '--force', action='store_true', help='Re-authenticate even if a valid token is cached.'
    )
    p_login.add_argument(
        '--port',
        type=int,
        default=0,
        help=(
            'Redirect port. 0 (default) auto-assigns for the local-server flow; '
            f'{_MANUAL_DEFAULT_PORT} is used for --manual/--print-url.'
        ),
    )
    p_login.add_argument(
        '--manual',
        action='store_true',
        help='Print the consent URL and wait for the code on stdin (one process).',
    )
    p_login.add_argument(
        '--print-url',
        action='store_true',
        help='Print the consent URL and exit; finish later with --code (two processes).',
    )
    p_login.add_argument(
        '--code',
        metavar='VALUE',
        help='Finish a --print-url/--manual login. Accepts the code or the whole redirect URL.',
    )

    sub.add_parser(
        'logout', help='Delete the cached token (does not revoke server-side).', **sub_kwargs
    )
    sub.add_parser(
        'profile',
        help='Show the authorized account and its message/thread totals.',
        **sub_kwargs,
    )
    sub.add_parser('labels', help='List all labels with their ids.', **sub_kwargs)

    p_list = sub.add_parser(
        'list', help='List messages, newest first, with optional filters.', **sub_kwargs
    )
    _add_filter_args(p_list)

    p_search = sub.add_parser(
        'search',
        help='Search with a raw Gmail query (from: subject: has:attachment older_than:1y ...).',
        **sub_kwargs,
    )
    p_search.add_argument('query', nargs='?', metavar='QUERY', help='Raw Gmail search query.')
    p_search.add_argument(
        '--subject-only',
        action='store_true',
        help='Match QUERY against the subject only (wraps it as subject:(QUERY)).',
    )
    _add_filter_args(p_search)

    p_read = sub.add_parser('read', help='Read one message, body included.', **sub_kwargs)
    p_read.add_argument('message_id', metavar='MESSAGE_ID')
    p_read.add_argument(
        '--body-type',
        choices=('text', 'html'),
        default='text',
        help='Preferred body flavour; falls back to the other when absent (text).',
    )
    p_read.add_argument('--headers', action='store_true', help='Include every header, verbatim.')
    p_read.add_argument('--raw', action='store_true', help='Return the RFC822 source instead.')
    p_read.add_argument(
        '--save-eml', metavar='PATH', help='Write the RFC822 source to PATH (or PATH/<id>.eml).'
    )

    p_thread = sub.add_parser('thread', help='Summarize every message in a thread.', **sub_kwargs)
    p_thread.add_argument('thread_id', metavar='THREAD_ID')

    p_attachment = sub.add_parser(
        'attachment', help='Inspect or download an attachment.', **sub_kwargs
    )
    p_attachment.add_argument('message_id', metavar='MSG_ID')
    p_attachment.add_argument('attachment_id', metavar='ATT_ID')
    p_attachment.add_argument(
        '--save',
        metavar='PATH',
        help='Write to PATH. A directory (or trailing /) keeps the sender-supplied filename.',
    )

    p_history = sub.add_parser(
        'history',
        help='Replay mailbox changes since a history id (from profile).',
        **sub_kwargs,
    )
    p_history.add_argument(
        '--start-history-id',
        required=True,
        metavar='N',
        help='History id to start from — take it from `gmail-cli profile`.',
    )
    p_history.add_argument(
        '--limit', type=int, default=100, help='Maximum history records to return (100).'
    )
    p_history.add_argument('--page-token', metavar='TOKEN', help='Continue from a previous page.')

    p_skill = sub.add_parser(
        'install-skill',
        help='Write the bundled Claude Code skill into a skills directory.',
        **sub_kwargs,
    )
    p_skill.add_argument(
        '--scope',
        choices=('project', 'user'),
        default='project',
        help='project -> ./.claude/skills (default); user -> ~/.claude/skills.',
    )
    p_skill.add_argument(
        '--target',
        metavar='DIR',
        help='Explicit skills directory; overrides --scope. Writes <DIR>/gmail-cli/SKILL.md.',
    )
    p_skill.add_argument(
        '--force', '-f', action='store_true', help='Overwrite an existing SKILL.md.'
    )

    return parser


def _add_filter_args(parser: argparse.ArgumentParser) -> None:
    """Shared query sugar for `list` and `search`. All filters AND together."""
    parser.add_argument('--limit', type=int, default=25, help='Maximum messages to return (25).')
    parser.add_argument('--page-token', metavar='TOKEN', help='Continue from a previous page.')
    parser.add_argument('--unread', action='store_true', help='Only unread messages.')
    parser.add_argument(
        '--has-attachments', action='store_true', help='Only messages with attachments.'
    )
    parser.add_argument('--from', dest='sender', metavar='ADDR', help='Match the sender.')
    parser.add_argument('--to', metavar='ADDR', help='Match a recipient.')
    parser.add_argument('--label', metavar='NAME', help='Restrict to a label, by name.')
    parser.add_argument('--after', metavar='YYYY-MM-DD', help='On or after this date.')
    parser.add_argument('--before', metavar='YYYY-MM-DD', help='Before this date.')
    parser.add_argument(
        '--include-spam-trash', action='store_true', help='Also search Spam and Trash.'
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return EXIT_OK

    fmt = resolve_format(args.output)

    handlers = {
        'configure': cmd_configure,
        'login': cmd_login,
        'logout': cmd_logout,
        'profile': cmd_profile,
        'labels': cmd_labels,
        'list': cmd_list,
        'search': cmd_search,
        'read': cmd_read,
        'thread': cmd_thread,
        'attachment': cmd_attachment,
        'history': cmd_history,
        'install-skill': cmd_install_skill,
    }
    handler = handlers.get(args.command)
    if handler is None:
        fail(f'Unknown command: {args.command}', fmt)
    handler(args, fmt)
    return EXIT_OK


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
