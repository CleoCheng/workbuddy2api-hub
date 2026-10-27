"""Proxy failover invariants with fake exits/controller; no upstream accounts."""
import copy
import http.client
import json
import socket
import ssl
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wb_proxy_health as H
import wb_settings as S
import wb_accounts as A
import wb_upstream_pool as U


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hub = types.SimpleNamespace(ACCOUNTS_DIR=self.tmp.name, log=lambda msg: None)
        self.mon = H.Monitor(self.hub)
        self.addCleanup(self.mon.stop)
        self.slots = S.set_proxy_slots(self.tmp.name, [
            dict(id='slot-1', url='http://127.0.0.1:17901', enabled=True,
                 ip='8.8.8.1', country_code='US'),
            dict(id='slot-2', url='http://127.0.0.1:17902', enabled=True,
                 ip='8.8.8.2', country_code='US')])
        self.mon.cfg = H.validate(dict(H.DEFAULTS, enabled=True, failures=2,
                          groups={'slot-1': 'g1', 'slot-2': 'g2'}))
        self.nodes = {'g1': 'old-1', 'g2': 'old-2', 'Gateway spare probe': 'REJECT'}
        self.ips = {'old-1': None, 'old-2': '8.8.8.2', 'duplicate': '8.8.8.2',
                    'new-1': '8.8.8.3', 'new-2': '8.8.8.4'}
        self.calls = []
        self.providers = {'sub': {'vehicleType': 'HTTP', 'proxies': [
            dict(name=n, alive=True) for n in self.ips]}}
        self.mon._api = self.api
        self.mon._select = self.select
        self.mon._probe = self.probe
        self.mon.rows['slot-2'] = dict(ip='8.8.8.2', ip_node='old-2', last_success=1)
        self.intel = mock.patch.object(H.wb_ipintel, 'lookup', return_value={
            'country_code': 'US', 'country': 'USA', 'isp': '', 'asn': '', 'ip_type': 'datacenter'})
        self.intel.start()
        self.addCleanup(self.intel.stop)
        self.shuffle = mock.patch.object(H.random, 'shuffle', lambda rows: None)
        self.shuffle.start()
        self.addCleanup(self.shuffle.stop)

    def api(self, cfg, path, body=None):
        if path == '/proxies':
            return {'proxies': {g: dict(type='Selector', now=n, all=['REJECT'] + list(self.ips))
                                for g, n in self.nodes.items()}}
        if path == '/providers/proxies':
            return {'providers': copy.deepcopy(self.providers)}
        raise AssertionError(path)

    def select(self, cfg, group, node):
        self.calls.append((group, node))
        self.nodes[group] = node

    def probe(self, url, timeout, endpoint=0):
        group = {17901: 'g1', 17902: 'g2', 17990: 'Gateway spare probe'}[
            urllib.parse.urlsplit(url).port]
        ip = self.ips[self.nodes[group]] if self.nodes[group] != 'REJECT' else None
        return dict(ok=True, ip=ip, latency_ms=12) if ip else dict(ok=False, error='exit_probe_failed')

    def rounds(self, count=2):
        for _ in range(count):
            self.mon.check()

    def test_defaults_disabled_and_loopback_validation(self):
        self.assertFalse(H.config(self.tmp.name)['enabled'])
        for value in ('http://example.com:9090', 'http://127.0.0.1:9090/x',
                      'http://user:pass@127.0.0.1:9090', 'https://127.0.0.1:9090'):
            with self.assertRaises(ValueError):
                H.validate(dict(controller=value))
        for value in (True, 1, 3601):
            with self.assertRaises(ValueError):
                H.validate(dict(interval=value))
        with self.assertRaises(ValueError):
            H.validate(dict(groups={'a': 'same', 'b': 'same'}))

    def test_failover_avoids_same_ip_and_preserves_bindings_and_port(self):
        self.rounds()
        self.assertEqual(self.nodes['g1'], 'new-1')
        self.assertNotIn(('g1', 'duplicate'), self.calls)
        self.assertEqual(self.nodes['g2'], 'old-2')
        self.assertEqual(self.nodes['Gateway spare probe'], 'REJECT')
        slots = S.proxy_slots(self.tmp.name)
        self.assertEqual(slots[0]['url'], self.slots[0]['url'])
        self.assertEqual(slots[0]['id'], 'slot-1')
        self.assertEqual(slots[0]['ip'], '8.8.8.3')
        self.assertTrue(self.mon.allow(slots[0]['url']))
        self.assertGreater(self.mon.generation(slots[0]['url']), 0)

    def test_single_failure_does_not_rotate(self):
        self.rounds(1)
        self.assertEqual(self.nodes['g1'], 'old-1')
        self.assertEqual(self.mon.rows['slot-1']['status'], 'suspect')

    def test_secondary_endpoint_prevents_false_positive(self):
        original = self.mon._probe
        self.mon._probe = lambda url, timeout, endpoint=0: (
            dict(ok=True, ip='8.8.8.1', latency_ms=1)
            if endpoint else original(url, timeout))
        self.rounds()
        self.assertFalse(self.calls)
        self.assertEqual(self.mon.rows['slot-1']['status'], 'healthy')

    def test_no_spare_blocks_new_requests_without_direct_fallback(self):
        self.providers['sub']['proxies'] = [{'name': 'duplicate', 'alive': True}]
        self.rounds()
        self.assertFalse(self.mon.allow(self.slots[0]['url']))
        self.assertTrue(self.mon.allow(''))
        self.assertEqual(self.nodes['g1'], 'old-1')
        self.assertEqual(self.mon.rows['slot-1']['error'], 'no_unique_healthy_spare')
        with mock.patch.object(A, 'PROXY_HEALTH', self.mon):
            with self.assertRaises(H.ProxyUnavailable):
                A.urlopen(urllib.request.Request('https://example.com'), proxy=self.slots[0]['url'])

    def test_other_country_rejected_unless_enabled(self):
        H.wb_ipintel.lookup.return_value['country_code'] = 'JP'
        self.rounds()
        self.assertEqual(self.nodes['g1'], 'old-1')
        self.mon.cfg['allow_cross_country'] = True
        self.rounds(1)
        self.assertEqual(self.nodes['g1'], 'new-1')

    def test_cooldown_and_manual_changes_are_respected(self):
        self.mon.rows['slot-1'] = dict(last_switch=H.time.time(), failures=1)
        self.rounds(1)
        self.assertEqual(self.mon.rows['slot-1']['error'], 'switch_cooldown')
        self.assertFalse(self.calls)
        self.mon.rows['slot-1']['last_switch'] = 0
        original = self.mon._candidate_probe
        def edited(cfg, node):
            value = original(cfg, node)
            self.nodes['g2'] = 'new-2'
            return value
        self.mon._candidate_probe = edited
        self.rounds(1)
        self.assertEqual(self.nodes['g1'], 'old-1')
        self.assertIn('configuration_changed', self.mon.rows['slot-1']['error'])

    def test_concurrent_checks_cannot_allocate_same_exit(self):
        entered, release = threading.Event(), threading.Event()
        original = self.mon._probe
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)
        self.mon._probe = blocked
        t = threading.Thread(target=self.mon.check)
        t.start()
        self.assertTrue(entered.wait(2))
        self.mon.check()  # Nonblocking: one controller owns the spare listener.
        with self.assertRaisesRegex(ValueError, 'running'):
            self.mon.configure(self.mon.cfg)
        release.set()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(self.mon.rows['slot-1']['failures'], 1)

    def test_two_failed_slots_reserve_different_replacements(self):
        self.ips['old-1'] = '8.8.8.1'
        self.rounds(1)
        self.ips.update({'old-1': None, 'old-2': None})
        self.rounds(2)
        self.assertNotIn(self.nodes['g1'], ('old-1', 'old-2'))
        self.assertNotIn(self.nodes['g2'], ('old-1', 'old-2'))
        # A healthy spare may keep this slot's former IP; only peer IPs are reserved.
        self.assertNotEqual(self.ips[self.nodes['g1']], self.ips[self.nodes['g2']])
        self.assertEqual(len({r['ip'] for r in self.mon.rows.values()}), 2)

    def test_blocked_slot_does_not_reopen_between_failed_checks(self):
        self.providers['sub']['proxies'] = []
        self.rounds(2)
        original = self.mon._api
        def controller(*args):
            self.assertFalse(self.mon.allow(self.slots[0]['url']))
            return original(*args)
        self.mon._api = controller
        self.rounds(1)
        self.assertFalse(self.mon.allow(self.slots[0]['url']))

    def test_qoder_stream_read_failure_reports_without_retry(self):
        response = mock.MagicMock()
        response.readline.side_effect = [b'data: first\n', TimeoutError('idle')]
        notify = mock.Mock()
        observed = H.ObservedResponse(response, notify)
        with observed:
            self.assertEqual(next(observed), b'data: first\n')
            with self.assertRaises(TimeoutError):
                next(observed)
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(response.readline.call_count, 2)

    def test_controller_failure_does_not_change_selectors(self):
        self.mon._api = mock.Mock(side_effect=OSError())
        self.rounds()
        self.assertFalse(self.calls)
        self.assertFalse(self.mon.allow(self.slots[0]['url']))

    def test_switch_verification_failure_rolls_back(self):
        original = self.mon._probe
        def flaky(url, timeout, endpoint=0):
            if ':17901' in url and self.nodes['g1'].startswith('new-'):
                return dict(ok=False, error='exit_probe_failed')
            return original(url, timeout, endpoint)
        self.mon._probe = flaky
        self.rounds()
        self.assertEqual(self.nodes['g1'], 'old-1')
        self.assertFalse(self.mon.allow(self.slots[0]['url']))

    def test_disabled_slots_still_reserve_ips(self):
        self.slots[1]['enabled'] = False
        S.set_proxy_slots(self.tmp.name, self.slots)
        self.rounds()
        self.assertEqual(self.nodes['g1'], 'new-1')
        self.assertNotIn(('g2', 'new-1'), self.calls)

    def test_error_classification_and_signal_debounce(self):
        for error in (TimeoutError(), ConnectionResetError(), socket.gaierror(),
                      urllib.error.URLError(TimeoutError()), http.client.IncompleteRead(b'x')):
            self.assertTrue(H.transport_failure(error))
        for code in (401, 403, 429, 500, 502, 503, 504):
            e = urllib.error.HTTPError('https://example.com', code, '', {}, None)
            self.assertEqual(H.transport_failure(e), code in (502, 503, 504))
            e.close()
        self.assertFalse(H.transport_failure(ssl.SSLCertVerificationError()))
        self.mon.notify(self.slots[0]['url'], TimeoutError())
        self.mon.notify(self.slots[0]['url'], TimeoutError())
        self.assertEqual(len(self.mon.pending), 1)
        self.assertTrue(self.mon.wake.is_set())

    def test_worker_signals_block_and_report_without_exposing_secret(self):
        self.rounds()
        self.mon.rows['slot-1']['status'] = 'unavailable'
        self.mon._persist()
        worker = H.WorkerSignals(self.mon.root)
        self.assertFalse(worker.allow(self.slots[0]['url']))
        worker.notify(self.slots[0]['url'], TimeoutError())
        worker.notify(self.slots[0]['url'], TimeoutError())
        files = list((self.mon.root / 'events').glob('*.json'))
        self.assertEqual(len(files), 1)
        self.assertNotIn('secret', json.loads(files[0].read_text()))
        self.assertNotIn('secret', self.mon.view())

    def test_panel_auth_and_config_secret_not_returned(self):
        h = types.SimpleNamespace(path='/proxy/health', _panel_ok=lambda: False,
                                  _error=mock.Mock())
        self.assertTrue(H.route(self.hub, h, 'GET'))
        h._error.assert_called_once_with(401, 'panel password required')
        h.path = '/other'
        self.assertFalse(H.route(self.hub, h, 'GET'))

    def test_connection_generation_avoids_old_idle_connections(self):
        request = urllib.request.Request('https://example.com')
        request.wb_proxy_generation = 42
        conn = mock.Mock()
        with mock.patch.object(U._POOL, 'acquire', return_value=(conn, False)) as acquire, \
             mock.patch.object(U, '_exchange', return_value=mock.Mock(status=200)), \
             mock.patch.object(U, '_fold_bypassed', return_value=({'host':'127.0.0.1','port':17901}, self.slots[0]['url'])):
            U.urlopen(request, proxy=self.slots[0]['url'])
        self.assertIn(':exit-42', acquire.call_args.args[0][-1])


if __name__ == '__main__':
    unittest.main()
