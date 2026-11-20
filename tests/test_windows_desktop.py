"""Native lifecycle contracts, using disposable processes and isolated doubles."""
import ctypes as c
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from codex_switcher.windows_desktop import (
    LifecycleApi, NativeActivation, NativeDesktop, OwnedProcess, SHELLEXECUTEINFOW, _shell_execute,
)


class Clock:
    def __init__(self):
        self.value = 1000.
    def __call__(self):
        return self.value
    def sleep(self, seconds):
        self.value += seconds


class Api:
    def __init__(self, rows):
        self.rows = rows
        self.states = {row['pid']: 258 for row in rows}
        self.sticky = set()
        self.closed, self.killed, self.events = [], [], []
    def inventory(self):
        return self.rows
    def open(self, row, captured_at):
        self.events.append(('open',row['pid']))
        return OwnedProcess(row['pid'],row['pid'],row['name'],True)
    def wait(self, process):
        return self.states[process.pid]
    def terminate(self, process):
        self.killed.append(process.pid)
        if process.pid not in self.sticky:
            self.states[process.pid] = 0
        return True
    def close(self, process):
        self.closed.append(process.pid)
    def request_close(self, pids):
        self.events.append(('close',set(pids)))


def row(pid, ppid=1, name='ChatGPT.exe'):
    return dict(pid=pid,ppid=ppid,name=name,threads=4)


