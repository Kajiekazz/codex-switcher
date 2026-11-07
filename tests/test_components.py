"""Browser download routing tests: no external network or installed browser changes."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from codex_switcher.components import BrowserComponents, ComponentError, _probe, _INSTALL_SCRIPT


URL = 'https://cdn.playwright.dev/builds/cft/156.0.8078.4/win64/chrome-win64.zip'
GOOGLE = 'https://storage.googleapis.com/chrome-for-testing-public/156.0.8078.4/win64/chrome-win64.zip'


def catalog(custom=False):
    return {'custom': custom, 'executables': [{'name': 'chromium', 'urls': [URL], 'version': '156.0.8078.4'}]}


def good_probe(url, cancel):
    return {'status': 'available', 'latency_ms': 10, 'speed_bps': 200 if url == GOOGLE else 100}


class ComponentSourceTests(unittest.TestCase):
    def manager(self, **kwargs):
        return BrowserComponents(executable=lambda: None, catalog_loader=catalog, probe=good_probe, **kwargs)

    def test_status_discovers_once_without_probing_and_offers_real_cft_routes(self):
        loader, probe = mock.Mock(side_effect=catalog), mock.Mock()
        manager = BrowserComponents(executable=lambda: None, catalog_loader=loader, probe=probe)
        for _ in range(3):
            result = manager.status()
        loader.assert_called_once()
        probe.assert_not_called()
        self.assertEqual([x['id'] for x in result['sources']], ['playwright', 'chrome-for-testing'])
        self.assertEqual(manager._routes['chromium'], [URL, GOOGLE])
        self.assertNotIn('prss.microsoft', ' '.join(manager._routes['chromium']))

    def test_configured_mirror_is_preserved(self):
        manager = BrowserComponents(executable=lambda: None, catalog_loader=lambda: catalog(True), probe=good_probe)
        result = manager.status()
        self.assertEqual([x['id'] for x in result['sources']], ['configured'])
        self.assertEqual(manager._routes['chromium'], [URL])

    def test_dependency_source_label_distinguishes_custom_and_official_hosts(self):
        from codex_switcher.components import _source
        mirror = _source('https://components.example/builds/ffmpeg/1013/ffmpeg-win64.zip')
        official = _source('https://cdn.playwright.dev/builds/ffmpeg/1013/ffmpeg-win64.zip')
        self.assertEqual(mirror['id'], 'configured')
        self.assertEqual(mirror['host'], 'components.example')
        self.assertEqual(official['id'], 'playwright')

    def test_unknown_source_rejected_without_probe_or_process(self):
        manager = self.manager(process_factory=mock.Mock())
        with self.assertRaises(ValueError):
            manager.start(source_id='untrusted-url')
        manager._factory.assert_not_called()

    def test_auto_uses_measured_exact_url_order_and_manual_preference(self):
        manager = self.manager()
        manager.status()
        self.assertEqual(manager._probe_routes('auto')['chromium'], [GOOGLE, URL])
        self.assertEqual(manager._probe_routes('playwright')['chromium'], [URL, GOOGLE])

    def test_one_probe_failure_falls_back_and_all_fail_is_actionable(self):
        manager = self.manager()
        manager.status()
        def fail_primary(url, cancel):
            if url == URL:
                raise TimeoutError()
            return good_probe(url, cancel)
        manager._probe = fail_primary
        self.assertEqual(manager._probe_routes('playwright')['chromium'], [GOOGLE])
        self.assertEqual(manager.status()['sources'][0]['status'], 'failed')
        manager._probe = mock.Mock(side_effect=TimeoutError())
        with self.assertRaisesRegex(ComponentError, '没有可用'):
            manager._probe_routes('auto')

    def test_download_command_receives_same_ranked_urls_and_progress_is_real(self):
        ready = []
        event = 'SWITCHER_COMPONENT '
        output = event + json.dumps({'kind':'download', 'component':'chromium','url':GOOGLE,'attempt':2,'attempts':5}) + '\n'
        output += event + json.dumps({'kind':'progress','downloaded_bytes':10,'total_bytes':20,'speed_bps':5}) + '\n'
        process = mock.Mock(stdout=io.StringIO(output))
        process.wait.side_effect = lambda: ready.append(True) or 0
        factory = mock.Mock(return_value=process)
        manager = BrowserComponents(factory, lambda: Path('chrome.exe') if ready else None,
                                    catalog_loader=catalog, probe=good_probe)
        with tempfile.TemporaryDirectory() as directory, mock.patch('codex_switcher.components.browser_root', return_value=Path(directory)):
            manager.start()
            self.assertTrue(manager._done.wait(2))
        routes = json.loads(factory.call_args.kwargs['env']['SWITCHER_COMPONENT_ROUTES'])
        self.assertEqual(routes['chromium'], [GOOGLE, URL])
        status = manager.status()
        self.assertTrue(status['ready'])
        self.assertEqual(status['source']['id'], 'chrome-for-testing')
        self.assertEqual(status['downloaded_bytes'], 10)
        self.assertEqual(status['attempt'], 2)
        self.assertIsNone(status['speed_bps'])

    def test_cancel_during_probe_never_launches_installer(self):
        started = threading.Event()
        def probe(url, cancel):
            started.set()
            cancel.wait(2)
            raise ComponentError('cancelled')
        factory = mock.Mock()
        manager = BrowserComponents(factory, lambda: None, timeout=.05, catalog_loader=catalog, probe=probe)
        manager.start()
        self.assertTrue(started.wait(1))
        self.assertTrue(manager._done.wait(2))
        self.assertEqual(manager.status()['state'], 'failed')
        factory.assert_not_called()

    def test_cancel_uses_owned_process_stdin_before_fallback(self):
        manager = self.manager()
        process = mock.Mock()
        process.poll.return_value = None
        manager._proc = process
        manager.cancel()
        process.stdin.write.assert_called_once_with('cancel\n')
        process.wait.assert_called_once_with(timeout=2)
        process.terminate.assert_not_called()

    def test_partial_browser_install_does_not_hide_dependency_failure(self):
        manager = BrowserComponents(executable=lambda: Path('chrome.exe'), catalog_loader=catalog)
        manager._state, manager._phase = 'failed', 'failed'
        self.assertFalse(manager.status()['ready'])
        self.assertEqual(manager.status()['state'], 'failed')

    def test_installed_dependencies_are_reused_without_a_probe(self):
        data = catalog()
        data['executables'].append({'name': 'ffmpeg', 'urls': ['https://example.invalid/ffmpeg.zip'], 'installed': True})
        probe = mock.Mock(side_effect=good_probe)
        manager = BrowserComponents(executable=lambda: None, catalog_loader=lambda: data, probe=probe)
        manager.status()
        routes = manager._probe_routes('auto')
        self.assertEqual(routes['ffmpeg'], ['https://example.invalid/ffmpeg.zip'])
        self.assertEqual(probe.call_count, 2)

    def test_total_timeout_does_not_wait_for_stuck_dns_thread(self):
        release = threading.Event()
        def probe(url, cancel):
            release.wait(3)
            return good_probe(url, cancel)
        manager = BrowserComponents(executable=lambda: None, timeout=.05, catalog_loader=catalog, probe=probe)
        try:
            manager.start()
            self.assertTrue(manager._done.wait(.5))
            self.assertEqual(manager.status()['state'], 'failed')
        finally:
            release.set()

    def test_sample_caps_ignored_range_and_rejects_html(self):
        from codex_switcher.components import _driver
        node, _, env = _driver()
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / 'probe.js'
            bundle.write_text("""module.exports.utils={httpRequest:(options, callback)=>{
                if(options.headers.Range!=='bytes=0-131071')throw Error('range');
                const {Readable}=require('stream');
                const body=process.env.SWITCHER_COMPONENT_PROBE.includes('bad')?Buffer.from('<html>bad proxy</html>'):Buffer.concat([Buffer.from('504b0304','hex'),Buffer.alloc(200000)]);
                const response=Readable.from([body]);response.statusCode=200;response.headers={};callback(response);
            }};""", encoding='utf8')
            with mock.patch('codex_switcher.components._driver', return_value=(node, str(bundle), env)):
                result = _probe(URL, threading.Event())
                self.assertEqual(result['status'], 'available')
                self.assertGreater(result['speed_bps'], 0)
                with self.assertRaisesRegex(ComponentError, 'ZIP'):
                    _probe(URL + '?bad', threading.Event())

    def test_probe_timeout_kills_only_owned_probe_process(self):
        process = mock.Mock()
        killed = []
        process.poll.side_effect = lambda: 1 if killed else None
        process.kill.side_effect = lambda: killed.append(True)
        def communicate(**kwargs):
            if killed:
                return ('', '')
            raise subprocess.TimeoutExpired('node-probe', .01)
        process.communicate.side_effect = communicate
        with mock.patch('codex_switcher.components.subprocess.Popen', return_value=process):
            with self.assertRaisesRegex(ComponentError, '超时'):
                _probe(URL, threading.Event(), timeout=.01)
        process.kill.assert_called_once()

    def test_cancel_tree_fallback_error_still_kills_and_waits_owned_parent(self):
        manager = self.manager()
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        process.stdin.write.side_effect = OSError('pipe closed')
        with mock.patch('codex_switcher.components.subprocess.run', side_effect=OSError('taskkill unavailable')):
            manager._stop_process(process)
        process.kill.assert_called_once()

    def test_node_shim_uses_registry_install_and_exact_routes(self):
        from codex_switcher.components import _driver
        node, _, env = _driver()
        env['SWITCHER_COMPONENT_ROUTES'] = json.dumps({'chromium': [GOOGLE, URL]})
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / 'registry.js'
            bundle.write_text("""module.exports.registry={registry:{
                resolveBrowsers:(names, options)=>{if(options.shell!=='no')throw Error('shell');return [{name:'chromium'}]},
                install:async function(items, options){
                    if(options.gc!==false)throw Error('gc');
                    const urls=this._downloadURLs(items[0]);
                    if(urls[0]!==process.env.EXPECT_URL)throw Error('wrong route');
                }
            }};""", encoding='utf8')
            env['EXPECT_URL'] = GOOGLE
            result = subprocess.run([node, '-e', _INSTALL_SCRIPT, str(bundle)], env=env,
                                    input='', text=True, capture_output=True, timeout=5,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('verifying', result.stdout)

    def test_cancelled_installer_cannot_fork_a_retry(self):
        from codex_switcher.components import _driver
        node, _, env = _driver()
        env['SWITCHER_COMPONENT_ROUTES'] = json.dumps({'chromium': [URL]})
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / 'registry.js'
            child = Path(directory) / 'child.js'
            child.write_text('setTimeout(()=>{}, 3000);', encoding='utf8')
            env['TEST_COMPONENT_CHILD'] = str(child)
            bundle.write_text("""module.exports.registry={registry:{
                resolveBrowsers:()=>[],
                install:async()=>new Promise((resolve,reject)=>{
                  const cp=require('child_process');
                  const child=cp.fork(process.env.TEST_COMPONENT_CHILD);
                  child.on('exit',()=>{
                    try { cp.fork(process.env.TEST_COMPONENT_CHILD); console.log('BAD_RETRY'); }
                    catch { console.log('RETRY_BLOCKED'); }
                    reject(Error('cancelled'));
                  });
                  console.log('CHILD_READY');
                })
            }};""", encoding='utf8')
            process = subprocess.Popen([node, '-e', _INSTALL_SCRIPT, str(bundle)], env=env,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                self.assertEqual(process.stdout.readline().strip(), 'CHILD_READY')
                output, errors = process.communicate('cancel\n', timeout=5)
                self.assertEqual(process.returncode, 130, errors)
                self.assertIn('RETRY_BLOCKED', output)
                self.assertNotIn('BAD_RETRY', output)
                self.assertNotIn('verifying', output)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def test_previous_timeout_cannot_cancel_new_install_generation(self):
        manager = self.manager()
        old = manager._cancel
        manager._cancel = threading.Event()
        manager._proc = mock.Mock()
        manager._cancel_download(old)
        self.assertFalse(manager._cancel.is_set())
        manager._proc.stdin.write.assert_not_called()

    def test_active_cleanup_keeps_retry_single_flight(self):
        manager = self.manager(process_factory=mock.Mock())
        manager._state, manager._active = 'failed', True
        manager.start()
        manager._factory.assert_not_called()
        self.assertTrue(manager._active)


if __name__ == '__main__':
    unittest.main()
