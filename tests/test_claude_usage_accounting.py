"""Native Claude cumulative snapshots must be attributed once per session."""
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_core as core
from adw_modules import agent_cc, process_io
from adw_modules.data_types import CCRequest


def assistant(identity='message-1', output=3):
    return {'type':'assistant','session_id':'fixture','message':{'id':identity,
        'content':[{'type':'text','text':'done'}], 'usage':{
        'input_tokens':2,'output_tokens':output,'cache_read_input_tokens':5,
        'cache_creation_input_tokens':1}}}


def terminal(input=10, output=5, read=20, write=5, cost=0.1, helper=2):
    return {'type':'result','subtype':'success','is_error':False,'session_id':'fixture',
        'result':'done','total_cost_usd':cost,'usage':{'input_tokens':1,'output_tokens':1},
        'modelUsage':{'main':{'inputTokens':input,'outputTokens':output,
            'cacheReadInputTokens':read,'cacheCreationInputTokens':write,'thinkingTokens':2},
            'helper':{'inputTokens':helper,'outputTokens':1,'cacheReadInputTokens':0,
                'cacheCreationInputTokens':0}}}


class ClaudeUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='kh-claude-usage-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.request=CCRequest(prompt='x',system_prompt='x',model='main',session_id='fixture',
            session_dir=str(self.root/'sessions'),raw_output_path=str(self.root/'raw.jsonl'),
            cwd=str(self.root),tools=['read'])

    def send(self, rows, *, timeout=False, code=0, stderr=''):

        def stream(request, receive, **kwargs):
            for row in rows:receive(json.dumps(row))
            return process_io.StreamResult(124 if timeout else code,stderr,timed_out=timeout)
        with patch.object(process_io,'run_stream',side_effect=stream):
            return agent_cc.run(self.request)

    def test_terminal_snapshot_replaces_duplicate_messages_and_includes_helpers(self):
        row=assistant()
        result=self.send([row,row,assistant(output=4),terminal()])
        self.assertEqual(result.tokens,43) # main 40 plus helper 3; thinking is already in output
        self.assertEqual(result.usage.total_tokens,43)
        self.assertEqual(result.usage.input_tokens,12)
        self.assertEqual(result.usage.reasoning_tokens,2)
        self.assertEqual(result.context_tokens,12) # last assistant occupancy remains independent

    def test_resumed_snapshot_and_price_are_deltas_not_another_full_session(self):
        first=self.send([assistant(),terminal()])
        second=self.send([assistant('message-2'),terminal(input=15,output=10,read=40,write=8,cost=0.25,helper=4)])
        self.assertEqual((first.tokens,second.tokens),(43,35))
        self.assertAlmostEqual(first.cost+second.cost,0.25)
        self.assertEqual(first.usage.total_tokens+second.usage.total_tokens,78)
        self.assertAlmostEqual(second.cost,0.15)

    def test_duplicate_terminal_does_not_duplicate_estimated_price(self):
        row=terminal()
        result=self.send([row,row])
        self.assertEqual(result.tokens,43)
        self.assertEqual(result.cost,0.1)


    def test_timeout_tail_is_deduplicated_and_reconciled_by_resume(self):
        row=assistant()
        with self.assertRaises(process_io.RoleTimeout) as caught:
            self.send([row,row,assistant(output=4)],timeout=True)
        self.assertEqual(caught.exception.result.tokens,12)
        self.assertFalse(caught.exception.result.cost_known)
        # The rejected fresh-session lookup performs no work, then resumes.
        result=self.send([assistant('message-2',output=2),terminal()])
        self.assertEqual(result.tokens,31)
        self.assertEqual(result.tokens+caught.exception.result.tokens,43)
        self.assertEqual(result.cost,0.1)
        self.assertTrue(result.accounting['complete'])

    def test_imported_resume_excludes_unknown_historical_totals(self):
        agent_cc._session_marker(self.request).touch()
        result=self.send([assistant(),terminal()])
        self.assertEqual(result.tokens,11)
        self.assertEqual(result.cost,0)
        self.assertFalse(result.cost_known)
        self.assertFalse(result.accounting['complete'])
        self.assertIn('unknown_session_baseline',result.accounting['warnings'])
        second=self.send([assistant('message-2'),terminal(input=15,output=10,read=40,write=8,cost=0.25,helper=4)])
        self.assertEqual(second.tokens,35)
        self.assertAlmostEqual(second.cost,0.15)
        self.assertFalse(second.accounting['complete'])

    def test_decreased_native_snapshot_is_visible_and_never_silently_clamped(self):
        self.send([terminal()])
        result=self.send([terminal(input=1,cost=0.01)])
        self.assertEqual(result.tokens,0)
        self.assertEqual(result.cost,0)
        self.assertFalse(result.cost_known)
        self.assertIn('invalid_or_decreased_native_snapshot',result.accounting['warnings'])
        self.assertIn('provider_estimated_price_decreased',result.accounting['warnings'])
        self.assertFalse(result.accounting['complete'])

    def test_conflicting_terminal_identity_is_disclosed(self):
        first=terminal();first['uuid']='result-1'
        second=terminal(input=15,cost=0.2);second['uuid']='result-1'
        result=self.send([first,second])
        self.assertEqual(result.tokens,43)
        self.assertEqual(result.cost,0.1)
        self.assertIn('conflicting_terminal_snapshot',result.accounting['warnings'])
        self.assertFalse(result.accounting['complete'])
        self.assertFalse(result.accounting['provider_estimated_cost_complete'])

    def test_replayed_message_after_terminal_is_not_charged_again(self):
        row=assistant()
        self.send([row,terminal()])
        result=self.send([row,terminal()])
        self.assertEqual(result.tokens,0)
        self.assertEqual(result.cost,0)

    def test_invalid_native_counts_do_not_hide_valid_provisional_usage(self):
        for value in (True,-1,1.5,'10',None):
            with self.subTest(value=value):
                event=terminal();event['modelUsage']['main']['inputTokens']=value
                result=self.send([assistant(),event])
                self.assertIn('invalid_or_decreased_native_snapshot',result.accounting['warnings'])
                self.assertFalse(result.accounting['complete'])
                self.assertGreaterEqual(result.tokens,0)

    def test_missing_message_identity_is_partial_until_terminal_settles_it(self):
        row=assistant();del row['message']['id']
        with self.assertRaises(agent_cc.CCRunError) as caught:self.send([row,row])
        self.assertEqual(caught.exception.result.tokens,11)
        self.assertFalse(caught.exception.result.accounting['complete'])
        result=self.send([terminal()])
        self.assertEqual(result.tokens,32)
        self.assertTrue(result.accounting['complete'])

    def test_corrupt_accounting_state_is_preserved_and_not_assumed_zero(self):
        agent_cc._session_marker(self.request).touch()
        from adw_modules.claude_usage import state_path
        state=state_path(self.request);state.write_text('broken')
        result=self.send([assistant(),terminal()])
        self.assertEqual(state.read_text(),'broken')
        self.assertEqual(result.tokens,0)
        self.assertEqual(result.accounting['unattributed_usage']['total_tokens'],11)
        self.assertEqual(result.cost,0)
        self.assertIn('invalid_session_accounting_state',result.accounting['warnings'])
        self.assertFalse(result.accounting['complete'])

    def test_session_mismatch_does_not_import_another_sessions_usage(self):
        row=terminal();row['session_id']='other'
        result=self.send([row])
        self.assertEqual((result.tokens,result.cost),(0,0))
        self.assertIn('native_session_mismatch',result.accounting['warnings'])

    def test_estimate_and_unknown_subscription_bill_are_separate_in_receipt(self):
        result=self.send([terminal()])
        receipt=json.loads((self.root/'claude-usage.json').read_text())
        self.assertEqual(receipt['provider_estimated_cost_usd'],0.1)
        self.assertIsNone(receipt['actual_billed_subscription_cost_usd'])
        self.assertEqual(receipt['tokens'],43)
        self.assertTrue(receipt['complete'])

    def test_actual_subprocess_resumes_with_persisted_cumulative_baseline(self):
        script=self.root/'fake-claude'
        script.write_text('#!'+sys.executable+'\nimport json,sys\n'+
            'rows='+repr([assistant(),terminal()])+'\n'+
            "if '--resume' in sys.argv: rows="+repr([assistant('message-2'),terminal(input=15,output=10,read=40,write=8,cost=0.25,helper=4)])+'\n'+
            'for row in rows: print(json.dumps(row),flush=True)\n')
        script.chmod(0o700)
        with patch.object(agent_cc,'CC_PATH',str(script)):
            first=agent_cc.run(self.request)
            second=agent_cc.run(self.request)
        self.assertEqual((first.tokens,second.tokens),(43,35))
        self.assertAlmostEqual(first.cost+second.cost,0.25)
        self.assertIn('--permission-mode',agent_cc._command(self.request,True))


    def test_failed_state_write_defers_charge_until_resume_can_record_it(self):
        from adw_modules import claude_usage
        first=self.send([terminal()])
        original=claude_usage._atomic_json
        def failing(path,value):
            if path.name.endswith('.usage.json'):raise OSError('state disk unavailable')
            return original(path,value)
        event=terminal(input=15,output=10,read=40,write=8,cost=0.25,helper=4)
        with patch.object(claude_usage,'_atomic_json',side_effect=failing):second=self.send([event])
        third=self.send([event])
        self.assertEqual((first.tokens,second.tokens,third.tokens),(43,0,35))
        self.assertAlmostEqual(first.cost+second.cost+third.cost,0.25)
        self.assertEqual(second.accounting['unattributed_usage']['total_tokens'],35)
        self.assertFalse(second.accounting['complete'])
        self.assertTrue(third.accounting['complete'])

    def test_structured_collision_does_not_establish_false_zero_baseline(self):
        calls=[]
        def stream(request,receive,**kwargs):
            calls.append(request.argv)
            if len(calls)==1:
                event=terminal(input=0,output=0,read=0,write=0,cost=0,helper=0)
                event['modelUsage']['main']['thinkingTokens']=0
                event['modelUsage']['helper']['outputTokens']=0
                event['is_error']=True
                receive(json.dumps(event))
                return 1,'session already in use'
            receive(json.dumps(terminal(input=100000,cost=100)))
            return 0,''
        with patch.object(process_io,'run_stream',side_effect=stream):result=agent_cc.run(self.request)
        self.assertEqual(len(calls),2)
        self.assertIn('--resume',calls[1])
        self.assertEqual((result.tokens,result.cost),(0,0))
        self.assertFalse(result.accounting['known_zero_baseline'])
        self.assertFalse(result.accounting['complete'])

    def test_later_incomplete_terminal_cannot_reuse_earlier_complete_flag(self):
        late=terminal(cost=0.2);del late['modelUsage']
        result=self.send([terminal(),late])
        self.assertEqual(result.tokens,43)
        self.assertFalse(result.accounting['complete'])
        self.assertIn('terminal_model_usage_missing',result.accounting['warnings'])

    def test_post_terminal_tail_and_timeout_receipt_are_partial(self):
        with self.assertRaises(process_io.RoleTimeout) as caught:
            self.send([terminal(),assistant('late')],timeout=True)
        result=caught.exception.result
        receipt=json.loads((self.root/'claude-usage.json').read_text())
        self.assertFalse(result.cost_known)
        self.assertFalse(receipt['complete'])
        self.assertFalse(receipt['provider_estimated_cost_complete'])
        self.assertTrue(receipt['interrupted'])

    def test_retrospective_thinking_is_not_charged_as_this_sends_output(self):
        row=assistant(output=100)
        with self.assertRaises(process_io.RoleTimeout) as caught:self.send([row],timeout=True)
        event=terminal(input=10,output=101,cost=0.5)
        event['modelUsage']['main']['thinkingTokens']=90
        result=self.send([event])
        self.assertLessEqual(result.usage.reasoning_tokens,result.usage.output_tokens)
        self.assertEqual(result.accounting['native_session_reasoning_tokens'],90)
        self.assertGreater(result.accounting['retrospective_reasoning_tokens'],0)
        self.assertFalse(result.accounting['complete'])
        self.assertEqual(caught.exception.result.tokens+result.tokens,139)

    def test_reader_exception_keeps_original_error_and_observed_usage(self):
        failure=OSError('reader failed')
        def stream(request,receive,**kwargs):
            receive(json.dumps(assistant()))
            raise failure
        with patch.object(process_io,'run_stream',side_effect=stream):
            with self.assertRaises(OSError) as caught:agent_cc.run(self.request)
        self.assertIs(caught.exception,failure)
        partial=failure.kh_claude_usage_result
        self.assertEqual(partial.tokens,11)
        self.assertFalse(partial.terminal_success)
        self.assertFalse(partial.cost_known)
        result=self.send([terminal()])
        self.assertEqual(result.tokens+partial.tokens,43)

    def test_confirmed_missing_conversation_resets_native_counter_generation(self):
        self.send([terminal()])
        calls=[]
        def stream(request,receive,**kwargs):
            calls.append(request.argv)
            if len(calls)==1:return 1,'No conversation found'
            receive(json.dumps(terminal()))
            return 0,''
        with patch.object(process_io,'run_stream',side_effect=stream):result=agent_cc.run(self.request)
        self.assertEqual(result.tokens,43)
        self.assertEqual(result.cost,0.1)
        self.assertIn('--resume',calls[0]);self.assertIn('--session-id',calls[1])


    def test_new_malformed_assistant_tail_invalidates_usage_and_price_coverage(self):
        row=assistant('bad-tail');row['message']['usage']['output_tokens']='invalid'
        result=self.send([terminal(),row])
        self.assertFalse(result.accounting['complete'])
        self.assertFalse(result.cost_known)
        self.assertFalse(result.accounting['provider_estimated_cost_complete'])

    def test_final_accounting_callback_failure_preserves_deliverable_usage(self):
        failure=OSError('final trace callback failed')
        def stream(request,receive,**kwargs):
            receive(json.dumps(terminal()))
            return 0,''
        def observe(event):
            if event.get('type')=='claude_usage_accounting':raise failure
        with patch.object(process_io,'run_stream',side_effect=stream):
            with self.assertRaises(OSError) as caught:agent_cc.run(self.request,on_event=observe)
        self.assertIs(caught.exception,failure)
        self.assertEqual(failure.kh_claude_usage_result.tokens,43)
        self.assertFalse(failure.kh_claude_usage_result.cost_known)
        result=self.send([terminal()])
        self.assertEqual(result.tokens,0)

    def test_marker_write_failure_preserves_usage_and_receipt_failure_state(self):
        original=Path.touch
        failure=OSError('marker failed')
        def touch(path,*args,**kwargs):
            if path.name.endswith('.created'):raise failure
            return original(path,*args,**kwargs)
        with patch.object(Path,'touch',touch):
            with self.assertRaises(OSError) as caught:self.send([terminal()])
        self.assertIs(caught.exception,failure)
        self.assertEqual(failure.kh_claude_usage_result.tokens,43)
        receipt=json.loads((self.root/'claude-usage.json').read_text())
        self.assertFalse(receipt['provider_estimated_cost_complete'])
        self.assertIn('session_marker_not_persisted',receipt['warnings'])

    def test_rejected_fresh_lookup_preserves_earlier_owned_failure_baseline(self):
        event=terminal();event['is_error']=True
        with self.assertRaises(agent_cc.CCRunError) as caught:self.send([event],code=1)
        self.assertEqual(caught.exception.result.tokens,43)
        calls=[]
        def stream(request,receive,**kwargs):
            calls.append(request.argv)
            if len(calls)==1:
                error=terminal(input=0,output=0,read=0,write=0,cost=0,helper=0)
                error['is_error']=True
                error['modelUsage']['main']['thinkingTokens']=0
                error['modelUsage']['helper']['outputTokens']=0
                receive(json.dumps(error))
                return 1,'already in use'
            receive(json.dumps(terminal(input=15,output=10,read=40,write=8,cost=0.25,helper=4)))
            return 0,''
        with patch.object(process_io,'run_stream',side_effect=stream):result=agent_cc.run(self.request)
        self.assertEqual(result.tokens,35)
        self.assertAlmostEqual(result.cost,0.15)
        self.assertTrue(result.accounting['complete'])


    def test_collision_preserves_unknown_but_already_measured_session_anchor(self):
        agent_cc._session_marker(self.request).touch()
        self.send([assistant(),terminal()])
        agent_cc._session_marker(self.request).unlink()
        calls=[]
        def stream(request,receive,**kwargs):
            calls.append(request.argv)
            if len(calls)==1:return 1,'already in use'
            receive(json.dumps(terminal(input=15,output=10,read=40,write=8,cost=0.25,helper=4)))
            return 0,''
        with patch.object(process_io,'run_stream',side_effect=stream):result=agent_cc.run(self.request)
        self.assertEqual(result.tokens,35)
        self.assertAlmostEqual(result.cost,0.15)
        self.assertFalse(result.accounting['known_zero_baseline'])
        self.assertFalse(result.accounting['complete'])