class NativeLifecycleTests(unittest.TestCase):
    def backend(self, rows):
        self.clock, self.api = Clock(), Api(rows)
        self.activation = mock.Mock()
        self.activation.submit.return_value = {'activation_pending':False}
        b=NativeDesktop(api=self.api,activation=self.activation,service_pid=999,
                        clock=self.clock,sleep=self.clock.sleep,wall_clock=lambda:1800000000)
        self.addCleanup(b.dispose)
        return b

    def test_closes_complete_desktop_tree_leaves_independent_cli_and_service_alone(self):
        b=self.backend([row(10),row(11,10,'codex.exe'),row(12,11,'worker.exe'),
                        row(20,1,'codex.exe'),row(999,1,'CodexSwitcher.exe')])
        result=b.close_tree()
        self.assertEqual(result['previous_pids'],[10,11,12])
        self.assertEqual(self.api.killed,[12,11,10])
        self.assertEqual(set(self.api.closed),{10,11,12})
        self.assertEqual(self.api.states[20],258)
        self.assertEqual(self.api.events[:3],[('open',10),('open',11),('open',12)])
        self.assertEqual(self.api.events[3],('close',{10}))
        self.assertFalse(result['launched'])
        self.activation.submit.assert_not_called()

    def test_desktop_ancestor_of_service_is_guarded_before_any_side_effect(self):
        b=self.backend([row(10),row(99,10,'powershell.exe'),row(999,99,'CodexSwitcher.exe')])
        result=b.close_tree()
        self.assertEqual(result['reason'],'service_in_desktop_tree')
        self.assertTrue(result['skipped'])
        self.assertEqual(self.api.events,[])
        self.assertEqual(self.api.killed,[])

    def test_windows_closing_gracefully_need_no_termination(self):
        b=self.backend([row(10)])
        self.api.request_close=lambda _:self.api.states.update({10:0})
        self.assertEqual(b.close_tree()['remaining'],[])
        self.assertEqual(self.api.killed,[])
        self.assertEqual(self.api.closed,[10])

    def test_lingering_descendant_blocks_launch_even_when_every_desktop_root_has_exited(self):
        b=self.backend([row(10),row(11,10,'codex.exe')])
        self.api.sticky={11}
        self.assertEqual(b.close_tree()['remaining'],[11])
        with self.assertRaises(OSError):
            b.launch('OpenAI.Codex_2p2nqsd0c76g0!App')
        self.activation.submit.assert_not_called()
        self.api.states[11]=0
        self.assertEqual(b.barrier()['remaining'],[])
        self.assertTrue(b.launch('OpenAI.Codex_2p2nqsd0c76g0!App')['launched'])
        self.assertEqual(sorted(self.api.closed),[10,11])

    def test_verified_system_monitor_is_excluded_from_close_and_restart_wait(self):
        b = self.backend([row(10), row(11, 10, 'codex.exe'), row(12, 10, 'WpcMon.exe')])
        api = LifecycleApi.__new__(LifecycleApi)
        api.kernel = mock.Mock()
        def system_directory(buffer, _size):
            buffer.value = r'C:\Windows\System32'
            return len(buffer.value)
        api.kernel.GetSystemDirectoryW.side_effect = system_directory
        def query_image(_handle, _flags, buffer, _size):
            buffer.value = r'c:\WINDOWS\System32\WpcMon.exe'
            return True
        api.kernel.QueryFullProcessImageNameW.side_effect = query_image
        api.kernel.OpenProcess.return_value = 120
        api.kernel.WaitForSingleObject.return_value = 258
        api.kernel.GetProcessTimes.return_value = True
        original_open = self.api.open
        self.api.open = lambda item, now: api.open(item, now) if item['pid'] == 12 else original_open(item, now)

        result = b.close_tree()
        self.assertEqual(result['previous_pids'], [10, 11])
        self.assertEqual(result['remaining'], [])
        self.assertEqual(self.api.killed, [11, 10])
        self.assertEqual(self.api.states[12], 258)
        api.kernel.CloseHandle.assert_called_once_with(120)
        api.kernel.TerminateProcess.assert_not_called()
        self.assertTrue(b.launch('OpenAI.Codex_2p2nqsd0c76g0!App')['launched'])
        self.assertFalse(api._is_windows_monitor(r'C:\Elsewhere\WpcMon.exe'))

    def test_unknown_wait_does_not_mean_exit_or_permission_to_launch(self):
        b=self.backend([row(10)])
        self.api.states[10]=0xffffffff
        self.assertEqual(b.close_tree()['remaining'],[10])
        self.assertEqual(self.api.killed,[])
        with self.assertRaises(OSError):b.launch('OpenAI.Codex_2p2nqsd0c76g0!App')

    def test_identity_or_permission_failure_prevents_all_closing(self):
        b=self.backend([row(10),row(11,10,'codex.exe')])
        original=self.api.open
        def fail(item, captured_at):
            if item['pid']==11:raise OSError('fixture identity changed')
            return original(item,captured_at)
        self.api.open=fail
        with self.assertRaises(OSError):b.close_tree()
        self.assertEqual(self.api.closed,[10])
        self.assertFalse(any(kind=='close' for kind,*_ in self.api.events))
        self.assertEqual(self.api.killed,[])

    def test_repeated_close_while_waiting_does_not_kill_again(self):
        b=self.backend([row(10)])
        self.api.sticky={10}
        b.close_tree()
        b.close_tree()
        self.assertEqual(self.api.killed,[10])
        b.dispose()
        b.dispose()
        self.assertEqual(self.api.closed,[10])

    def test_no_registered_app_does_not_activate_anything(self):
        b=self.backend([])
        self.assertFalse(b.launch(None)['launched'])
        self.activation.submit.assert_not_called()

    def test_invalid_uri_and_identity_are_rejected_without_activation(self):
        b=self.backend([])
        for uri in ('https://example.com','codex://threads/wake/../../bad','codex://threads/'+'-'*36):
            with self.assertRaises(ValueError):b.open_uri(uri)
        with self.assertRaises(ValueError):b.launch('Other.App_publisher!App')
        self.activation.submit.assert_not_called()

    def test_native_uri_and_appfolder_handoff_have_no_interpreter(self):
        b=self.backend([])
        uri='codex://threads/wake/01a17a9b-88bf-7620-a979-c2ecbf5dcc26'
        b.open_uri(uri)
        b.launch('OpenAI.Codex_2p2nqsd0c76g0!App')
        self.assertEqual(self.activation.submit.call_args_list,[mock.call(uri),
            mock.call('shell:AppsFolder\\OpenAI.Codex_2p2nqsd0c76g0!App')])


