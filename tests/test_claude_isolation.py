"""Claude Code roles run as scoped sessions, not as the owner's whole Claude Code.

Reported on a real project: every Claude role inherited the owner's global
setup (324 tools, 128 MCP servers, 291 skills, 31 plugins). The reviewer spent
318K tokens before reading a file and stopped with SCOPE_TOO_LARGE_FOR_REVIEW,
and roles could see mail, database and docs tools no role was given.
"""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import agent_cc  # noqa: E402
from adw_modules.data_types import CCRequest  # noqa: E402

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
record = {'argv': args, 'env': {k: os.environ.get(k) for k in ('ENABLE_CLAUDEAI_MCP_SERVERS',)}}
if '--settings' in args:
    record['settings'] = json.loads(Path(args[args.index('--settings') + 1]).read_text())
Path(os.environ['FAKE_CLAUDE_LOG']).write_text(json.dumps(record))
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '{}',
                  'session_id': 'sid', 'total_cost_usd': 0}))
'''

MARKERS = ('USER_CLAUDE_MD_MARKER', 'demo-user-skill', 'demo-user-agent', 'leak_tool')

MCP_SERVER = r'''import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if 'id' not in m:
        continue
    if m['method'] == 'initialize':
        r = {'protocolVersion': m['params']['protocolVersion'], 'capabilities': {'tools': {}},
             'serverInfo': {'name': 'demo', 'version': '1'}}
    elif m['method'] == 'tools/list':
        r = {'tools': [{'name': 'leak_tool', 'description': 'USER MCP TOOL',
                        'inputSchema': {'type': 'object', 'properties': {}}}]}
    else:
        r = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': r}), flush=True)
