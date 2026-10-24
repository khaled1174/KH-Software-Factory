"""Sequential execution over real SQLite state with shared real repository locks."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from tests import test_project as fixture
ADWS=fixture.ADWS
plan=fixture.plan
sys.path.insert(0,str(ADWS))
import adw_project
import adw_start
from adw_modules import project, session


class RunNextTests(unittest.TestCase):
    setUp=fixture.ProjectTests.setUp
    tearDown=fixture.ProjectTests.tearDown
    receipt=fixture.ProjectTests.receipt
    reviewing=fixture.ProjectTests.reviewing
    complete=fixture.ProjectTests.complete

    def set_plan(self, independent=False):
        document=plan()
        if independent: document['tasks'][1]['dependencies']=[]
        project.init_project(self.root,document)

    def set_large_plan(self, count=21):
        document=plan()
        template=document['tasks'][0]
        document['milestones']=[document['milestones'][0]]
        document['tasks']=[{**template, 'id':f'T{index}', 'title':f'Task {index}',
                            'dependencies':[] if index == 1 else [f'T{index-1}']}
                           for index in range(1,count+1)]
        project.init_project(self.root,document)

    def executor(self, **kwargs):
        self.assertEqual(Path.cwd(),self.root)
        lease=kwargs['repo_lock']
        self.assertFalse(lease.closed)
        with self.assertRaisesRegex(RuntimeError,'BUSY'):
            session.acquire_repo_lock(self.root)
        borrowed=lease.borrow(self.root)
        borrowed.close()
        self.assertFalse(lease.closed)
        task=kwargs['task_id']
        self.complete(task,run='run'+task)
        return 0

    def test_retired_runner_and_strategy_flags_are_not_advertised(self):
        result=subprocess.run([sys.executable,'-B',str(ADWS/'adw_project.py'),'--help'],
                              capture_output=True,text=True,check=True)
        self.assertIn('run-continuous',result.stdout)
        for name in ('run-next','propose-execution','approve-execution','run-approved',
                     'parallel-status','parallel-resolve'):
            with self.subTest(command=name):
                self.assertNotIn(name,result.stdout)
                rejected=subprocess.run([sys.executable,'-B',str(ADWS/'adw_project.py'),name],
                                        capture_output=True,text=True)
                self.assertNotEqual(rejected.returncode,0)
                self.assertIn('invalid choice',rejected.stderr)

    def test_old_parallel_state_refuses_sequential_dispatch_without_mutation(self):
        import json
        self.set_plan()
        for relative in ('.kh/parallel/old/state.json','.kh/execution-decision.json',
                         '.kh/execution-metadata.json'):
            with self.subTest(record=relative):
                record=self.root/relative
                record.parent.mkdir(parents=True,exist_ok=True)
                record.write_text(json.dumps({'schema_version':1,'mode':'parallel','status':'complete'}))
                try:
                    before=fixture.ProjectTests.files(self)
                    for command in ([sys.executable,'-B',str(ADWS/'adw_project.py'),
                                     '--root',str(self.root),'run-continuous'],
                                    [sys.executable,'-B',str(ADWS/'adw_start.py'),'--task','T1']):
                        result=subprocess.run(command,cwd=self.root,capture_output=True,text=True,timeout=10)
                        self.assertNotEqual(result.returncode,0)
                        self.assertIn('LEGACY_PARALLEL_STATE',result.stderr)
                        self.assertIn(relative.split('/')[1],result.stderr)
                        self.assertEqual(before,fixture.ProjectTests.files(self))
                finally:
                    record.unlink()
                    if '/parallel/' in relative:
                        record.parent.rmdir();record.parent.parent.rmdir()

    def test_dependencies_unlock_under_one_lease_and_cwd_is_restored(self):
        self.set_plan()
        cwd=Path.cwd()
        with patch.object(adw_start,'main',side_effect=self.executor) as execute:
            result=adw_project.run_continuous(self.root,mode='light')
        self.assertEqual([entry['task_id'] for entry in result['attempted']],['T1','T2'])
        self.assertEqual(result['exit_code'],0)
        self.assertEqual(execute.call_args.kwargs['mode'],'light')
        self.assertIs(execute.call_args_list[0].kwargs['repo_lock'],execute.call_args_list[1].kwargs['repo_lock'])
        self.assertEqual(Path.cwd(),cwd)
        with session.acquire_repo_lock(self.root): pass

    def test_failure_stops_batch_and_blocked_is_not_retried(self):
        self.set_plan(independent=True)
        def fail(**kwargs):
            project.begin_task(self.root,'T1','failed')
            project.fail_task(self.root,'T1','failed','Verification rejected candidate')
            return 1
        with patch.object(adw_start,'main',side_effect=fail) as execute:
            result=adw_project.run_continuous(self.root)
        self.assertEqual(execute.call_count,1)
        self.assertEqual(result['status'],'stopped')
        with patch.object(adw_start,'main',side_effect=self.executor) as execute:
            second=adw_project.run_continuous(self.root)
        self.assertEqual([x['task_id'] for x in second['attempted']],['T2'])
        self.assertEqual(project.load_task(self.root,'T1')['status'],'blocked')
        self.assertEqual(second['status'],'stopped')
        self.assertEqual(second['exit_code'],1)

    def test_success_exit_without_receipt_does_not_continue(self):
        self.set_plan(independent=True)
        with patch.object(adw_start,'main',return_value=0) as execute:
            result=adw_project.run_continuous(self.root)
        self.assertEqual(execute.call_count,1)
        self.assertEqual(result['exit_code'],1)
        self.assertIn('verified completion',result['attempted'][0]['reason'])

    def test_busy_is_bounded_and_never_dispatches(self):
        self.set_plan()
        with session.acquire_repo_lock(self.root), patch.object(adw_start,'main') as execute:
            started=time.monotonic()
            with self.assertRaisesRegex(RuntimeError,'BUSY'):
                adw_project.run_continuous(self.root,wait_seconds=1)
            self.assertLess(time.monotonic()-started,2)
            execute.assert_not_called()

    def test_lock_is_shared_when_processes_use_different_tempdirs(self):
        self.set_plan()
        first, second = self.root/'temp-a', self.root/'temp-b'
        first.mkdir(); second.mkdir()
        code = ('import sys;sys.path.insert(0,sys.argv[1]);'
                'from adw_modules.session import acquire_repo_lock;'
                'lease=acquire_repo_lock(sys.argv[2]);print("acquired",flush=True);'
                'sys.stdin.readline();lease.close()')
        argv = [sys.executable, '-B', '-c', code, str(ADWS), str(self.root)]
        holder = subprocess.Popen(argv, env={**os.environ, 'TMPDIR': str(first)},
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), 'acquired')
            probe = subprocess.run(argv, env={**os.environ, 'TMPDIR': str(second)},
                                   input='\n', capture_output=True, text=True, timeout=5)
            self.assertNotEqual(probe.returncode, 0)
            self.assertIn('BUSY:', probe.stderr)
        finally:
            holder.stdin.write('\n'); holder.stdin.flush()
            holder.wait(timeout=5)
            holder.stdin.close(); holder.stdout.close(); holder.stderr.close()

    def test_pause_arriving_before_next_dispatch_stops_without_extra_task(self):
        self.set_plan()
        original_status = project.status
        original_pause = adw_project._pause_requested
        checked = False
        injected = False
        def pause(root, controller_id):
            nonlocal checked
            result = original_pause(root, controller_id)
            checked = True
            return result
        def status(root):
            nonlocal injected
            if checked and not injected:
                state = adw_project._read_json(adw_project._control_path(root, 'controller-state.json'))
                adw_project._write_control(root, 'controller-request.json', {
                    'schema_version': 1, 'controller_id': state['controller_id'],
                    'action': 'pause_after_current', 'requested_at': adw_project._now()})
                injected = True
            return original_status(root)
        with patch.object(project, 'status', side_effect=status), \
             patch.object(adw_project, '_pause_requested', side_effect=pause), \
             patch.object(adw_start, 'main', side_effect=self.executor) as execute:
            result = adw_project.run_continuous(self.root, notify=lambda _event: None)
        self.assertTrue(injected)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(project.load_task(self.root, 'T2')['status'], 'pending')

    def test_exception_and_cancellation_release_writer_and_restore_cwd(self):
        self.set_plan()
        cwd=Path.cwd()
        with patch.object(adw_start,'main',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): adw_project.run_continuous(self.root)
        self.assertEqual(Path.cwd(),cwd)
        with session.acquire_repo_lock(self.root): pass
        self.assertEqual(project.load_task(self.root,'T1')['status'],'pending')

    def test_invalid_bounds_rejected_before_lock_or_dispatch(self):
        for kwargs in ({'wait_seconds':61},{'wait_seconds':-1},{'mode':'auto'}):
            with self.assertRaises(ValueError): adw_project.run_continuous(self.root,**kwargs)

    def test_continuous_has_no_twenty_task_ceiling_and_notifies_before_next(self):
        self.set_large_plan()
        notifications=[]
        def execute(**kwargs):
            index=int(kwargs['task_id'][1:])
            self.assertEqual(len(notifications),index-1)
            return self.executor(**kwargs)
        with patch.object(adw_start,'main',side_effect=execute) as dispatched:
            result=adw_project.run_continuous(self.root,notify=notifications.append)
        self.assertEqual(dispatched.call_count,21)
        self.assertEqual(len(result['attempted']),21)
        self.assertEqual(len(notifications),21)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['exit_code'],0)
        self.assertEqual(notifications[0]['event'],'task_completed')
        self.assertEqual(notifications[0]['task_id'],'T1')
        self.assertEqual(notifications[-1]['completed_tasks'],21)
        self.assertEqual(notifications[-1]['total_tasks'],21)
        self.assertEqual(notifications[-1]['completion_percent'],100.0)
        self.assertEqual(adw_project.controller_status(self.root)['state'],'complete')

    def test_continuous_pause_after_current_then_resume(self):
        self.set_plan()
        notifications=[]
        def pause_during_first(**kwargs):
            if kwargs['task_id']=='T1':
                requested=adw_project.request_pause_after_current(self.root)
                self.assertEqual(requested['action'],'pause_after_current')
            return self.executor(**kwargs)
        with patch.object(adw_start,'main',side_effect=pause_during_first) as dispatched:
            first=adw_project.run_continuous(self.root,notify=notifications.append)
        self.assertEqual(dispatched.call_count,1)
        self.assertEqual(first['status'],'paused')
        self.assertEqual(first['exit_code'],0)
        self.assertEqual(adw_project.controller_status(self.root)['state'],'paused')
        self.assertEqual(project.load_task(self.root,'T2')['status'],'pending')
        with patch.object(adw_start,'main',side_effect=self.executor):
            second=adw_project.run_continuous(self.root,notify=notifications.append)
        self.assertEqual(second['status'],'complete')
        self.assertEqual([item['task_id'] for item in notifications],['T1','T2'])

    def test_continuous_stops_on_failed_task_without_notifying_completion(self):
        self.set_plan(independent=True)
        notifications=[]
        def fail(**kwargs):
            task=kwargs['task_id']
            project.begin_task(self.root,task,'failed')
            project.fail_task(self.root,task,'failed','Verification rejected candidate')
            return 1
        with patch.object(adw_start,'main',side_effect=fail) as dispatched:
            result=adw_project.run_continuous(self.root,notify=notifications.append)
        self.assertEqual(dispatched.call_count,1)
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(result['exit_code'],1)
        self.assertEqual(notifications,[])
        self.assertEqual(project.load_task(self.root,'T2')['status'],'pending')
        self.assertEqual(adw_project.controller_status(self.root)['state'],'stopped')

    def test_continuous_notification_failure_stops_before_later_task(self):
        self.set_plan()
        def unavailable(_event):
            raise OSError('notification channel unavailable')
        with patch.object(adw_start,'main',side_effect=self.executor) as dispatched:
            result=adw_project.run_continuous(self.root,notify=unavailable)
        self.assertEqual(dispatched.call_count,1)
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(result['exit_code'],1)
        self.assertIn('notification failed',result['reason'].lower())
        self.assertEqual(project.load_task(self.root,'T1')['status'],'completed')
        self.assertEqual(project.load_task(self.root,'T2')['status'],'pending')


if __name__=='__main__': unittest.main()
