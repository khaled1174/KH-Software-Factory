"""Actual subprocess protocol, read-only prompt and gate regressions."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
from contextlib import contextmanager
from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
ADWS=ROOT/'.claude/skills/kh/templates/adws'
sys.path.insert(0,str(ADWS))
from adw_modules import agent_cc, agents, gates
from adw_modules.data_types import CCRequest, BuildOutput
from adw_modules.process_io import StreamRequest, run_stream


class RuntimeTests(unittest.TestCase):
    def test_prompt_defaults_to_configured_read_only_scout(self):
        import adw_prompt
        template = ROOT / '.claude/skills/kh/templates/kh.config.yaml'
        with patch.object(agents.utils, 'set_dotenv_allowlist', return_value=[]):
            cfg = agents.load_config(str(template))
        self.assertEqual([], agents.resolve(cfg, 'scout').writes)
        owners = []
        class Flow:
            engineer = 'fixture'
            @contextmanager
            def phase(self, params):
                owners.append(params.owner)
                yield NS(log=lambda **_: None,
                         call=lambda call: call.output_type(status='success'))
            def finish(self): return 0
        with patch.object(agents, 'load_config', return_value=cfg), \
             patch.object(agents, 'validate') as validate, \
             patch.object(adw_prompt.session, 'ensure', return_value=Flow()):
            self.assertEqual(0, adw_prompt.main('Inspect without editing source'))
        validate.assert_called_once_with(cfg, ['scout'])
        self.assertEqual(['fixture', 'scout'], owners)

    def test_cc_terminal_and_process_outcomes(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            cli=folder/'fake-claude'
            def invoke(events,exitcode=0):
                cli.write_text('#!'+sys.executable+'\nimport json,sys\n'+
                    '\n'.join('print('+repr(json.dumps(e))+')' for e in events)+f'\nsys.exit({exitcode})\n')
                cli.chmod(0o700)
                req=CCRequest(prompt='fixture',system_prompt='fixture',model='sonnet',session_id='id',
                    session_dir=str(folder/'sessions'),raw_output_path=str(folder/'raw.jsonl'),cwd=str(folder),tools=[])
                with patch.object(agent_cc,'CC_PATH',str(cli)):
                    return agent_cc.run(req)
            good={'type':'result','subtype':'success','is_error':False,'result':'{"status":"success"}'}
            self.assertTrue(invoke([good]).terminal_success)
            for events,code in [([good],1),([dict(good,is_error=True)],0),
                ([{'type':'assistant','message':{'content':[{'type':'text','text':'{"status":"success"}'}]}}],0)]:
                with self.assertRaises(RuntimeError):invoke(events,code)

    def test_stream_drains_stderr_and_honors_timeout(self):
        lines=[]
        code='import sys;sys.stderr.write("e"*200000);print("done",flush=True)'
        result,stderr=run_stream(StreamRequest([sys.executable,'-c',code],os.getcwd(),dict(os.environ),5),lines.append)
        self.assertEqual(result,0)
        self.assertEqual(lines,['done\n'])
        self.assertLessEqual(len(stderr),8000)
        result,stderr=run_stream(StreamRequest([sys.executable,'-c','import time;time.sleep(20)'],os.getcwd(),dict(os.environ),0.2),lines.append)
        self.assertEqual(result,124)
        self.assertIn('Timed out',stderr)

    def test_isolated_gate_cwd_and_relative_json(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'artifact.json').write_text('{invalid')
            run=NS(repo_root=root)
            result=gates.json_parses(BuildOutput(status='success',artifacts=['artifact.json']),run)
            self.assertFalse(result.passed)
            gate=gates.tests_pass([sys.executable,'-c',"from pathlib import Path; assert Path('artifact.json').exists()"])
            self.assertTrue(gate(BuildOutput(status='success'),run).passed)
            with self.assertRaises(ValueError):
                gates.tests_pass(f'{sys.executable} -c "pass"')


if __name__=='__main__':unittest.main()
