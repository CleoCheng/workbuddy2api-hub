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
import wb_proxy_subscriptions as P


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

    def ranked_fixture(self):
        self.mon.cfg.update(ranked=True, allow_cross_country=True)
        self.ips['old-1'] = '8.8.8.1'
        self.delays = {'old-1': 900, 'old-2': 800, 'duplicate': 2000, 'new-1': 100, 'new-2': 200}
        original = self.mon._probe
        def delayed(url, timeout, endpoint=0):
            out = original(url, timeout, endpoint)
            group = {17901:'g1', 17902:'g2', 17990:'Gateway spare probe'}[urllib.parse.urlsplit(url).port]
            if out['ok']:
                out['latency_ms'] = self.delays[self.nodes[group]]
            return out
        self.mon._probe = delayed

    def test_ranking_scans_all_nodes_and_assigns_fastest_unique_exits(self):
        self.ranked_fixture()
        self.mon.cfg['candidate_limit'] = 1  # Legacy random-sample cap must not truncate a full scan.
        self.rounds(1)
        self.assertEqual({self.nodes['g1'], self.nodes['g2']}, {'new-1', 'new-2'})
        ranking = self.mon.view()['ranking']
        self.assertEqual(ranking['done'], len(self.ips))
        self.assertEqual(len(ranking['ranked']), 4)
        self.assertEqual(ranking['assigned'], 2)
        self.assertEqual(self.nodes['Gateway spare probe'], 'REJECT')
        self.assertEqual(len({r['ip'] for r in self.mon.rows.values()}), 2)
        before = list(self.calls)
        self.rounds(1)
        self.assertEqual(before, self.calls, 'Healthy top nodes must stay selected between scans')

    def test_residential_preference_cannot_chain_past_tie_boundary(self):
        rows = [dict(ok=True, node=n, ip=str(i), latency_ms=delay, ip_type=kind)
                for i, (n, delay, kind) in enumerate([
                    ('a', 100, 'datacenter'), ('b', 109, 'residential'),
                    ('c', 118, 'residential'), ('d', 200, 'residential')])]
        ranked = H.rank_exits(rows, 10)
        self.assertEqual([r['node'] for r in ranked], ['b', 'a', 'c', 'd'])
        self.assertEqual([r['node'] for r in H.rank_exits(rows, 0)], ['a', 'b', 'c', 'd'])

    def test_slow_threshold_and_fresh_improvement_are_required(self):
        self.ranked_fixture()
        self.rounds(1)
        self.mon.rows['slot-1']['last_switch'] = 0
        self.delays[self.nodes['g1']] = 2000
        self.rounds(2)
        old = self.nodes['g1']
        self.assertEqual(self.mon.rows['slot-1']['slow_count'], 2)
        self.rounds(1)
        self.assertNotEqual(self.nodes['g1'], old)
        self.assertEqual(self.mon.rows['slot-1']['slow_count'], 0)
        self.assertEqual(self.mon.history[-1]['reason'], 'slow')
        replacement = self.nodes['g1']
        self.mon.rows['slot-1']['last_switch'] = 0
        self.rounds(1)
        self.assertEqual(self.nodes['g1'], replacement, 'Never rotate back using an obsolete fast score')

    def test_ranking_respects_cooldown_country_and_disabled_reservations(self):
        self.ranked_fixture()
        self.slots[1]['enabled'] = False
        S.set_proxy_slots(self.tmp.name, self.slots)
        self.mon.rows['slot-1'] = dict(last_switch=H.time.time())
        self.rounds(1)
        self.assertEqual(self.nodes['g1'], 'old-1')
        self.assertEqual(self.nodes['g2'], 'old-2')
        self.mon.rows['slot-1']['last_switch'] = 0
        self.mon.cfg['allow_cross_country'] = False
        self.slots[0]['country_code'] = 'JP'
        S.set_proxy_slots(self.tmp.name, self.slots)
        self.rounds(1)
        self.assertEqual(self.nodes['g1'], 'old-1')
        self.mon.cfg['allow_cross_country'] = True
        self.rounds(1)
        self.assertEqual(self.nodes['g1'], 'new-1')
        self.assertEqual(self.nodes['g2'], 'old-2')

    def test_cached_exit_ip_change_is_not_committed(self):
        self.ranked_fixture()
        self.rounds(1)
        self.mon.rows['slot-1']['last_switch'] = 0
        old = self.nodes['g1']
        self.delays[old] = 2000
        # Leave no unchanged candidate except the other slot's occupied exit.
        for node in ('old-1', 'old-2', 'duplicate'):
            self.ips[node] = '9.9.9.9'
        self.rounds(3)
        self.assertEqual(self.nodes['g1'], old)
        self.assertTrue(self.mon.allow(self.slots[0]['url']), 'Slow but reachable slots stay available')

    def test_ranked_verification_failure_rolls_back_and_preserves_working_slot(self):
        self.ranked_fixture()
        original = self.mon._probe
        def flaky(url, timeout, endpoint=0):
            if ':17901' in url and self.nodes['g1'].startswith('new-'):
                return dict(ok=False, error='exit_probe_failed')
            return original(url, timeout, endpoint)
        self.mon._probe = flaky
        self.rounds(1)
        self.assertEqual(self.nodes['g1'], 'old-1')
        self.assertTrue(self.mon.allow(self.slots[0]['url']))
        self.assertEqual(self.nodes['Gateway spare probe'], 'REJECT')

    def test_ranking_manual_change_during_scan_aborts_commits(self):
        self.ranked_fixture()
        original = self.mon._candidate_probe
        def edited(cfg, node):
            value = original(cfg, node)
            self.nodes['g2'] = 'duplicate'
            return value
        self.mon._candidate_probe = edited
        self.rounds(1)
        self.assertEqual(self.nodes['g1'], 'old-1')
        self.assertNotIn(('g1', 'new-1'), self.calls)

    def test_ranking_insufficient_unique_exits_never_uses_direct_or_duplicates(self):
        self.ranked_fixture()
        self.ips = {n:'8.8.8.2' for n in self.ips}
        self.rounds(1)
        self.assertEqual(len(self.mon.ranking['ranked']), 1)
        self.assertNotIn(('g1', 'new-1'), self.calls)
        self.assertEqual(self.nodes['g1'], 'old-1')
        self.assertLess(self.mon.ranking['assigned'], 2)

    def test_ranking_config_bounds_and_default_off(self):
        self.assertFalse(H.validate({})['ranked'])
        for field, value in [('ranked', 1), ('rank_interval', 299), ('slow_ms', True),
                             ('slow_failures', 1), ('residential_tie_pct', 21), ('improvement_pct', 91)]:
            with self.assertRaises(ValueError):
                H.validate({field:value})

    def test_manual_choice_is_retained_until_sustained_slow_health_takeover(self):
        self.ranked_fixture()
        self.rounds(1)
        result = self.mon.manual_switch('slot-1', 'old-1')
        self.assertTrue(result['ok'])
        self.assertTrue(self.mon.rows['slot-1']['manual'])
        self.rounds(2)
        self.assertEqual(self.nodes['g1'], 'old-1', 'Ranking must respect the explicit manual choice')
        self.mon.rows['slot-1']['last_switch'] = 0
        self.delays['old-1'] = 2000
        self.rounds(3)
        self.assertNotEqual(self.nodes['g1'], 'old-1')
        self.assertFalse(self.mon.rows['slot-1']['manual'])
        self.assertEqual(self.mon.history[-1]['reason'], 'slow')

    def test_manual_candidates_mark_occupied_exits_and_recheck_live_ip(self):
        self.ranked_fixture()
        self.rounds(1)
        data = self.mon.node_choices('slot-1')
        occupied = next(n for n in data['nodes'] if n['node'] == self.nodes['g2'])
        self.assertFalse(occupied['selectable'])
        self.assertEqual(occupied['unavailable_reason'], 'exit_occupied')
        self.assertNotIn(self.nodes['g1'], [n['node'] for n in data['nodes']])
        old = self.nodes['g1']
        self.ips['old-1'] = self.ips[self.nodes['g2']]
        with self.assertRaises(ValueError):
            self.mon.manual_switch('slot-1', 'old-1')
        self.assertEqual(self.nodes['g1'], old)
        self.assertEqual(self.nodes['Gateway spare probe'], 'REJECT')

    def test_manual_choice_requires_known_peers_and_serializes_with_health(self):
        self.ranked_fixture()
        self.rounds(1)
        self.mon.run_lock.acquire()
        try:
            with self.assertRaisesRegex(ValueError, 'busy'):
                self.mon.manual_switch('slot-1', 'old-1')
        finally:
            self.mon.run_lock.release()
        old = self.nodes['g1']
        self.ips[self.nodes['g2']] = None
        with self.assertRaisesRegex(ValueError, 'peer_exit_unknown'):
            self.mon.manual_switch('slot-1', 'old-1')
        self.assertEqual(self.nodes['g1'], old)
        self.assertTrue(self.mon.allow(self.slots[1]['url']), 'Manual probes must not bypass the failure threshold and block a peer')
        self.assertEqual(self.mon.rows['slot-2']['status'], 'suspect')
        self.assertIn(self.slots[1]['url'], self.mon.pending)

    def test_manual_peer_uses_secondary_endpoint_before_refusing_unique_exit(self):
        self.ranked_fixture()
        self.rounds(1)
        original = self.mon._probe
        def primary_failed(url, timeout, endpoint=0):
            if ':17902' in url and not endpoint:
                return dict(ok=False, error='exit_probe_failed')
            return original(url, timeout, endpoint)
        self.mon._probe = primary_failed
        result = self.mon.manual_switch('slot-1', 'old-1')
        self.assertTrue(result['ok'])
        self.assertEqual(self.mon.rows['slot-2']['status'], 'healthy')

    def test_resume_automatic_and_rejected_peers_can_recover_after_subscription_change(self):
        self.ranked_fixture()
        self.rounds(1)
        self.mon.rows['slot-1']['manual'] = True
        with mock.patch.object(self.mon, 'start'):
            self.mon.manual_switch('slot-1', automatic=True)
        self.assertFalse(self.mon.rows['slot-1']['manual'])
        for sid, group in [('slot-1','g1'), ('slot-2','g2')]:
            self.nodes[group] = 'REJECT'
            self.mon.rows[sid].update(status='unavailable', last_success=0, ip='', last_switch=0)
        self.rounds(1)
        self.assertEqual(len({r['ip'] for r in self.mon.rows.values()}), 2)
        self.assertTrue(all(r['status'] == 'healthy' for r in self.mon.rows.values()))

    def test_same_region_candidate_prioritization(self):
        slot = {'country_code': 'US', 'country': '美国'}
        candidates = [
            {'node': 'hk-1', 'country_code': 'HK', 'latency_ms': 200, 'ok': True},
            {'node': 'us-1', 'country_code': 'US', 'latency_ms': 400, 'ok': True},
            {'node': 'us-slow', 'country_code': 'US', 'latency_ms': 2000, 'ok': True},
        ]
        sorted_c = H.prioritize_same_region_candidates(candidates, slot, slow_ms=1500)
        self.assertEqual(sorted_c[0]['node'], 'us-1', 'Low latency same region node must be prioritized first')
        self.assertEqual(sorted_c[1]['node'], 'hk-1', 'Other region comes second if same region has high latency')
        self.assertEqual(sorted_c[2]['node'], 'us-slow')

    def test_pinned_slot_is_not_rotated_by_ranked_check(self):
        self.ranked_fixture()
        self.rounds(1)
        self.slots[0]['pinned'] = True
        self.mon.rows['slot-1']['pinned'] = True
        old = self.nodes['g1']
        self.rounds(2)
        self.assertEqual(self.nodes['g1'], old, 'Pinned slot must retain its node through ranked check')


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'mihomo.json'
        self.doc = {'secret':'private-controller', 'listeners':[{'port':17901}],
            'proxy-providers':{
                'a':{'type':'http','url':'https://one.example/path-private-token','interval':3600,'path':'./a.yaml'},
                'b':{'type':'http','url':'https://two.example/sub?token=query-private-token','interval':3600,'path':'./b.yaml'}},
            'proxy-groups':[dict(name=g,type='select',proxies=['REJECT'],use=['a','b'])
                            for g in ('g1','g2','Gateway spare probe')],
            'rules':['MATCH,REJECT']}
        self.path.write_text(json.dumps(self.doc), encoding='utf-8')
        self.env = mock.patch.dict(P.os.environ, WB_MIHOMO_CONFIG=str(self.path))
        self.env.start();self.addCleanup(self.env.stop)
        self.validate = mock.patch.object(P, '_validate_native')
        self.validate.start();self.addCleanup(self.validate.stop)
        hub = types.SimpleNamespace(ACCOUNTS_DIR=str(Path(self.tmp.name)/'accounts'),log=lambda s:None)
        self.mon = H.Monitor(hub);self.addCleanup(self.mon.stop)
        self.mon.cfg.update(groups={'slot-1':'g1','slot-2':'g2'})
        self.mon.trigger = mock.Mock()
        self.live = copy.deepcopy(self.doc)
        self.selections = {'g1':'a-node','g2':'b-node','Gateway spare probe':'REJECT'}
        self.mon.rows = {s:dict(id=s,url='http://127.0.0.1:17901',status='healthy',ip='8.8.8.8')
                         for s in ('slot-1','slot-2')}
        self.mon._api = self.api
        self.mon._select = lambda cfg,g,n:self.selections.update({g:n})
        self.reject_new = False

    def api(self, cfg, path, body=None):
        if path == '/configs?force=true':
            data = json.loads(body['payload'])
            if self.reject_new and 'c' in data['proxy-providers']:
                raise OSError('private-url-token upstream')
            self.live = data
            for g in self.selections:self.selections[g] = 'REJECT'
            return {}
        if path == '/providers/proxies':
            return {'providers':{name:{'proxies':[{'name':name+'-node'}]} for name in self.live['proxy-providers']}}
        if path == '/proxies':
            return {'proxies':{g['name']:dict(type='Selector',now=self.selections[g['name']],
                all=['REJECT']+[n+'-node' for n in g['use']]) for g in self.live['proxy-groups']}}
        if path.startswith('/providers/proxies/') and body is not None:return {}
        raise AssertionError(path)

    def test_subscription_view_never_returns_token_path_or_controller_secret(self):
        text = json.dumps(P.view(self.mon))
        for secret in ('private-controller','path-private-token','query-private-token','token='):
            self.assertNotIn(secret, text)
        self.assertIn('one.example', text)
        self.assertEqual(len(P.view(self.mon)['providers']), 2)

    def test_new_subscription_joins_all_existing_groups_without_listener_changes(self):
        P.change(self.mon, dict(name='c',url='https://three.example/sub?token=new-private',enabled=True,interval=1800))
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['listeners'], self.doc['listeners'])
        self.assertEqual(saved['rules'], self.doc['rules'])
        self.assertTrue(all(g['use']==['a','b','c'] for g in saved['proxy-groups']))
        self.assertEqual(self.selections['g1'], 'a-node')
        self.assertEqual(saved['proxy-providers']['c']['override']['additional-prefix'], '[c] ')
        self.assertEqual(saved['proxy-providers']['c']['proxy'], 'DIRECT', 'Subscription downloads must not depend on a working account exit')
        self.assertEqual(saved['proxy-providers']['c']['size-limit'], 10 * 1024 * 1024)
        self.assertEqual(len(list((self.mon.root/'config-backups').glob('*.json'))), 1)
        self.mon.trigger.assert_called_once_with(rescan=True)

    def test_blank_url_retained_and_disable_marks_removed_selected_exit_unavailable(self):
        P.change(self.mon, dict(name='a',url='',enabled=False,interval=7200))
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['proxy-providers']['a']['url'], self.doc['proxy-providers']['a']['url'])
        self.assertEqual(self.selections['g1'], 'REJECT')
        self.assertEqual(self.mon.rows['slot-1']['status'], 'unavailable')
        self.assertEqual(self.selections['g2'], 'b-node')

    def test_apply_failure_restores_config_selections_and_health_without_secrets(self):
        self.reject_new = True
        before = copy.deepcopy(self.mon.rows)
        with self.assertRaisesRegex(ValueError, '^subscription_apply_failed_rolled_back$'):
            P.change(self.mon, dict(name='c',url='https://bad.example/private-url-token'))
        self.assertEqual(json.loads(self.path.read_text()), self.doc)
        self.assertEqual(self.selections['g1'], 'a-node')
        self.assertEqual(self.mon.rows, before)

    def test_validation_and_busy_guards_preserve_original_config(self):
        for url in ('http://example.com/sub', 'https://127.0.0.1/x', 'https://localhost/x',
                    'https://user:password@example.com/sub', 'https://example.com/x#private'):
            with self.assertRaises(ValueError):P.change(self.mon, dict(name='c',url=url))
        self.mon.run_lock.acquire()
        try:
            with self.assertRaisesRegex(ValueError, 'busy'):P.change(self.mon, dict(name='c',url='https://three.example/sub'))
        finally:self.mon.run_lock.release()
        self.assertEqual(json.loads(self.path.read_text()), self.doc)
        with mock.patch.dict(P.os.environ, WB_MIHOMO_CONFIG=''):
            self.assertFalse(P.view(self.mon)['available'])

    def test_refresh_preserves_selection_and_never_sends_url_to_panel(self):
        result = P.change(self.mon, dict(name='a'), refresh=True)
        self.assertEqual(self.selections['g1'], 'a-node')
        self.assertNotIn('path-private-token', json.dumps(result))
        self.mon.trigger.assert_called_once_with(rescan=True)

    def test_delete_subscription_removes_from_groups_and_reloads(self):
        P.change(self.mon, dict(name='c',url='https://three.example/sub?token=new',enabled=True,interval=1800))
        result = P.delete(self.mon, dict(name='c'))
        self.assertEqual(len(result['providers']), 2)
        saved = json.loads(self.path.read_text())
        self.assertNotIn('c', saved['proxy-providers'])
        self.assertTrue(all('c' not in g['use'] for g in saved['proxy-groups']))

    def test_delete_last_subscription_fails(self):
        P.delete(self.mon, dict(name='a'))
        with self.assertRaisesRegex(ValueError, 'at_least_one_subscription_required'):
            P.delete(self.mon, dict(name='b'))

    def test_nodes_lists_subscription_proxies_with_latency(self):
        self.mon.ranking = {'ranked': [{'node': 'a-node', 'latency_ms': 123, 'ip': '1.2.3.4'}]}
        data = P.nodes(self.mon, 'a')
        self.assertEqual(data['name'], 'a')
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['nodes'][0]['name'], 'a-node')
        self.assertEqual(data['nodes'][0]['latency_ms'], 123)
        self.assertEqual(data['nodes'][0]['ip'], '1.2.3.4')

    def test_scanning_interrupted_by_cancel_scan(self):
        self.mon.ranking['scanning'] = True
        self.mon.cancel_scan()
        self.assertTrue(self.mon.scan_cancel.is_set())


if __name__ == '__main__':
    unittest.main()
