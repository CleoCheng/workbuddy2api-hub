"""Private Qoder worker. Only the hub may authenticate to this listener.

The upstream runtime stays unmodified under third_party; integration lives here.
"""
import functools
import hmac
import json
import os
import sys
import threading
import urllib.request
from pathlib import Path


def _wrap_account_methods(account_class, context):
    def with_account(method):
        @functools.wraps(method)
        def wrapped(self, *args, **kwargs):
            previous = getattr(context, 'uid', '')
            context.uid = self.uid
            try:
                return method(self, *args, **kwargs)
            finally:
                context.uid = previous
        return wrapped

    for name, method in list(vars(account_class).items()):
        # Static helpers have no account instance; preserve their descriptors.
        if isinstance(method, (staticmethod, classmethod)):
            continue
        if callable(method) and not name.startswith('__'):
            setattr(account_class, name, with_account(method))


def main():
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root / 'third_party' / 'qoder2api'))
    import qoder_accounts as accounts
    import qoder_proxy as proxy

    secret = os.environ.pop('WB_QODER_BRIDGE_SECRET')
    # The pipe closes even when the hub is killed without running atexit.
    def parent_watchdog():
        sys.stdin.buffer.read()
        os._exit(0)
    threading.Thread(target=parent_watchdog, daemon=True).start()
    proxy_path = Path(os.environ.pop('WB_QODER_PROXY_FILE'))
    context = threading.local()
    original_open = urllib.request.urlopen
    original_opener = accounts._opener_for_request
    original_ready = accounts.Account.ready

    def bindings():
        try:
            return json.loads(proxy_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            # Missing metadata must not silently change a bound account's exit.
            raise RuntimeError('Qoder proxy bindings unavailable') from None

    def account_proxy(uid):
        mapping = bindings()
        value = mapping.get(uid, '')
        if value is None:
            raise RuntimeError('The assigned Qoder proxy slot is disabled or missing')
        return value

    def opener():
        uid = getattr(context, 'uid', '')
        if not uid:
            return original_opener()
        value = account_proxy(uid)
        if not value:
            return original_opener()
        return urllib.request.build_opener(urllib.request.ProxyHandler(
            {'http': value, 'https': value}))

    def urlopen(request, *args, **kwargs):
        # COSY signing names the actual account, including after a retry switches it.
        uid = request.get_header('Cosy-user') if hasattr(request, 'get_header') else None
        if uid:
            context.uid = uid
        if getattr(context, 'uid', ''):
            return opener().open(request, *args, **kwargs)
        return original_open(request, *args, **kwargs)

    # Refresh, quota, check-in and other background work all inherit the binding.
    _wrap_account_methods(accounts.Account, context)

    def ready(self, *args, **kwargs):
        allowed = getattr(context, 'allowed', None)
        if allowed is not None and self.uid not in allowed:
            return False
        try:
            account_proxy(self.uid)
        except RuntimeError:
            return False
        return original_ready(self, *args, **kwargs)

    accounts.Account.ready = ready
    accounts._opener_for_request = opener
    urllib.request.urlopen = urlopen
    parent = proxy.Handler
    original_get = parent.do_GET
    original_post = parent.do_POST

    class HubHandler(parent):
        def _panel_ok(self):
            return hmac.compare_digest(self.headers.get('X-Hub-Bridge', ''), secret)

        def _key_ok(self):
            key_id = self.headers.get('X-Hub-Key-Id', '')
            self.key_entry = {'id': key_id} if key_id else None
            return self._panel_ok()

        def _run(self, method):
            context.uid = ''
            context.allowed = None
            if not self._panel_ok():
                self.close_connection = True
                return self._error(401, 'private hub worker')
            raw = self.headers.get('X-Hub-Allowed-Accounts')
            if raw is not None:
                try:
                    context.allowed = set(json.loads(raw))
                except (TypeError, ValueError):
                    return self._error(400, 'invalid account scope')
            try:
                return method(self)
            finally:
                context.uid = ''
                context.allowed = None

        def do_GET(self):
            if self.path.split('?')[0] == '/hub/status' and self._panel_ok():
                from urllib.parse import urlsplit, parse_qs
                model = parse_qs(urlsplit(self.path).query).get('model', [None])[0]
                rows = []
                for account in proxy.POOL.accounts if proxy.POOL else []:
                    rows.append({'uid': account.uid, 'realm': account.realm,
                                 'enabled': account.enabled,
                                 'nickname': account.nickname,
                                 'ready': account.ready(model=model),
                                 'cooldown_until': account.cooldown_until})
                return self._json(200, {'accounts': rows})
            return self._run(original_get)

        def do_POST(self):
            return self._run(original_post)

    # Upstream uses super(Handler, self), so preserve its global class identity.
    for name in ('_panel_ok', '_key_ok', '_run', 'do_GET', 'do_POST'):
        setattr(parent, name, vars(HubHandler)[name])
    proxy.main()


if __name__ == '__main__':
    main()
