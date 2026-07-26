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
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

from gmail_cli import __version__

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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return EXIT_OK

    fmt = resolve_format(args.output)

    if args.command == 'configure':
        cmd_configure(args, fmt)
        return EXIT_OK

    fail(f'Unknown command: {args.command}', fmt)


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