class ClaudePhaseUsageTests(unittest.TestCase):
    setUp=core.CoreTests.setUp
    tearDown=core.CoreTests.tearDown
    call=core.CoreTests.call

    def test_json_correction_reconciles_phase_and_sqlite_totals(self):
        native=agent_cc.run
        sends=[]
        def stream(request,receive,**kwargs):
            sends.append(request.argv)
            (Path(request.cwd)/'source.py').write_text('value = 2\n')
            row=terminal() if len(sends)==1 else terminal(input=15,output=10,read=40,write=8,cost=0.25,helper=4)
            sid=request.argv[request.argv.index('--resume' if '--resume' in request.argv else '--session-id')+1]
            row['session_id']=sid
            row['result']='invalid JSON' if len(sends)==1 else json.dumps({'status':'success','changed_files':['source.py']})
            receive(json.dumps(row))
            return 0,''
        with patch.object(process_io,'run_stream',side_effect=stream):self.call(native)
        self.assertEqual(len(sends),2)
        self.assertIn('--session-id',sends[0]);self.assertIn('--resume',sends[1])
        self.assertEqual(self.run.tokens,78)
        self.assertAlmostEqual(self.run.cost,0.25)
        self.assertEqual(self.tracer.conn.execute("SELECT total_tokens FROM sessions WHERE adw_id='probe'").fetchone()[0],78)
        events=[json.loads(x) for x in self.tracer.events_jsonl.read_text().splitlines()]
        end=next(x for x in events if x['type']=='agent_end')
        self.assertEqual(end['tokens'],78)
        self.assertAlmostEqual(end['payload']['cost'],0.25)
        accounting=[x for x in events if x.get('name')=='claude_usage_accounting']
        self.assertEqual(len(accounting),2)
        self.assertTrue(all(x['payload']['actual_billed_subscription_cost_usd'] is None for x in accounting))

    def test_failed_native_call_keeps_measured_consumption_without_accepting_work(self):
        native=agent_cc.run
        def stream(request,receive,**kwargs):
            sid=request.argv[request.argv.index('--session-id')+1]
            row=terminal();row.update(session_id=sid,is_error=True)
            receive(json.dumps(row))
            return 1,'provider failed'
        with patch.object(process_io,'run_stream',side_effect=stream):
            with self.assertRaises(agent_cc.CCRunError):self.call(native)
        self.assertEqual(self.run.tokens,43)
        self.assertEqual(self.run.cost,0.1)
        self.assertFalse(self.run.cost_known)
        self.assertEqual((self.root/'source.py').read_text(),'value = 1\n')


    def test_callback_failure_keeps_phase_usage_and_original_exception(self):
        native=agent_cc.run
        failure=OSError('fixture reader failed')
        def stream(request,receive,**kwargs):
            sid=request.argv[request.argv.index('--session-id')+1]
            row=assistant();row['session_id']=sid
            receive(json.dumps(row))
            raise failure
        with patch.object(process_io,'run_stream',side_effect=stream):
            with self.assertRaises(OSError) as caught:self.call(native)
        self.assertIs(caught.exception,failure)
        self.assertEqual(self.run.tokens,11)
        self.assertFalse(self.run.cost_known)
        self.assertEqual((self.root/'source.py').read_text(),'value = 1\n')


if __name__=='__main__':unittest.main()
