"""Cross-platform key boundaries, weighted routing, retry and worker isolation."""
import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wb_platforms as P
import wb_settings as S
import wb_proxy as W
import wb_qoder_worker as Q


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hub = types.SimpleNamespace(ACCOUNTS_DIR=self.temp.name)
        self.manager = P.Manager(self.hub)
        self.addCleanup(self.manager.stop)

    def test_legacy_keys_stay_on_workbuddy(self):
        self.assertEqual(S.key_platforms({}), ['workbuddy'])
        S.set_api_keys(self.temp.name, [{'key': 'test-old-key'}])
        self.assertEqual(S.api_keys(self.temp.name)[0]['platforms'], ['workbuddy'])

    def test_platform_scope_roundtrip(self):
        S.set_api_keys(self.temp.name, [{'key': 'test-both-key',
                                       'platforms': ['qoder', 'workbuddy']}])
        self.assertEqual(S.match_api_key(self.temp.name, 'test-both-key')['platforms'],
                         ['workbuddy', 'qoder'])

    def test_invalid_platforms_never_expand_access(self):
        for value in (None, [], 'qoder', ['unknown'], ['workbuddy', 'unknown']):
            with self.assertRaises(ValueError):
                S.validate_key_platforms(value)
        self.assertEqual(S.key_platforms({'platforms': 'qoder'}), [])

    def test_account_namespace(self):
        entry = {'accounts': ['wb-one', 'qoder/q-one', 'workbuddy/wb-two']}
        self.assertEqual(P.account_scope(entry, 'qoder'), ['q-one'])
        self.assertEqual(P.account_scope(entry, 'workbuddy'), ['wb-one', 'wb-two'])
        self.assertEqual(P.account_scope({'accounts': ['wb-one']}, 'qoder'), [])

    def test_models_do_not_change_generation(self):
        self.assertTrue(P.model_match('Claude-Sonnet-4.5', {'id': 'claude-sonnet-4-5'}))
        self.assertFalse(P.model_match('Claude-Sonnet-4.5', {'id': 'Claude-Sonnet-4.6'}))
        self.assertFalse(P.model_match('', {'id': 'anything'}))

    def test_session_scope_and_stable_history(self):
        payload = {'model': 'model-1', 'messages': [{'role': 'user', 'content': 'hello'}]}
        first = P.session_identity({}, payload, 'key1', '/v1/chat/completions')
        payload['messages'] += [{'role': 'assistant', 'content': 'hi'},
                                {'role': 'user', 'content': 'again'}]
        self.assertEqual(first, P.session_identity({}, payload, 'key1', '/v1/chat/completions'))
        self.assertNotEqual(first, P.session_identity({}, payload, 'key2', '/v1/chat/completions'))

    def test_affinity_is_scoped_and_expires(self):
        choices = [{'provider': 'qoder', 'realm': 'cn', 'weight': 1},
                   {'provider': 'workbuddy', 'realm': 'cn', 'weight': 3}]
        self.manager.remember('session', choices[0])
        self.assertEqual(self.manager.ordered(choices, 'session')[0], choices[0])
        self.assertEqual(self.manager.ordered(choices[1:], 'session'), choices[1:])
        with mock.patch.object(P.random, 'choices', return_value=[choices[1]]) as rng:
            # Only inspect the first choice; the second draw has one item left.
            rng.side_effect = [[choices[1]], [choices[0]]]
            self.manager.ordered(choices, 'new-session')
            self.assertEqual(rng.call_args_list[0].kwargs['weights'], [1, 3])

    def test_proxy_binding_missing_slot_is_fail_closed(self):
        S.save(self.temp.name, {'qoder_proxy_slots': {'uid': 'missing'}})
        self.manager.save_bindings()
        self.assertIsNone(json.loads(self.manager.proxy_file.read_text())['uid'])

    def test_worker_disabled_by_default(self):
        with self.assertRaises(RuntimeError):
            self.manager.start()
        self.assertIsNone(self.manager.process)

    def test_worker_checkin_preserves_static_helpers_and_proxy_context(self):
        context = threading.local()
        context.uid = 'previous-account'

        class Account:
            uid = 'qoder-account'

            @staticmethod
            def campaign_label(campaign):
                return campaign['title']

            @classmethod
            def account_kind(cls):
                return cls.__name__

            @property
            def label(self):
                return 'account label'

            def campaign_checkin(self, campaign):
                return self.campaign_label(campaign), context.uid

            def failing_request(self):
                raise RuntimeError(context.uid)

        Q._wrap_account_methods(Account, context)
        account = Account()
        self.assertEqual(account.campaign_checkin({'title': 'Daily credits'}),
                         ('Daily credits', 'qoder-account'))
        self.assertEqual(Account.campaign_label({'title': 'Daily credits'}),
                         'Daily credits')
        self.assertEqual(account.account_kind(), 'Account')
        self.assertEqual(account.label, 'account label')
        self.assertEqual(context.uid, 'previous-account')
        with self.assertRaisesRegex(RuntimeError, 'qoder-account'):
            account.failing_request()
        self.assertEqual(context.uid, 'previous-account')

    def test_private_worker_boot_and_auth(self):
        import http.client
        S.save(self.temp.name, {'qoder_enabled': True})
        self.manager.start()
        self.assertEqual(self.manager.qoder_status(), [])
        self.assertGreater(len(self.manager.qoder_models('cn')), 0)
        conn = http.client.HTTPConnection('127.0.0.1', self.manager.port, timeout=5)
        try:
            conn.request('GET', '/accounts')
            response = conn.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
        finally:
            conn.close()

    def test_management_requires_panel_session(self):
        handler = types.SimpleNamespace(path='/platforms/status',
                                        _panel_ok=lambda: False,
                                        _error=mock.Mock())
        self.assertTrue(P.panel_route(self.hub, handler, 'GET'))
        self.assertEqual(handler._error.call_args.args[0], 401)
        self.assertIsNone(self.manager.process)

    def test_workbuddy_only_path_is_unchanged(self):
        handler = types.SimpleNamespace(key_entry={'platforms': ['workbuddy']})
        self.assertFalse(P.dispatch(self.hub, handler, '/v1/responses', {}))

    def _dispatch_fixture(self):
        self.hub.BLOCK_BACKGROUND_REQUESTS = False
        self.hub.CHAT_SLOT_WAIT_SECONDS = 0
        self.hub._chat_slots = threading.BoundedSemaphore(1)
        self.hub._acquire_key_slot = mock.Mock(return_value=(True, 1, 1))
        self.hub._release_key_slot = mock.Mock()
        handler = types.SimpleNamespace(
            key_entry={'id': 'key-a', 'platforms': ['workbuddy', 'qoder']},
            headers={}, _key_id=lambda: 'key-a',
            _banned_model_error=lambda model: '',
            _key_model_error=lambda model: '', _error=mock.Mock())
        return handler

    def test_previous_response_id_rejected_before_any_backend(self):
        handler = self._dispatch_fixture()
        with mock.patch.object(P, 'manager', return_value=self.manager):
            P.dispatch(self.hub, handler, '/v1/responses',
                       {'model': 'm', 'previous_response_id': 'resp_x'})
        self.assertEqual(handler._error.call_args.args[0], 400)
        self.hub._acquire_key_slot.assert_not_called()

    def test_empty_candidates_release_all_limits(self):
        handler = self._dispatch_fixture()
        with mock.patch.object(P, 'manager', return_value=self.manager), \
                mock.patch.object(self.manager, 'candidates', return_value=[]):
            self.assertTrue(P.dispatch(self.hub, handler, '/v1/chat/completions', {'model': 'm'}))
        self.assertEqual(handler._error.call_args.args[0], 503)
        self.hub._release_key_slot.assert_called_once()
        self.assertTrue(self.hub._chat_slots.acquire(blocking=False))

    def test_transient_failure_moves_to_next_platform(self):
        handler = self._dispatch_fixture()
        choices = [{'provider': 'workbuddy', 'realm': 'cn', 'model': 'm', 'weight': 1},
                   {'provider': 'workbuddy', 'realm': 'intl', 'model': 'm', 'weight': 1}]
        with mock.patch.object(P, 'manager', return_value=self.manager), \
                mock.patch.object(self.manager, 'candidates', return_value=choices), \
                mock.patch.object(self.manager, 'ordered', return_value=choices), \
                mock.patch.object(P, '_dispatch_workbuddy', side_effect=[P.ProviderFailure(503, b''), None]) as send:
            P.dispatch(self.hub, handler, '/v1/chat/completions', {'model': 'm'})
        self.assertEqual(send.call_count, 2)
        handler._error.assert_not_called()

    def test_transport_failure_after_headers_does_not_replay(self):
        handler = self._dispatch_fixture()
        choice = {'provider': 'workbuddy', 'realm': 'cn', 'model': 'm', 'weight': 1}
        def started(*args):
            handler._platform_started = True
            raise ConnectionResetError('after headers')
        with mock.patch.object(P, 'manager', return_value=self.manager), \
                mock.patch.object(self.manager, 'candidates', return_value=[choice, choice]), \
                mock.patch.object(P, '_dispatch_workbuddy', side_effect=started) as send:
            P.dispatch(self.hub, handler, '/v1/chat/completions', {'model': 'm'})
        self.assertEqual(send.call_count, 1)
        self.assertTrue(handler.close_connection)
        self.hub._release_key_slot.assert_called_once()

    def test_forbidden_explicit_platform_never_inspects_pools(self):
        handler = types.SimpleNamespace(key_entry={'platforms': ['qoder']})
        with self.assertRaises(P.ProviderFailure) as raised:
            self.manager.candidates(handler, '/v1/chat/completions', {'model': 'workbuddy/m'})
        self.assertEqual(raised.exception.status, 403)

    def test_qoder_candidates_obey_account_and_model_scope(self):
        handler = types.SimpleNamespace(
            key_entry={'platforms': ['qoder'], 'accounts': ['qoder/allowed']},
            _request_realm=lambda: 'cn')
        self.hub.pinned_account = lambda: None
        S.save(self.temp.name, {'qoder_enabled': True})
        accounts = [{'uid': 'allowed', 'ready': True, 'realm': 'cn'},
                    {'uid': 'other', 'ready': True, 'realm': 'cn'}]
        with mock.patch.object(self.manager, 'qoder_status', return_value=accounts), \
                mock.patch.object(self.manager, 'qoder_models', return_value=[{'id': 'Model-1'}]):
            result = self.manager.candidates(handler, '/v1/chat/completions', {'model': 'model1'})
            self.assertEqual(result[0]['weight'], 1)
            self.assertEqual(self.manager.candidates(handler, '/v1/chat/completions', {'model': 'Model-2'}), [])

    def test_cold_account_model_is_not_selected(self):
        handler = types.SimpleNamespace(key_entry={'platforms': ['qoder']}, _request_realm=lambda: 'cn')
        self.hub.pinned_account = lambda: None
        S.save(self.temp.name, {'qoder_enabled': True})
        with mock.patch.object(self.manager, 'qoder_status', side_effect=[
                [{'uid': 'a', 'ready': True, 'realm': 'cn'}],
                [{'uid': 'a', 'ready': False, 'realm': 'cn'}]]), \
                mock.patch.object(self.manager, 'qoder_models', return_value=[{'id': 'm'}]):
            self.assertEqual(self.manager.candidates(handler, '/v1/responses', {'model': 'm'}), [])

    def test_worker_accepts_hub_session_and_tracks_key_identity(self):
        S.save(self.temp.name, {'qoder_enabled': True})
        self.manager.start()
        conn, response = self.manager.request('/v1/chat/completions', 'POST',
            json.dumps({'model': 'not-a-model', 'messages': [{'role': 'user', 'content': 'test'}]}),
            {'X-Hub-Key-Id': 'key-a', 'X-Hub-Allowed-Accounts': '[]'})
        try:
            self.assertNotEqual(response.status, 401)
            response.read()
        finally:
            conn.close()

    def test_only_retry_before_headers(self):
        handler = types.SimpleNamespace(key_entry={}, _error=mock.Mock(),
            send_response=mock.Mock(), send_header=mock.Mock(),
            _request_realm=lambda: 'cn')
        def failure(path, payload):
            handler._error(503, 'busy')
        handler._dispatch_chat_post = failure
        with self.assertRaises(P.ProviderFailure):
            P._dispatch_workbuddy(W, handler, '/v1/chat/completions', {},
                                  {'realm': 'cn'})
        def partial(path, payload):
            handler.send_response(200)
            handler._error(503, 'partial')
        handler._dispatch_chat_post = partial
        P._dispatch_workbuddy(W, handler, '/v1/chat/completions', {}, {'realm': 'cn'})
        handler._error.assert_called_once_with(503, 'partial')


if __name__ == '__main__':
    unittest.main()
