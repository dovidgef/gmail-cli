"""Offline tests for gmail-cli.

Nothing here touches the network or needs credentials: these cover the pure
functions — path resolution, the read-only scope, base64url decoding, header
parsing and body selection — that everything else is built on.
"""

from __future__ import annotations

import argparse
import base64
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from gmail_cli import cli


def _b64(text: str) -> str:
    """Encode the way Gmail does: base64url, padding stripped."""
    return base64.urlsafe_b64encode(text.encode('utf-8')).decode('ascii').rstrip('=')


class ScopeTests(unittest.TestCase):
    def test_scope_is_exactly_readonly(self) -> None:
        # The read-only guarantee is this list and nothing else.
        self.assertEqual(cli.SCOPES, ['https://www.googleapis.com/auth/gmail.readonly'])

    def test_no_mutating_scope_anywhere(self) -> None:
        source = Path(cli.__file__).read_text(encoding='utf-8')
        for forbidden in ('gmail.send', 'gmail.modify', 'gmail.compose', 'gmail.labels'):
            self.assertNotIn(forbidden, source)


class PathTests(unittest.TestCase):
    def test_state_paths_live_under_home(self) -> None:
        with mock.patch.object(Path, 'home', return_value=Path('/home/tester')):
            self.assertEqual(cli.state_dir(), Path('/home/tester/.gmail-cli'))
            self.assertEqual(cli.token_path(), Path('/home/tester/.gmail-cli/token_cache.json'))
            self.assertEqual(cli.config_path(), Path('/home/tester/.gmail-cli/config.json'))
            self.assertEqual(
                cli.manual_state_path(),
                Path('/home/tester/.gmail-cli/manual-flow-state.json'),
            )

    def test_env_var_beats_config_and_default(self) -> None:
        with (
            mock.patch.dict('os.environ', {'GMAIL_CLI_CREDENTIALS': '/tmp/from-env.json'}),
            mock.patch.object(
                cli, 'load_config', return_value={'credentials_path': '/tmp/cfg.json'}
            ),
        ):
            self.assertEqual(cli.resolve_credentials_path(), Path('/tmp/from-env.json'))

    def test_config_beats_default(self) -> None:
        with (
            mock.patch.dict('os.environ', {}, clear=True),
            mock.patch.object(
                cli, 'load_config', return_value={'credentials_path': '/tmp/cfg.json'}
            ),
        ):
            self.assertEqual(cli.resolve_credentials_path(), Path('/tmp/cfg.json'))

    def test_default_when_nothing_configured(self) -> None:
        with (
            mock.patch.dict('os.environ', {}, clear=True),
            mock.patch.object(cli, 'load_config', return_value={}),
            mock.patch.object(Path, 'home', return_value=Path('/home/tester')),
        ):
            self.assertEqual(
                cli.resolve_credentials_path(), Path('/home/tester/.gmail-cli/credentials.json')
            )

    def test_token_cache_is_written_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'nested' / 'token_cache.json'
            cli.write_private(path, '{"token": "x"}')
            self.assertEqual(path.read_text(encoding='utf-8'), '{"token": "x"}')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class Base64Tests(unittest.TestCase):
    def test_decodes_without_padding(self) -> None:
        # Gmail strips '='; a naive b64decode would raise on these.
        self.assertEqual(cli.b64url_decode(_b64('a')), b'a')
        self.assertEqual(cli.b64url_decode(_b64('ab')), b'ab')
        self.assertEqual(cli.b64url_decode(_b64('abc')), b'abc')

    def test_decodes_url_safe_alphabet(self) -> None:
        raw = bytes([251, 255, 190])
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip('=')
        self.assertIn('-', encoded + '_')
        self.assertEqual(cli.b64url_decode(encoded), raw)

    def test_empty_is_empty(self) -> None:
        self.assertEqual(cli.b64url_decode(''), b'')

    def test_decode_text_replaces_bad_bytes(self) -> None:
        broken = base64.urlsafe_b64encode(b'ok \xff\xfe').decode().rstrip('=')
        self.assertTrue(cli.decode_text(broken).startswith('ok '))


class HeaderTests(unittest.TestCase):
    payload: ClassVar[dict[str, object]] = {
        'headers': [
            {'name': 'Subject', 'value': 'Quarterly report'},
            {'name': 'From', 'value': '"Doe, Jane" <jane@example.com>'},
            {'name': 'To', 'value': 'a@example.com, "Roe, Rich" <rich@example.com>'},
        ]
    }

    def test_headers_are_lowercased(self) -> None:
        headers = cli.headers_map(self.payload)
        self.assertEqual(headers['subject'], 'Quarterly report')
        self.assertEqual(headers['from'], '"Doe, Jane" <jane@example.com>')

    def test_comma_in_display_name_stays_one_address(self) -> None:
        addresses = cli.split_addresses(cli.headers_map(self.payload)['to'])
        self.assertEqual(addresses, ['a@example.com', '"Roe, Rich" <rich@example.com>'])

    def test_parse_address_splits_name_and_email(self) -> None:
        self.assertEqual(
            cli.parse_address('"Doe, Jane" <jane@example.com>'), ('Doe, Jane', 'jane@example.com')
        )
        self.assertEqual(cli.parse_address('bare@example.com'), ('', 'bare@example.com'))
        self.assertEqual(cli.parse_address(''), ('', ''))

    def test_missing_subject_falls_back(self) -> None:
        summary = cli.message_summary({'id': 'm', 'payload': {'headers': []}})
        self.assertEqual(summary['subject'], '(no subject)')

    def test_internal_date_becomes_iso_utc(self) -> None:
        self.assertEqual(cli.iso_from_internal_date('1700000000000'), '2023-11-14T22:13:20+00:00')
        self.assertEqual(cli.iso_from_internal_date(None), '')
        self.assertEqual(cli.iso_from_internal_date('not-a-number'), '')


