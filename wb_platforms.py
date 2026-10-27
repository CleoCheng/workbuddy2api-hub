"""Opt-in multi-platform routing with a private, bundled Qoder worker.

No third-party Python dependencies. Client keys never leave the hub; a random
per-process secret authenticates the worker. Provider retries stop at headers.
"""
import atexit
import hashlib
import http.client
import json
import os
import random
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import wb_settings

PLATFORMS = ('workbuddy', 'qoder')
RETRY_STATUSES = (408, 429, 500, 502, 503, 504)
INFERENCE_PATHS = frozenset(('/v1/chat/completions', '/chat/completions',
                            '/v1/completions', '/completions',
                            '/v1/responses', '/responses', '/v1/messages',
                            '/messages', '/v1/messages/count_tokens',
                            '/messages/count_tokens'))
MANAGEMENT_GET = frozenset(('/health', '/realm', '/panel/status', '/accounts',
                            '/accounts/login/poll', '/accounts/credits',
                            '/accounts/credits/usage', '/accounts/credits/detail',
                            '/usage', '/v1/usage', '/usage/recent', '/usage/perf',
                            '/usage/timeseries', '/usage/analytics', '/usage/daily',
                            '/tasks', '/scheduler', '/logs', '/pricing',
                            '/v1/models', '/models/refresh', '/settings',
                            '/usage/by-account', '/usage/remaining', '/usage/stats',
                            '/accounts/credits/grants', '/accounts/activity',
                            '/accounts/export', '/diag/vm', '/update/check'))
MANAGEMENT_POST = frozenset(('/accounts/login/start', '/accounts/login/cancel',
                             '/accounts/import', '/accounts/import/pat',
                             '/accounts/refresh', '/accounts/credits',
                             '/accounts/credits/fetch', '/accounts/checkin',
                             '/accounts/set', '/accounts/set-all',
                             '/accounts/delete', '/accounts/note',
                             '/accounts/checkin-when-disabled', '/accounts/test',
                             '/tasks/run', '/tasks/travel', '/realm', '/scheduler/config',
                             '/scheduler/trigger', '/scheduler/toggle'))
_managers = {}
_managers_lock = threading.Lock()


def canonical_model(name):
    return re.sub(r'[^a-z0-9]', '', str(name or '').lower())


def model_match(requested, entry):
    names = [entry.get('id'), entry.get('name'), entry.get('upstream_key')]
    aliases = entry.get('aliases') or []
    if isinstance(aliases, list):
        names.extend(aliases)
    wanted = canonical_model(requested)
    return bool(wanted and any(wanted == canonical_model(n) for n in names if n))


def account_scope(entry, provider):
    values = (entry or {}).get('accounts') or []
    if not values:
        return None
    if provider == 'qoder':
        return [v[6:] for v in values if v.startswith('qoder/')]
    return [v[10:] if v.startswith('workbuddy/') else v for v in values
            if not v.startswith('qoder/')]


