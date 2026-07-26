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
import contextlib
import json
import os
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

from gmail_cli import __version__

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    _IMPORT_ERROR: str | None = None
except ImportError as exc:  # pragma: no cover - exercised only on a broken install
    _IMPORT_ERROR = str(exc)

# The single scope this tool ever requests. Read-only is enforced by Google,
# not merely by the absence of mutating subcommands.
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

EXIT_OK = 0
EXIT_ERROR = 1

_STATE_DIR_NAME = '.gmail-cli'
_INSTALL_HINT = (
    'Missing dependencies. Install with: '
    'uv tool install gmail-cli  (or: pip install google-auth google-auth-oauthlib '
    'google-api-python-client)'
)

_SETUP_GUIDE = """\
No OAuth client found. One-time Google Cloud setup:

  1. Open https://console.cloud.google.com/ and create (or pick) a project.
  2. APIs & Services -> Library -> enable "Gmail API".
  3. APIs & Services -> OAuth consent screen -> External -> add yourself as a
     test user. Add the scope https://www.googleapis.com/auth/gmail.readonly
  4. APIs & Services -> Credentials -> Create credentials -> OAuth client ID ->
     Application type: "Desktop app" -> Download JSON.
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
            f'{marker} {_format_date(msg.get("date", ""))}  '
            f'{_truncate(str(sender), 30)}  {_truncate(str(msg.get("subject", "")), 60)}'
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
# Commands
# --------------------------------------------------------------------------


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
    sub = parser.add_subparsers(dest='command', metavar='COMMAND')

    p_configure = sub.add_parser(
        'configure', help='Set credentials path and default output format.'
    )
    p_configure.add_argument('--credentials', metavar='PATH', help='Path to the OAuth client JSON.')
    p_configure.add_argument(
        '--default-format', choices=('json', 'text'), help='Default output format.'
    )

    p_login = sub.add_parser('login', help='Authorize this machine (read-only scope).')
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

    sub.add_parser('logout', help='Delete the cached token (does not revoke server-side).')

    return parser


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
    }
    handler = handlers.get(args.command)
    if handler is None:
        fail(f'Unknown command: {args.command}', fmt)
    handler(args, fmt)
    return EXIT_OK


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