class AttachmentDetectionTests(unittest.TestCase):
    def test_metadata_filename_marks_attachment(self) -> None:
        payload = {'parts': [{'mimeType': 'application/pdf', 'filename': 'invoice.pdf'}]}
        self.assertTrue(cli.metadata_has_attachments(payload))

    def test_metadata_content_disposition_marks_attachment(self) -> None:
        payload = {
            'parts': [
                {
                    'mimeType': 'application/octet-stream',
                    'filename': '',
                    'headers': [{'name': 'Content-Disposition', 'value': 'attachment'}],
                }
            ]
        }
        self.assertTrue(cli.metadata_has_attachments(payload))

    def test_plain_message_has_none(self) -> None:
        payload = {'mimeType': 'text/plain', 'filename': '', 'body': {'data': _b64('hi')}}
        self.assertFalse(cli.metadata_has_attachments(payload))

    def test_full_format_attachment_is_not_body(self) -> None:
        payload = {
            'mimeType': 'multipart/mixed',
            'parts': [
                {'mimeType': 'text/plain', 'filename': '', 'body': {'data': _b64('body text')}},
                {
                    'mimeType': 'text/plain',
                    'filename': 'notes.txt',
                    'body': {'attachmentId': 'ATT1', 'size': 12},
                },
            ],
        }
        texts: list[tuple[str, str]] = []
        attachments: list[dict[str, object]] = []
        cli.collect_parts(payload, texts, attachments)
        self.assertEqual(texts, [('text/plain', 'body text')])
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]['name'], 'notes.txt')
        self.assertFalse(attachments[0]['isInline'])

    def test_sanitize_filename_strips_traversal(self) -> None:
        self.assertEqual(cli.sanitize_filename('../../etc/passwd'), 'passwd')
        self.assertEqual(cli.sanitize_filename('report.pdf'), 'report.pdf')
        self.assertEqual(cli.sanitize_filename('..'), '')


class BodySelectionTests(unittest.TestCase):
    both: ClassVar[list[tuple[str, str]]] = [('text/plain', 'PLAIN'), ('text/html', '<p>HTML</p>')]

    def test_text_preferred(self) -> None:
        self.assertEqual(cli.select_body(self.both, 'text'), ('text', 'PLAIN'))

    def test_html_preferred(self) -> None:
        self.assertEqual(cli.select_body(self.both, 'html'), ('html', '<p>HTML</p>'))

    def test_text_falls_back_to_html(self) -> None:
        self.assertEqual(cli.select_body([('text/html', 'H')], 'text'), ('html', 'H'))

    def test_html_falls_back_to_text_and_reports_text(self) -> None:
        # The reported type is what came back, not what was asked for.
        self.assertEqual(cli.select_body([('text/plain', 'P')], 'html'), ('text', 'P'))

    def test_empty_message(self) -> None:
        self.assertEqual(cli.select_body([], 'text'), ('text', ''))

    def test_multiple_chunks_are_joined(self) -> None:
        chunks = [('text/plain', 'one'), ('text/plain', 'two')]
        self.assertEqual(cli.select_body(chunks, 'text'), ('text', 'one\ntwo'))


class ParserTests(unittest.TestCase):
    """--output is accepted before OR after the subcommand."""

    def test_global_position(self) -> None:
        args = cli.build_parser().parse_args(['-o', 'text', 'profile'])
        self.assertEqual(args.output, 'text')

    def test_subcommand_position(self) -> None:
        args = cli.build_parser().parse_args(['profile', '-o', 'text'])
        self.assertEqual(args.output, 'text')

    def test_unset_stays_none(self) -> None:
        # None (not a literal 'json') so config.default_format can still win.
        args = cli.build_parser().parse_args(['profile'])
        self.assertIsNone(args.output)

    def test_subcommand_flag_does_not_clobber_global(self) -> None:
        args = cli.build_parser().parse_args(['-o', 'text', 'list'])
        self.assertEqual(args.output, 'text')


class QueryTests(unittest.TestCase):
    def test_flags_and_together(self) -> None:
        args = argparse.Namespace(
            unread=True, has_attachments=True, sender='a@x.com', to=None, after=None, before=None
        )
        self.assertEqual(cli.build_query(args, 'json'), 'is:unread has:attachment from:a@x.com')

    def test_dates_are_rewritten_for_gmail(self) -> None:
        self.assertEqual(cli.gmail_date('2024-03-11', '--after', 'json'), '2024/03/11')

    def test_malformed_date_exits_one(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            cli.gmail_date('2024-13-99', '--after', 'json')
        self.assertEqual(caught.exception.code, cli.EXIT_ERROR)


if __name__ == '__main__':
    unittest.main()