def session_identity(headers, payload, key_id, path):
    explicit = next((headers.get(n) for n in
                     ('X-Conversation-Id', 'Conversation-Id', 'X-Session-Id',
                      'Session-Id') if headers.get(n)), None)
    metadata = payload.get('metadata') or {}
    if not isinstance(metadata, dict):
        metadata = {}
    explicit = explicit or payload.get('conversation_id') or payload.get('session_id') \
        or metadata.get('conversation_id') or payload.get('prompt_cache_key')
    messages = payload.get('messages') or payload.get('input') or []
    if not isinstance(messages, list):
        messages = [messages]
    # First user turn is stable when later history is appended. No prompt persists.
    prefix = []
    for message in messages:
        prefix.append(message)
        if not isinstance(message, dict) or message.get('role') == 'user':
            break
    seed = explicit or prefix
    blob = json.dumps([key_id or '', path, canonical_model(payload.get('model')),
                       seed], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


class ProviderFailure(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__('platform request failed (%s)' % status)


class Manager:
    def __init__(self, hub):
        self.hub = hub
        self.process = None
        self.port = None
        self.secret = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.affinity = {}
        self.catalog = {}
        self.cooldowns = {}
        self.root = Path(hub.ACCOUNTS_DIR) / 'qoder-state'
        self.proxy_file = self.root / 'proxy-bindings.json'
        self.last_error = ''
        atexit.register(self.stop)

    def enabled(self):
        return wb_settings.load(self.hub.ACCOUNTS_DIR).get('qoder_enabled') is True

    def save_bindings(self):
        with self.lock:
            self._save_bindings_locked()

    def _save_bindings_locked(self):
        cfg = wb_settings.load(self.hub.ACCOUNTS_DIR)
        bound = cfg.get('qoder_proxy_slots') or {}
        slots = {s['id']: s for s in wb_settings.proxy_slots(self.hub.ACCOUNTS_DIR)
                 if s.get('enabled')}
        mapping = {}
        for uid, slot_id in bound.items():
            slot = slots.get(slot_id)
            mapping[uid] = slot.get('url') if slot else None
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.proxy_file.with_suffix('.tmp')
        temp.write_text(json.dumps(mapping), encoding='utf-8')
        os.chmod(str(temp), 0o600)
        os.replace(str(temp), str(self.proxy_file))

    def start(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                return
            if not self.enabled():
                raise RuntimeError('Qoder is disabled; enable it in the platform page')
            self.save_bindings()
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                self.port = sock.getsockname()[1]
            env = dict(os.environ)
            # Prevent the host WorkBuddy environment from selecting Qoder state/keys.
            for name in ('ACCOUNTS_DIR', 'API_KEY', 'PORT', 'HOST', 'QD_PROXY_KEY'):
                env.pop(name, None)
            env.update({'WB_QODER_BRIDGE_SECRET': self.secret,
                        'WB_QODER_PROXY_FILE': str(self.proxy_file.resolve()),
                        'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1'})
            worker = Path(__file__).resolve().with_name('wb_qoder_worker.py')
            args = [sys.executable, str(worker), '--host', '127.0.0.1',
                    '--port', str(self.port), '--accounts-dir',
                    str((self.root / 'accounts').resolve()), '--usage-dir',
                    str((self.root / 'usage').resolve())]
            log = (self.root / 'worker.log').open('ab')
            try:
                self.process = subprocess.Popen(
                    args, env=env, stdin=subprocess.PIPE, stdout=log,
                    stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            finally:
                log.close()
            for _ in range(80):
                if self.process.poll() is not None:
                    self.last_error = 'Qoder worker exited; inspect its private log'
                    raise RuntimeError(self.last_error)
                try:
                    self.json_request('/health', timeout=1)
                    self.last_error = ''
                    return
                except (OSError, ValueError, http.client.HTTPException):
                    time.sleep(0.1)
            self.stop()
            raise RuntimeError('Qoder worker startup timed out')

    def stop(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                if self.process.stdin:
                    self.process.stdin.close()
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None
            self.catalog.clear()

    def request(self, path, method='GET', body=None, headers=None, timeout=20):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=timeout)
        supplied = {'X-Hub-Bridge': self.secret, 'Content-Type': 'application/json'}
        supplied.update(headers or {})
        try:
            conn.request(method, path, body=body, headers=supplied)
            response = conn.getresponse()
            return conn, response
        except BaseException:
            conn.close()
            raise

    def json_request(self, path, method='GET', body=None, timeout=20):
        conn, response = self.request(path, method, body, timeout=timeout)
        try:
            data = response.read()
            if response.status >= 400:
                raise ProviderFailure(response.status, data)
            return json.loads(data or b'{}')
        finally:
            conn.close()

    def qoder_status(self, model=None):
        self.start()
        query = '?model=' + urllib.parse.quote(model, safe='') if model else ''
        return self.json_request('/hub/status' + query)['accounts']

    def qoder_models(self, realm):
        now = time.monotonic()
        cached = self.catalog.get(realm)
        if cached and now - cached[0] < 60:
            return cached[1]
        self.start()
        path = '/v1/models?realm=' + realm
        data = self.json_request(path, timeout=20).get('data') or []
        self.catalog[realm] = now, data
        return data

    def candidates(self, handler, path, payload):
        entry = handler.key_entry
        providers = wb_settings.key_platforms(entry)
        requested = str(payload.get('model') or '')
        forced = None
        if '/' in requested and requested.split('/', 1)[0] in PLATFORMS:
            forced, requested = requested.split('/', 1)
            if forced not in providers:
                raise ProviderFailure(403, b'{"error":{"message":"platform not allowed by this key"}}')
            providers = [forced]
        realm = handler._request_realm()
        if realm and realm not in ('cn', 'intl'):
            raise ValueError('realm must be cn or intl')
        realms = [realm] if realm in ('cn', 'intl') else ['cn', 'intl']
        result = []
        for provider in providers:
            allowed = account_scope(entry, provider)
            if provider == 'workbuddy':
                if path in ('/v1/messages', '/messages', '/v1/messages/count_tokens',
                            '/messages/count_tokens') and not self.hub._anthropic_messages_enabled():
                    continue
                accounts = list(self.hub.POOL.accounts) if self.hub.POOL else []
                if self.hub.pinned_account() is not None:
                    accounts = [self.hub.pinned_account()]
                for region in realms:
                    ready = [a for a in accounts if a.realm == region and a.ready(model=requested)
                             and (allowed is None or a.uid in allowed)]
                    if not ready or self.hub.restriction_hold_remaining(region) > 0:
                        continue
                    try:
                        models = [self.hub.model_entry(mid, meta)
                                  for mid, meta in self.hub.fetch_models(realm=region)]
                    except Exception:
                        continue
                    match = next((m for m in models if model_match(requested, m)), None)
                    if match:
                        result.append({'provider': provider, 'realm': region,
                                       'model': match['id'], 'weight': len(ready)})
            elif self.enabled() and self.hub.pinned_account() is None:
                try:
                    accounts = self.qoder_status()
                    for region in realms:
                        ready = [a for a in accounts if a['realm'] == region and a['ready']
                                 and (allowed is None or a['uid'] in allowed)]
                        if not ready:
                            continue
                        match = next((m for m in self.qoder_models(region)
                                      if m.get('enabled', True) is not False
                                      and model_match(requested, m)), None)
                        if match:
                            ready = [a for a in self.qoder_status(match['id'])
                                     if a['realm'] == region and a['ready']
                                     and (allowed is None or a['uid'] in allowed)]
                            if not ready:
                                continue
                            result.append({'provider': provider, 'realm': region,
                                           'model': match['id'], 'weight': len(ready)})
                except (OSError, ValueError, RuntimeError, ProviderFailure,
                        http.client.HTTPException):
                    continue
        return [c for c in result if self.cooldowns.get(
            (c['provider'], c['realm'], c['model']), 0) < time.monotonic()]

    def ordered(self, candidates, identity):
        with self.lock:
            now = time.monotonic()
            self.affinity = {k: v for k, v in self.affinity.items() if v[0] > now}
            pinned = self.affinity.get(identity)
        remaining = list(candidates)
        result = []
        if pinned:
            selected = next((c for c in remaining if
                             (c['provider'], c['realm']) == pinned[1:]), None)
            if selected:
                result.append(selected)
                remaining.remove(selected)
        while remaining:
            choice = random.choices(remaining, weights=[c['weight'] for c in remaining], k=1)[0]
            result.append(choice)
            remaining.remove(choice)
        if result:
            self.remember(identity, result[0])
        return result

    def remember(self, identity, candidate):
        with self.lock:
            if len(self.affinity) > 4096:
                self.affinity.pop(next(iter(self.affinity)))
            self.affinity[identity] = (time.monotonic() + 3600,
                                       candidate['provider'], candidate['realm'])

def manager(hub):
    key = os.path.abspath(hub.ACCOUNTS_DIR)
    with _managers_lock:
        if key not in _managers:
            _managers[key] = Manager(hub)
        return _managers[key]


def qoder_allowed(entry):
    return 'qoder' in wb_settings.key_platforms(entry)


def panel_catalog_enabled(hub, handler):
    panel_ok = getattr(handler, '_panel_ok', None)
    return bool(hasattr(handler, 'headers') and panel_ok and panel_ok()
                and manager(hub).enabled())


def _relay(handler, conn, response, provider=None):
    try:
        handler._platform_started = True
        handler.send_response(response.status)
        for name in ('Content-Type', 'Content-Disposition', 'Retry-After',
                     'X-Qoder-Realm', 'X-Qoder-Realm-Reason'):
            value = response.getheader(name)
            if value:
                handler.send_header(name, value)
        if provider:
            handler.send_header('X-Gateway-Platform', provider)
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Connection', 'close')
        handler.close_connection = True
        handler.end_headers()
        while True:
            chunk = response.read1(16384)
            if not chunk:
                break
            handler.wfile.write(chunk)
            handler.wfile.flush()
    finally:
        response.close()
        conn.close()


def dispatch(hub, handler, path, payload):
    if path not in INFERENCE_PATHS or wb_settings.key_platforms(handler.key_entry) == ['workbuddy']:
        return False
    mgr = manager(hub)
    if payload.get('previous_response_id'):
        handler._error(400, 'previous_response_id is not supported; resend full conversation history',
                       'invalid_request_error')
        return True
    requested = str(payload.get('model') or '')
    unprefixed = requested.split('/', 1)[1] if requested.split('/', 1)[0] in PLATFORMS and '/' in requested else requested
    for error in (handler._banned_model_error(unprefixed), handler._key_model_error(unprefixed)):
        if error:
            handler._error(400, error, 'invalid_request_error')
            return True
    if hub.BLOCK_BACKGROUND_REQUESTS:
        reason = hub.background_request_reason(payload)
        if reason:
            handler._error(400, hub.background_request_message(reason), 'invalid_request_error')
            return True
    ok, _, _ = hub._acquire_key_slot(handler.key_entry)
    if not ok:
        handler._error(429, 'API key concurrency limit reached', 'rate_limit_error')
        return True
    acquired = False
    try:
        acquired = hub._chat_slots.acquire(timeout=hub.CHAT_SLOT_WAIT_SECONDS)
        if not acquired:
            handler._error(503, 'gateway concurrency limit reached')
            return True
        try:
            choices = mgr.candidates(handler, path, payload)
        except ProviderFailure as exc:
            handler._error(exc.status, 'platform not allowed by this key', 'invalid_request_error')
            return True
        except ValueError as exc:
            handler._error(400, str(exc), 'invalid_request_error')
            return True
        if not choices:
            handler._error(503, 'No allowed platform has a ready account for this model')
            return True
        identity = session_identity(handler.headers, payload, handler._key_id(), path)
        failure = None
        for choice in mgr.ordered(choices, identity):
            forwarded = dict(payload, model=choice['model'])
            try:
                if choice['provider'] == 'workbuddy':
                    _dispatch_workbuddy(hub, handler, path, forwarded, choice)
                else:
                    mgr.save_bindings()
                    headers = {'X-Realm': choice['realm'],
                               'X-Hub-Key-Id': handler._key_id() or '',
                               'X-Session-Id': identity}
                    allowed = account_scope(handler.key_entry, 'qoder')
                    if allowed is not None:
                        headers['X-Hub-Allowed-Accounts'] = json.dumps(allowed)
                    target = '/v1/' + path.split('/')[-1]
                    if path.endswith('/count_tokens'):
                        target = '/v1/messages/count_tokens'
                    conn, response = mgr.request(target, 'POST',
                        json.dumps(forwarded).encode('utf-8'), headers, timeout=620)
                    if response.status in RETRY_STATUSES:
                        try:
                            body = response.read(65536)
                        finally:
                            conn.close()
                        raise ProviderFailure(response.status, body)
                    if response.status >= 400:
                        _relay(handler, conn, response, 'qoder')
                        return True
                    _relay(handler, conn, response, 'qoder')
                mgr.remember(identity, choice)
                return True
            except ProviderFailure as exc:
                failure = exc
                mgr.cooldowns[(choice['provider'], choice['realm'], choice['model'])] = time.monotonic() + 10
            except (OSError, http.client.HTTPException):
                # If headers were sent, replaying would duplicate a partial answer.
                if getattr(handler, '_platform_started', False):
                    handler.close_connection = True
                    return True
                failure = ProviderFailure(502, b'')
        handler._error(failure.status if failure else 503,
                       'All allowed platform candidates are temporarily unavailable')
        return True
    finally:
        if acquired:
            hub._chat_slots.release()
        hub._release_key_slot(handler.key_entry)


def _dispatch_workbuddy(hub, handler, path, payload, candidate):
    original_error = handler._error
    original_send = handler.send_response
    original_realm = handler._request_realm
    original_entry = handler.key_entry
    handler._platform_started = False

    def send(code, *args, **kwargs):
        handler._platform_started = True
        original_send(code, *args, **kwargs)
        handler.send_header('X-Gateway-Platform', 'workbuddy')

    def error(status, message, *args, **kwargs):
        if status in RETRY_STATUSES and not handler._platform_started:
            raise ProviderFailure(status, b'')
        return original_error(status, message, *args, **kwargs)

    scoped = dict(original_entry or {})
    allowed = account_scope(original_entry, 'workbuddy')
    if allowed is not None:
        scoped['accounts'] = allowed
    handler.key_entry = scoped
    handler._error = error
    handler.send_response = send
    handler._request_realm = lambda explicit=None: candidate['realm']
    try:
        if path in ('/v1/messages', '/messages'):
            return handler._handle_messages(payload)
        if path.endswith('/count_tokens'):
            return handler._handle_messages_count_tokens(payload)
        return handler._dispatch_chat_post(path, payload)
    finally:
        handler._error = original_error
        handler.send_response = original_send
        handler._request_realm = original_realm
        handler.key_entry = original_entry


def models(hub, handler, data):
    providers = list(PLATFORMS) if panel_catalog_enabled(hub, handler) else wb_settings.key_platforms(handler.key_entry)
    merged = [dict(m, platforms=['workbuddy']) for m in data] if 'workbuddy' in providers else []
    if 'qoder' not in providers or not manager(hub).enabled():
        return merged
    mgr = manager(hub)
    realm = handler._request_realm()
    for region in ([realm] if realm in ('cn', 'intl') else ['cn', 'intl']):
        try:
            extra = mgr.qoder_models(region)
        except (OSError, RuntimeError, ProviderFailure, ValueError, http.client.HTTPException):
            continue
        for model in extra:
            if model.get('enabled', True) is False:
                continue
            if not wb_settings.key_allows_model(handler.key_entry, model.get('id')):
                continue
            found = next((m for m in merged if model_match(model['id'], m)), None)
            if found:
                if 'qoder' not in found['platforms']:
                    found['platforms'].append('qoder')
            else:
                merged.append(dict(model, platforms=['qoder']))
    return merged


def panel_route(hub, handler, method):
    parsed = urllib.parse.urlsplit(handler.path)
    if not parsed.path.startswith('/platforms'):
        return False
    if not handler._panel_ok():
        handler._error(401, 'panel password required', 'invalid_request_error')
        return True
    mgr = manager(hub)
    try:
        if parsed.path == '/platforms/status' and method == 'GET':
            if mgr.enabled():
                mgr.save_bindings()
            rows = mgr.qoder_status() if mgr.enabled() else []
            bound = wb_settings.load(hub.ACCOUNTS_DIR).get('qoder_proxy_slots') or {}
            for row in rows:
                row['proxy_slot'] = bound.get(row['uid'], '')
            handler._json(200, {'qoder_enabled': mgr.enabled(), 'accounts': rows,
                                'slots': hub.proxy_slots_view()})
        elif parsed.path == '/platforms/config' and method == 'POST':
            payload = handler._payload_or_error()
            if payload is None:
                return True
            if not isinstance(payload.get('qoder_enabled'), bool):
                raise ValueError('qoder_enabled must be a boolean')
            cfg = wb_settings.load(hub.ACCOUNTS_DIR)
            cfg['qoder_enabled'] = payload['qoder_enabled']
            wb_settings.save(hub.ACCOUNTS_DIR, cfg)
            if mgr.enabled():
                mgr.start()
            else:
                mgr.stop()
            handler._json(200, {'ok': True, 'qoder_enabled': mgr.enabled()})
        elif parsed.path == '/platforms/qoder/bind' and method == 'POST':
            payload = handler._payload_or_error()
            if payload is None:
                return True
            uid, slot = str(payload.get('uid') or ''), str(payload.get('proxy_slot') or '')
            if uid not in [a['uid'] for a in mgr.qoder_status()]:
                raise ValueError('unknown Qoder account')
            slots = wb_settings.proxy_slots(hub.ACCOUNTS_DIR)
            if slot and not any(s['id'] == slot and s.get('enabled') for s in slots):
                raise ValueError('unknown or disabled proxy slot')
            cfg = wb_settings.load(hub.ACCOUNTS_DIR)
            bound = dict(cfg.get('qoder_proxy_slots') or {})
            if slot:
                bound[uid] = slot
            else:
                bound.pop(uid, None)
            cfg['qoder_proxy_slots'] = bound
            wb_settings.save(hub.ACCOUNTS_DIR, cfg)
            mgr.save_bindings()
            handler._json(200, {'ok': True})
        elif parsed.path == '/platforms/qoder/ui' and method == 'GET':
            mgr.start()
            page = Path(__file__).parent / 'third_party/qoder2api/dashboard.html'
            script = """<script>
const hubFetch = window.fetch.bind(window);
window.fetch = function(input, options) {
  if(typeof input !== 'string' || !input.startsWith('/')) return hubFetch(input, options);
  options = Object.assign({}, options || {});
  options.headers = new Headers(options.headers || {});
  options.headers.delete('Authorization');
  options.headers.set('X-Panel-Token', sessionStorage.getItem('wb-proxy-panel-token') || '');
  return hubFetch('/platforms/qoder/api' + input, options);
};
</script>"""
            html = page.read_text(encoding='utf-8').replace(
                "const PANEL_STORE = 'wb-proxy-panel-token';",
                "const PANEL_STORE = 'qoder-embedded-panel-token';")
            # Key management and client configuration belong to the outer hub.
            style = '<style>#btnNavSettings,#btnNavAgents{display:none!important}</style>'
            body = html.replace('<head>', '<head>' + script + style, 1).encode('utf-8')
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/html; charset=utf-8')
            handler.send_header('Content-Length', str(len(body)))
            handler.send_header('Cache-Control', 'no-store')
            handler.end_headers()
            handler.wfile.write(body)
        elif parsed.path.startswith('/platforms/qoder/api/'):
            target = parsed.path[len('/platforms/qoder/api'):]
            allowed = MANAGEMENT_GET if method == 'GET' else MANAGEMENT_POST
            if target not in allowed:
                handler._error(404, 'Qoder management route not exposed')
                return True
            mgr.start()
            mgr.save_bindings()
            body = None
            if method == 'POST':
                payload = handler._payload_or_error(allow_list=target == '/accounts/import')
                if payload is None:
                    return True
                body = json.dumps(payload).encode('utf-8')
            conn, response = mgr.request(target + ('?' + parsed.query if parsed.query else ''),
                                         method, body, timeout=120)
            _relay(handler, conn, response)
        else:
            handler._error(404, 'platform route not found')
    except ValueError as exc:
        handler._error(400, str(exc), 'invalid_request_error')
    except (OSError, RuntimeError, ProviderFailure, http.client.HTTPException):
        handler._error(503, 'Qoder worker unavailable; inspect platform status and private worker log')
    return True


def refresh_bindings(hub):
    """Push a saved proxy-slot change without starting a disabled backend."""
    key = os.path.abspath(hub.ACCOUNTS_DIR)
    with _managers_lock:
        mgr = _managers.get(key)
    if mgr is not None:
        mgr.save_bindings()