'''


def request(base, cwd):
    return CCRequest(prompt='say ok', system_prompt='sys', model='sonnet', session_id=str(uuid.uuid4()),
                     session_dir=str(base / 'sessions'), raw_output_path=str(base / 'attempt' / 'raw_output.jsonl'),
                     tools=['read', 'bash'], cwd=str(cwd))


class IsolationArgvTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='kh-iso-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        (self.base / 'work').mkdir()
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(agent_cc.INHERIT_ENV, None)

    def run_fake(self):
        fake = self.base / 'claude'
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o700)
        log = self.base / 'claude.json'
        with patch.object(agent_cc, 'CC_PATH', str(fake)), patch.dict(os.environ, {'FAKE_CLAUDE_LOG': str(log)}):
            agent_cc.run(request(self.base, self.base / 'work'))
        return json.loads(log.read_text())

    def test_every_send_is_isolated_by_default(self):
        record = self.run_fake()
        argv = record['argv']
        self.assertIn('--strict-mcp-config', argv)
        self.assertEqual(argv[argv.index('--setting-sources') + 1], 'project,local')
        self.assertIn('--disable-slash-commands', argv)
        self.assertIn('--no-chrome', argv)
        self.assertLess(argv.index('--strict-mcp-config'), argv.index('--'), 'flags must precede the prompt')
        self.assertEqual(record['settings']['disableClaudeAiConnectors'], True)
        self.assertTrue(record['settings']['sandbox']['enabled'])
        self.assertEqual(record['env']['ENABLE_CLAUDEAI_MCP_SERVERS'], 'false')

    def test_owner_can_opt_back_into_the_inherited_setup(self):
        for value in ('1', 'true', 'YES'):
            with self.subTest(value=value), patch.dict(os.environ, {agent_cc.INHERIT_ENV: value}):
                record = self.run_fake()
                for flag in agent_cc.ISOLATION_FLAGS:
                    if flag.startswith('--'):
                        self.assertNotIn(flag, record['argv'])
                self.assertNotIn('disableClaudeAiConnectors', record['settings'])
                self.assertTrue(record['settings']['sandbox']['enabled'])
                self.assertIsNone(record['env']['ENABLE_CLAUDEAI_MCP_SERVERS'])
        with patch.dict(os.environ, {agent_cc.INHERIT_ENV: '0'}):
            self.assertIn('--strict-mcp-config', self.run_fake()['argv'])


class Recorder(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('content-length', 0)))
        if 'count_tokens' in self.path:
            return self.reply(b'{"input_tokens":10}', 'application/json')
        body = json.loads(raw)
        text = raw.decode('utf-8', 'replace')
        Recorder.requests.append({'tools': [t.get('name') for t in body.get('tools', [])],
                                  'markers': [m for m in MARKERS if m in text]})
        usage = {'input_tokens': 10, 'output_tokens': 1, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}
        events = [
            ('message_start', {'type': 'message_start', 'message': {
                'id': 'm', 'type': 'message', 'role': 'assistant', 'model': 'claude-sonnet-4-5', 'content': [],
                'stop_reason': None, 'stop_sequence': None, 'usage': usage}}),
            ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}),
            ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'ok'}}),
            ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
            ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
                               'usage': {'output_tokens': 1}}),
            ('message_stop', {'type': 'message_stop'})]
        self.reply(''.join(f'event: {n}\ndata: {json.dumps(d)}\n\n' for n, d in events).encode(), 'text/event-stream')

    def reply(self, data, kind):
        self.send_response(200)
        self.send_header('content-type', kind)
        self.send_header('content-length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(shutil.which(agent_cc.CC_PATH) and os.environ.get('KH_REAL_CLAUDE_PROBE', '1') != '0',
                     'needs the claude CLI')
class RealClaudeIsolationProbe(unittest.TestCase):
    """The real CLI, a planted user setup, and a local endpoint that records each request.

    Nothing leaves the machine: the CLI talks to 127.0.0.1 with a dummy key.
    """

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='kh-iso-real-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        home = self.base / 'home'
        (home / '.claude/skills/demo-user-skill').mkdir(parents=True)
        (home / '.claude/skills/demo-user-skill/SKILL.md').write_text(
            '---\nname: demo-user-skill\ndescription: demo-user-skill planted user skill\n---\nbody\n')
        (home / '.claude/agents').mkdir()
        (home / '.claude/agents/demo-user-agent.md').write_text(
            '---\nname: demo-user-agent\ndescription: demo-user-agent planted user agent\n---\nbody\n')
        (home / '.claude/CLAUDE.md').write_text('USER_CLAUDE_MD_MARKER\n')
        (self.base / 'mcp.py').write_text(MCP_SERVER)
        (home / '.claude.json').write_text(json.dumps({'hasCompletedOnboarding': True, 'mcpServers': {
            'demo': {'type': 'stdio', 'command': sys.executable, 'args': [str(self.base / 'mcp.py')]}}}))
        (self.base / 'work').mkdir()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(home), 'TMPDIR': os.environ['TMPDIR'],
                    'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{self.server.server_address[1]}',
                    'ANTHROPIC_API_KEY': 'sk-ant-test-not-a-key', 'DISABLE_AUTOUPDATER': '1',
                    'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'}

    def send(self, inherit):
        Recorder.requests = []
        env = dict(self.env)
        if inherit:
            env[agent_cc.INHERIT_ENV] = '1'
        with patch.dict(os.environ, env, clear=True):
            try:
                agent_cc.run(request(self.base / ('inherit' if inherit else 'isolated'), self.base / 'work'))
            except RuntimeError as error:
                self.skipTest(f'claude CLI could not run against the local endpoint: {error}')
        self.assertTrue(Recorder.requests, 'the CLI made no model request')
        main = max(Recorder.requests, key=lambda r: len(r['tools']) + len(r['markers']))
        return set(main['markers']) | {t for t in main['tools'] if 'leak' in t}

    def test_planted_user_setup_reaches_the_model_only_when_inherited(self):
        inherited = self.send(inherit=True)
        if not inherited:
            self.skipTest('this claude CLI did not load the planted user setup even without isolation')
        self.assertTrue({'USER_CLAUDE_MD_MARKER', 'leak_tool'} & inherited, inherited)
        self.assertEqual(self.send(inherit=False), set(), 'user setup leaked into an isolated role')


if __name__ == '__main__':
    unittest.main()