class NativeActivationTests(unittest.TestCase):
    def test_pending_activation_is_kept_alive_and_duplicate_calls_are_merged(self):
        entered,release,done=threading.Event(),threading.Event(),threading.Event()
        calls=[]
        def execute(target):
            calls.append(target);entered.set()
            release.wait(3);done.set()
        activation=NativeActivation(execute=execute)
        try:
            self.assertTrue(activation.submit('fixture')['activation_pending'])
            self.assertTrue(entered.wait(1))
            self.assertTrue(activation.submit('fixture')['activation_pending'])
            self.assertEqual(calls,['fixture'])
        finally:release.set()
        self.assertTrue(done.wait(1))

    def test_completed_error_is_not_reported_as_a_successful_handoff(self):
        for exc in (OSError('fixture no association'),TimeoutError('fixture internal timeout')):
            def fail(_):raise exc
            with self.subTest(error=type(exc).__name__),self.assertRaises(type(exc)):
                NativeActivation(execute=fail).submit('fixture')

    @unittest.skipUnless(os.name=='nt','Windows ABI and mocked shell')
    def test_shell_initializes_sta_closes_returned_handle_and_releases_com(self):
        ole,shell,kernel=mock.Mock(),mock.Mock(),mock.Mock()
        ole.CoInitializeEx.return_value=0
        def execute(info):info._obj.hProcess=77;return True
        shell.ShellExecuteExW.side_effect=execute
        libraries={'ole32':ole,'shell32':shell,'kernel32':kernel}
        with mock.patch('codex_switcher.windows_desktop.c.WinDLL',side_effect=lambda name,**_:libraries[name]):
            _shell_execute('fixture')
        info=shell.ShellExecuteExW.call_args.args[0]._obj
        self.assertEqual(info.lpFile,'fixture')
        self.assertEqual(info.fMask,0x8540)
        ole.CoInitializeEx.assert_called_once_with(None,6)
        kernel.CloseHandle.assert_called_once_with(77)
        ole.CoUninitialize.assert_called_once()
        self.assertEqual(c.sizeof(SHELLEXECUTEINFOW),112 if c.sizeof(c.c_void_p)==8 else 60)

    @unittest.skipUnless(os.name=='nt','mocked Windows COM')
    def test_com_failure_never_calls_shell_or_uninitializes_an_unowned_apartment(self):
        ole,shell,kernel=mock.Mock(),mock.Mock(),mock.Mock()
        ole.CoInitializeEx.return_value=-2147417850
        libs={'ole32':ole,'shell32':shell,'kernel32':kernel}
        with mock.patch('codex_switcher.windows_desktop.c.WinDLL',side_effect=lambda name,**_:libs[name]),self.assertRaises(OSError):
            _shell_execute('fixture')
        shell.ShellExecuteExW.assert_not_called()
        ole.CoUninitialize.assert_not_called()


@unittest.skipUnless(os.name=='nt','disposable Windows process')
class NativeDisposableProcessTests(unittest.TestCase):
    def test_closes_only_the_disposable_target_and_keeps_an_independent_process(self):
        flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
        target=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],creationflags=flags)
        other=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],creationflags=flags)
        real=LifecycleApi()
        # Restrict the inventory to our two Popen-owned fixtures. The adapter's
        # alias only chooses a test root; image validation still checks python.
        class FixtureApi:
            def inventory(self):return [row(target.pid,name='ChatGPT.exe'),row(other.pid,name='python.exe')]
            def open(self,item,captured_at):
                return real.open({**item,'name':Path(sys.executable).name},captured_at)
            wait=staticmethod(real.wait)
            terminate=staticmethod(real.terminate)
            close=staticmethod(real.close)
            request_close=staticmethod(lambda _:None)
        backend=NativeDesktop(api=FixtureApi(),activation=mock.Mock())
        try:
            result=backend.close_tree()
            self.assertEqual(result['remaining'],[])
            self.assertEqual(result['killed'],[target.pid])
            target.wait(3)
            self.assertIsNone(other.poll())
        finally:
            backend.dispose()
            for owned in (target,other):
                if owned.poll() is None:owned.terminate()
                owned.wait(3)


if __name__=='__main__':unittest.main()
