"""Opt-in proxy health and serialized Mihomo failover; standard library only.

The spare listener must be dedicated to probes. Production selectors are only
changed after the spare exit has been checked and reserved across all slots.
No inference is sent or replayed by this module.
"""
import atexit
import copy
import hashlib
import http.client
import ipaddress
import json
import os
import random
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import wb_ipintel
import wb_settings

DEFAULTS = dict(enabled=False, interval=90, failures=3, cooldown=600,
                timeout=5, candidate_limit=12, allow_cross_country=False,
                ranked=False, rank_interval=1800, slow_ms=1500,
                slow_failures=3, improvement_pct=20, residential_tie_pct=10,
                controller='http://127.0.0.1:9090',
                probe_proxy='http://127.0.0.1:17990',
                probe_group='Gateway spare probe', groups={})
CHECK_URLS = ('https://api.ipify.org', 'https://checkip.amazonaws.com')
_managers = {}
_lock = threading.Lock()


def config(accounts_dir):
    out = dict(DEFAULTS, groups={})
    raw = wb_settings.load(accounts_dir).get('proxy_health')
    if isinstance(raw, dict):
        out.update({k: v for k, v in raw.items() if k in DEFAULTS})
    return validate(out)


def _loopback_url(value):
    u = urllib.parse.urlsplit(str(value))
    if (u.scheme != 'http' or u.username or u.password or u.query or u.fragment
            or u.path not in ('', '/') or not u.port
            or not ipaddress.ip_address(u.hostname).is_loopback):
        raise ValueError('controller and probe proxy must be loopback HTTP URLs')
    return str(value).rstrip('/')


def validate(raw):
    out = dict(DEFAULTS, groups={})
    for name in ('enabled', 'allow_cross_country', 'ranked'):
        value = raw.get(name, out[name])
        if not isinstance(value, bool):
            raise ValueError(name + ' must be boolean')
        out[name] = value
    for name, bounds in dict(interval=(30, 3600), failures=(2, 10),
                             cooldown=(60, 3600), timeout=(2, 15),
                             candidate_limit=(1, 50), rank_interval=(300, 86400),
                             slow_ms=(100, 30000), slow_failures=(2, 10),
                             improvement_pct=(0, 90), residential_tie_pct=(0, 20)).items():
        value = raw.get(name, out[name])
        if isinstance(value, bool) or not isinstance(value, int) or not bounds[0] <= value <= bounds[1]:
            raise ValueError('%s must be an integer in %s..%s' % (name, *bounds))
        out[name] = value
    for name in ('controller', 'probe_proxy'):
        out[name] = _loopback_url(raw.get(name, out[name]))
    out['probe_group'] = str(raw.get('probe_group', out['probe_group'])).strip()
    groups = raw.get('groups', {})
    if not isinstance(groups, dict) or len(groups) > 100:
        raise ValueError('groups must map slot IDs to Mihomo selector names')
    out['groups'] = {str(k): str(v).strip() for k, v in groups.items() if str(v).strip()}
    if (not out['probe_group'] or out['probe_group'] in out['groups'].values()
            or len(set(out['groups'].values())) != len(out['groups'])):
        raise ValueError('each slot and the spare probe require distinct selectors')
    return out


def rank_exits(results, tie_pct):
    """Latency first; residential preference only within anchored, bounded bands.

    Anchor each band to its fastest member, avoiding non-transitive comparisons
    that could let a chain of similar delays promote a very slow residential IP.
    Keep only one node per observed exit IP.
    """
    remaining = sorted((r for r in results if r.get('ok') and r.get('ip')),
                       key=lambda r: (r['latency_ms'], r['node']))
    ranked, seen = [], set()
    while remaining:
        ceiling = remaining[0]['latency_ms'] * (1 + tie_pct / 100)
        at = next((i for i, r in enumerate(remaining) if r['latency_ms'] > ceiling), len(remaining))
        band, remaining = remaining[:at], remaining[at:]
        band.sort(key=lambda r: (r.get('ip_type') != 'residential', r['latency_ms'], r['node']))
        for row in band:
            if row['ip'] not in seen:
                ranked.append(dict(row, rank=len(ranked) + 1))
                seen.add(row['ip'])
    return ranked


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.%s.%s.tmp' % (os.getpid(), threading.get_ident()))
    try:
        with temp.open('w', encoding='utf-8', newline='\n') as f:
            json.dump(value, f, ensure_ascii=False)
        os.chmod(str(temp), 0o600)
        os.replace(str(temp), str(path))
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def transport_failure(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (502, 503, 504)
    if isinstance(error, ssl.SSLCertVerificationError):
        return False
    if isinstance(error, urllib.error.URLError):
        return transport_failure(error.reason)
    return isinstance(error, (TimeoutError, socket.timeout, ConnectionError,
                              socket.gaierror, ssl.SSLEOFError,
                              http.client.IncompleteRead, http.client.RemoteDisconnected))


class ProxyUnavailable(urllib.error.URLError):
    pass


class WorkerSignals:
    """Qoder's private worker uses bounded per-exit event files, never a key."""
    def __init__(self, root):
        self.root = Path(root)
        self.state = {}
        self.at = 0
        self.sent = {}
        self.lock = threading.RLock()

    def _load(self):
        if time.monotonic() - self.at < 1:
            return self.state
        try:
            self.state = json.loads((self.root / 'state.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            # Preserve the previous fail-closed decision across an atomic write.
            pass
        self.at = time.monotonic()
        return self.state

    def allow(self, proxy):
        with self.lock:
            d = self._load()
            return not d.get('enabled') or proxy not in d.get('blocked', [])

    def notify(self, proxy, error):
        if not proxy or not transport_failure(error):
            return
        if not self._load().get('enabled'):
            return
        now = time.monotonic()
        with self.lock:
            if now - self.sent.get(proxy, -60) < 20:
                return
            self.sent[proxy] = now
        key = hashlib.sha256(proxy.encode()).hexdigest()
        try:
            atomic_json(self.root / 'events' / (key + '.json'),
                        dict(proxy=proxy, at=time.time()))
        except OSError:
            pass


class ObservedResponse:
    """Preserve urllib response semantics while reporting read/stream failures."""
    def __init__(self, response, notify):
        self.response = response
        self.notify = notify

    def __getattr__(self, name):
        return getattr(self.response, name)

    def read(self, *args):
        try:
            return self.response.read(*args)
        except Exception as exc:
            self.notify(exc)
            raise

    def readline(self, *args):
        try:
            return self.response.readline(*args)
        except Exception as exc:
            self.notify(exc)
            raise

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def __enter__(self):
        self.response.__enter__()
        return self

    def __exit__(self, *args):
        return self.response.__exit__(*args)


class Monitor:
    def __init__(self, hub):
        self.hub = hub
        self.root = Path(hub.ACCOUNTS_DIR) / 'proxy-health'
        self.lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.thread = None
        self.rows = {}
        self.history = []
        self.pending = {}
        self.last_signal = {}
        self.last_round = 0
        self.error = ''
        self.ranking = {}
        self.cfg = config(hub.ACCOUNTS_DIR)
        try:
            old = json.loads((self.root / 'state.json').read_text(encoding='utf-8'))
            self.rows = old.get('slots', {})
            self.history = old.get('history', [])[-100:]
            # Scan again after restart; persisted results remain visible meanwhile.
            self.ranking = dict(old.get('ranking', {}), completed_at=0, scanning=False)
        except (OSError, ValueError):
            pass
        atexit.register(self.stop)

    def start(self):
        if not self.cfg['enabled']:
            return
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._loop, name='proxy-health', daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()

    def configure(self, raw):
        if not self.run_lock.acquire(blocking=False):
            raise ValueError('health check is running; retry configuration after it finishes')
        try:
            self._configure(raw)
        finally:
            self.run_lock.release()

    def _configure(self, raw):
        new = validate(raw)
        slots = wb_settings.proxy_slots(self.hub.ACCOUNTS_DIR)
        if any(urllib.parse.urlsplit(s['url']).port == urllib.parse.urlsplit(new['probe_proxy']).port
               for s in slots):
            raise ValueError('spare probe listener cannot be an account proxy slot')
        if new['enabled']:
            if not new['groups']:
                raise ValueError('configure the slot-to-selector mapping first')
            if not (self.root / 'secret').is_file():
                raise ValueError('install the Mihomo secret in accounts/proxy-health/secret first')
        with wb_settings._lock:
            d = wb_settings.load(self.hub.ACCOUNTS_DIR)
            d['proxy_health'] = new
            wb_settings.save(self.hub.ACCOUNTS_DIR, d)
        with self.lock:
            self.cfg = new
            self.pending.clear()
            self.last_round = 0
            self.ranking['completed_at'] = 0
            self._persist()
        self.start()
        self.wake.set()

    def allow(self, proxy):
        with self.lock:
            if not self.cfg['enabled']:
                return True
            return not any(r.get('url') == proxy and r.get('status') in
                           ('unavailable', 'switching') for r in self.rows.values())

    def generation(self, proxy):
        with self.lock:
            return max((r.get('generation', 0) for r in self.rows.values()
                        if r.get('url') == proxy), default=0)

    def notify(self, proxy, error):
        if not transport_failure(error):
            return
        with self.lock:
            if not self.cfg['enabled']:
                return
            slots = wb_settings.proxy_slots(self.hub.ACCOUNTS_DIR)
            if not any(s['url'] == proxy for s in slots):
                return
            now = time.monotonic()
            if now - self.last_signal.get(proxy, -60) < 20:
                return
            self.last_signal[proxy] = now
            self.pending[proxy] = 'request_failure'
        self.wake.set()

    def trigger(self, rescan=False):
        with self.lock:
            self.last_round = 0
            if rescan:
                self.ranking['completed_at'] = 0
        self.start()
        self.wake.set()

    def view(self):
        with self.lock:
            return dict(config=copy.deepcopy(self.cfg), running=bool(self.thread and self.thread.is_alive()),
                        secret_configured=(self.root / 'secret').is_file(),
                        last_round=self.last_round, error=self.error,
                        slots=json.loads(json.dumps(self.rows)), history=self.history[-30:],
                        ranking=copy.deepcopy(self.ranking))

    def node_choices(self, sid):
        slots = wb_settings.proxy_slots(self.hub.ACCOUNTS_DIR)
        slot = next((s for s in slots if s['id'] == sid), None)
        if not slot or sid not in self.cfg['groups']:
            raise ValueError('slot_selector_not_configured')
        with self.lock:
            cfg, rows = copy.deepcopy(self.cfg), copy.deepcopy(self.rows)
            ranking = copy.deepcopy(self.ranking)
        live = self._api(cfg, '/proxies')['proxies']
        group = live.get(cfg['groups'][sid], {})
        current = group.get('now', '')
        occupied = {r.get('ip') for k, r in rows.items() if k != sid}
        used = {live.get(g, {}).get('now') for k, g in cfg['groups'].items() if k != sid}
        nodes = []
        for result in ranking.get('ranked', []):
            if result['node'] not in group.get('all', []) or result['node'] == current:
                continue
            reason = ('exit_occupied' if result['ip'] in occupied or result['node'] in used else
                      'country_restricted' if not cfg['allow_cross_country'] and
                      (not slot.get('country_code') or result.get('country_code') != slot['country_code']) else '')
            nodes.append(dict(result, selectable=not reason, unavailable_reason=reason))
        return dict(id=sid, current=current, manual=bool(rows.get(sid, {}).get('manual')),
                    nodes=nodes, scanning=ranking.get('scanning', False),
                    tested_at=ranking.get('completed_at', 0), enabled=cfg['enabled'])

    def manual_switch(self, sid, node=None, automatic=False):
        if not self.run_lock.acquire(blocking=False):
            raise ValueError('health_check_busy')
        try:
            cfg = copy.deepcopy(self.cfg)
            slots = wb_settings.proxy_slots(self.hub.ACCOUNTS_DIR)
            slot = next((s for s in slots if s['id'] == sid), None)
            if not slot or not slot.get('enabled') or sid not in cfg['groups'] or not cfg['enabled']:
                raise ValueError('enabled_managed_slot_required')
            with self.lock:
                rows = copy.deepcopy(self.rows)
            if automatic:
                if sid in rows:
                    rows[sid]['manual'] = False
                    self._publish(rows)
                self._record(sid, 'resume_auto')
                self.trigger()
                return dict(ok=True, automatic=True)
            candidate = next((r for r in self.ranking.get('ranked', []) if r['node'] == node), None)
            if not candidate:
                raise ValueError('measured_candidate_required')
            live = self._api(cfg, '/proxies')['proxies']
            with ThreadPoolExecutor(max_workers=4) as ex:
                probes = list(ex.map(lambda s: self._probe(s['url'], cfg['timeout']), slots))
            for s, probe in zip(slots, probes):
                if not probe['ok']:
                    probe = self._probe(s['url'], cfg['timeout'], 1)
                row = rows.setdefault(s['id'], dict(id=s['id'], url=s['url'], failures=0))
                row.update(checked_at=time.time())
                if probe['ok']:
                    if row.get('ip') and row['ip'] != probe['ip']:
                        row['generation'] = time.time_ns()
                    row.update(probe, status='healthy', failures=0, error='', last_success=time.time(),
                               ip_node=live.get(cfg['groups'].get(s['id']), {}).get('now'))
                else:
                    # A failed peer probe cannot prove that its cached IP is still unique.
                    # Manual selection must not bypass the normal failure threshold
                    # by blocking a previously reachable peer after a single check.
                    status = 'unavailable' if row.get('status') in ('unavailable', 'switching') else 'suspect'
                    row.update(status=status, last_success=0, error='exit_probe_failed')
                    with self.lock:
                        self.pending[s['url']] = 'manual_probe_failure'
                    self.wake.set()
            self._publish(rows)
            if not self._ranked_switch(cfg, slot, slots, live, rows, [candidate], 'manual'):
                self._publish(rows)
                raise ValueError(rows[sid].get('error') or 'candidate_revalidation_failed')
            rows[sid]['manual'] = True
            self._publish(rows)
            return dict(ok=True, slot=sid, node=rows[sid]['node'], ip=rows[sid]['ip'])
        finally:
            self.run_lock.release()

    def _publish(self, rows):
        with self.lock:
            self.rows = copy.deepcopy(rows)
            self._persist()

    def _persist(self):
        blocked = [r['url'] for r in self.rows.values() if r.get('status') in
                   ('unavailable', 'switching') and r.get('url')]
        atomic_json(self.root / 'state.json', dict(enabled=self.cfg['enabled'],
                    blocked=blocked, slots=self.rows, history=self.history[-100:], ranking=self.ranking))

    def _record(self, slot, reason, **fields):
        item = dict(at=time.time(), slot=slot, reason=reason, **fields)
        with self.lock:
            self.history.append(item)
            self.history = self.history[-100:]
        log = getattr(self.hub, 'log', None)
        if log:
            log('proxy health: %s %s' % (slot, reason))

    def _api(self, cfg, path, body=None):
        secret = (self.root / 'secret').read_text(encoding='utf-8').strip()
        req = urllib.request.Request(cfg['controller'] + path,
              data=json.dumps(body).encode() if body is not None else None,
              headers={'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json'},
              method='PUT' if body is not None else 'GET')
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(req, timeout=cfg['timeout']) as r:
            return json.loads(r.read(4 * 1024 * 1024) or b'{}')

    def _select(self, cfg, group, node):
        self._api(cfg, '/proxies/' + urllib.parse.quote(group, safe=''), {'name': node})

    def _probe(self, url, timeout, endpoint=0):
        started = time.monotonic()
        op = urllib.request.build_opener(urllib.request.ProxyHandler({'http': url, 'https': url}))
        try:
            with op.open(CHECK_URLS[endpoint], timeout=timeout) as r:
                ip = str(ipaddress.ip_address(r.read(128).decode().strip()))
                if not ipaddress.ip_address(ip).is_global:
                    raise ValueError('probe returned a non-public exit')
            return dict(ok=True, ip=ip, latency_ms=int((time.monotonic() - started) * 1000))
        except Exception:
            return dict(ok=False, error='exit_probe_failed')

    def _candidate_probe(self, cfg, node):
        self._select(cfg, cfg['probe_group'], node)
        out = self._probe(cfg['probe_proxy'], cfg['timeout'])
        if not out['ok']:
            out = self._probe(cfg['probe_proxy'], cfg['timeout'], 1)
        return out

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                with self.lock:
                    cfg = dict(self.cfg)
                if cfg['enabled']:
                    for p in (self.root / 'events').glob('*.json'):
                        try:
                            data = json.loads(p.read_text(encoding='utf-8'))
                            p.unlink()
                            if time.time() - data.get('at', 0) < 120:
                                self.notify(data.get('proxy'), TimeoutError())
                        except (OSError, ValueError):
                            pass
                    with self.lock:
                        pending = bool(self.pending)
                    if pending or time.time() - self.last_round >= cfg['interval']:
                        self.check()
            except Exception:
                with self.lock:
                    self.error = 'monitor_error; inspect controller, secret and spare listener'
            self.wake.wait(1)
            self.wake.clear()

    def check(self):
        if not self.run_lock.acquire(blocking=False):
            return
        try:
            cfg = dict(self.cfg)
            if not cfg['enabled']:
                return
            slots = wb_settings.proxy_slots(self.hub.ACCOUNTS_DIR)
            with self.lock:
                reasons = dict(self.pending)
                self.pending.clear()
                previous_rows = copy.deepcopy(self.rows)
            # A full snapshot protects unmonitored and disabled slots' IPs too.
            with ThreadPoolExecutor(max_workers=4) as ex:
                probes = list(ex.map(lambda s: self._probe(s['url'], cfg['timeout']), slots))
            now = time.time()
            current = {s['id']: s for s in slots}
            rows = {k: v for k, v in previous_rows.items() if k in current}
            for slot, probe in zip(slots, probes):
                old = rows.get(slot['id'], {})
                # Disabled/unmanaged slots are observed for reservations, never rotated.
                row = dict(old, id=slot['id'], url=slot['url'], checked_at=now,
                           managed=slot['id'] in cfg['groups'], reason=reasons.get(slot['url'], 'scheduled'))
                if probe['ok']:
                    row.update(probe, status='healthy', failures=0, error='', last_success=now)
                    row['slow_count'] = old.get('slow_count', 0) + 1 if probe['latency_ms'] > cfg['slow_ms'] else 0
                    if old.get('ip') and old['ip'] != probe['ip']:
                        row['generation'] = time.time_ns()
                else:
                    count = old.get('failures', 0) + 1
                    status = 'unavailable' if old.get('status') in ('unavailable', 'switching') else 'suspect'
                    row.update(status=status, failures=count, error=probe['error'])
                    row['slow_count'] = 0
                    row.setdefault('ip', slot.get('ip', ''))
                rows[slot['id']] = row
            self._publish(rows)
            # Read the controller once; failure does not erase health decisions.
            try:
                proxy_map = self._api(cfg, '/proxies')['proxies']
                providers = self._api(cfg, '/providers/proxies')['providers']
                self.error = ''
            except Exception:
                proxy_map, providers = {}, {}
                self.error = 'controller_unavailable; no selector changes performed'
            for slot, probe in zip(slots, probes):
                group = cfg['groups'].get(slot['id'])
                if probe['ok'] and group in proxy_map:
                    rows[slot['id']]['ip_node'] = proxy_map[group].get('now')
            if cfg['ranked']:
                self._ranked_check(cfg, slots, proxy_map, providers, rows)
                with self.lock:
                    self.last_round = time.time()
                self._publish(rows)
                return
            for slot in slots:
                if self.stop_event.is_set():
                    break
                row = rows[slot['id']]
                group = cfg['groups'].get(slot['id'])
                if group and group in proxy_map:
                    row['node'] = proxy_map[group].get('now', '')
                if (not slot.get('enabled') or not group or row['failures'] < cfg['failures']):
                    continue
                # Independent endpoint confirmation is mandatory before blocking/switching.
                confirm = self._probe(slot['url'], cfg['timeout'], 1)
                if confirm['ok']:
                    row.update(confirm, status='healthy', failures=0, error='', last_success=now)
                    if group in proxy_map:
                        row['ip_node'] = proxy_map[group].get('now')
                    continue
                row['status'] = 'unavailable'
                self._publish(rows)
                if not proxy_map:
                    continue
                if now - row.get('last_switch', 0) < cfg['cooldown']:
                    row['error'] = 'switch_cooldown'
                    continue
                self._replace(cfg, slot, slots, proxy_map, providers, rows)
            with self.lock:
                self.last_round = time.time()
            self._publish(rows)
        finally:
            self.run_lock.release()

    def _scan_candidates(self, cfg, proxy_map, providers):
        spare = proxy_map.get(cfg['probe_group'], {})
        if spare.get('type') != 'Selector':
            raise ValueError('dedicated_probe_selector_required')
        allowed = set(spare.get('all', []))
        production = set()
        for group in cfg['groups'].values():
            production.update(proxy_map.get(group, {}).get('all', []))
        nodes = sorted({p['name'] for provider in providers.values()
                        if provider.get('vehicleType') in ('HTTP', 'File')
                        for p in provider.get('proxies', []) if p.get('name') in allowed & production})
        original = spare.get('now')
        results, intel_cache = [], {}
        with self.lock:
            self.ranking = dict(scanning=True, started_at=time.time(), completed_at=0,
                                done=0, total=len(nodes), results=[], ranked=[])
            self._persist()
        try:
            for node in nodes:
                if self.stop_event.is_set() or self.cfg != cfg:
                    raise ValueError('scan_interrupted')
                try:
                    probe = self._candidate_probe(cfg, node)
                except Exception:
                    probe = dict(ok=False, error='candidate_probe_failed')
                result = dict(probe, node=node, tested_at=time.time())
                if probe['ok']:
                    ip = probe['ip']
                    if ip not in intel_cache:
                        intel_cache[ip] = wb_ipintel.lookup(ip)
                    result.update(intel_cache[ip])
                results.append(result)
                with self.lock:
                    self.ranking.update(done=len(results), results=copy.deepcopy(results))
                    self._persist()
            ranked = rank_exits(results, cfg['residential_tie_pct'])
            with self.lock:
                self.ranking.update(ranked=ranked, completed_at=time.time())
            return ranked
        finally:
            with self.lock:
                self.ranking['scanning'] = False
                self._persist()
            if original:
                self._select(cfg, cfg['probe_group'], original)

    def _ranked_check(self, cfg, slots, proxy_map, providers, rows):
        targets = [s for s in slots if s.get('enabled') and s['id'] in cfg['groups']]
        for slot in targets:
            row = rows[slot['id']]
            group = cfg['groups'][slot['id']]
            row['node'] = proxy_map.get(group, {}).get('now', '')
            if row['failures'] >= cfg['failures']:
                confirm = self._probe(slot['url'], cfg['timeout'], 1)
                if confirm['ok']:
                    row.update(confirm, status='healthy', failures=0, slow_count=0, error='', last_success=time.time())
                    row['ip_node'] = row['node']
                else:
                    row['status'] = 'unavailable'
        self._publish(rows)
        if not proxy_map:
            return
        age = time.time() - self.ranking.get('completed_at', 0)
        needs_spare = any(rows[s['id']]['status'] == 'unavailable' or
                          rows[s['id']].get('slow_count', 0) >= cfg['slow_failures'] for s in targets)
        full = age >= cfg['rank_interval'] or (needs_spare and age >= max(300, cfg['interval']))
        try:
            ranked = (self._scan_candidates(cfg, proxy_map, providers) if full
                      else copy.deepcopy(self.ranking.get('ranked', [])))
        except Exception:
            self.error = 'ranking_scan_failed; no selector changes performed'
            return
        if not full:
            # Retire failed scores and update sustained slow scores before choosing
            # the target set. Otherwise the next round could rotate back to the
            # same slow exit using its obsolete fast score.
            with self.lock:
                results = copy.deepcopy(self.ranking.get('results', []))
                for slot in targets:
                    row = rows[slot['id']]
                    if row['status'] != 'unavailable' and row.get('slow_count', 0) < cfg['slow_failures']:
                        continue
                    for result in results:
                        if result['node'] == row.get('node'):
                            result.update(ok=row['status'] != 'unavailable', tested_at=time.time())
                            if result['ok']:
                                result.update(latency_ms=row['latency_ms'], ip=row['ip'])
                ranked = rank_exits(results, cfg['residential_tie_pct'])
                self.ranking.update(results=results, ranked=ranked)
        # Measurements can take minutes. Refresh reservations before committing;
        # selectors/slot edits are independently reconciled on every write.
        if full:
            for slot in slots:
                probe = self._probe(slot['url'], cfg['timeout'])
                if probe['ok']:
                    rows[slot['id']].update(probe, status='healthy', failures=0, last_success=time.time())
        reserved = {rows[s['id']].get('ip') for s in slots if s not in targets}
        usable = [r for r in ranked if r['ip'] not in reserved]
        desired = usable[:len(targets)]
        desired_ips = {r['ip'] for r in desired}
        for slot in targets:
            if self.stop_event.is_set() or self.cfg != cfg:
                return
            row = rows[slot['id']]
            bad = row['status'] == 'unavailable'
            slow = row.get('slow_count', 0) >= cfg['slow_failures']
            peers = [rows[s['id']] for s in slots if s['id'] != slot['id']]
            duplicate = any(p.get('ip') == row.get('ip') for p in peers)
            optimize = not row.get('manual') and not any(r['ip'] == row.get('ip') and r['node'] == row.get('node') for r in desired)
            if not (bad or slow or duplicate or optimize):
                continue
            if time.time() - row.get('last_switch', 0) < cfg['cooldown']:
                row['error'] = 'switch_cooldown'
                continue
            pool = usable if bad or slow else desired
            choices = [r for r in pool if r['node'] != row.get('node')]
            if slow and not bad and not full:
                choices = [r for r in choices if r['latency_ms'] <= row.get('latency_ms', 0) *
                           (1 - cfg['improvement_pct'] / 100)]
            switched = self._ranked_switch(cfg, slot, slots, proxy_map, rows, choices,
                                           'unavailable' if bad else 'slow' if slow else 'ranking')
            if not switched and not row.get('error'):
                row['error'] = 'no_unique_healthy_spare' if bad else 'no_better_unique_exit'
        with self.lock:
            self.ranking['assigned'] = len({rows[s['id']].get('ip') for s in targets
                if rows[s['id']].get('status') == 'healthy' and any(
                    r['ip'] == rows[s['id']].get('ip') and r['node'] == rows[s['id']].get('node') for r in desired)})
            self.ranking['target'] = len(targets)
            self.ranking['eligible_unique'] = len(usable)
            self.ranking['constrained'] = not cfg['allow_cross_country']

    def _ranked_switch(self, cfg, slot, slots, proxy_map, rows, candidates, reason):
        sid, group = slot['id'], cfg['groups'][slot['id']]
        row = rows[sid]
        old = proxy_map.get(group, {}).get('now')
        original_status = row['status']
        spare = proxy_map.get(cfg['probe_group'], {})
        original_spare = spare.get('now')
        if (proxy_map.get(group, {}).get('type') != 'Selector' or spare.get('type') != 'Selector'
                or cfg['probe_proxy'] in [s['url'] for s in slots]):
            row['error'] = 'dedicated_probe_selector_required'
            return False
        rejected = {s['id'] for s in slots if proxy_map.get(cfg['groups'].get(s['id']), {}).get('now') == 'REJECT'
                    and rows[s['id']].get('status') == 'unavailable'}
        peers = [rows[s['id']] for s in slots if s['id'] != sid and s['id'] not in rejected]
        if any(not p.get('ip') or not p.get('last_success') for p in peers):
            row['error'] = 'peer_exit_unknown'
            return False
        occupied = {p['ip'] for p in peers}
        used = {proxy_map.get(g, {}).get('now') for g in cfg['groups'].values()}
        try:
            for candidate in candidates:
                node = candidate['node']
                if (node in used or candidate['ip'] in occupied
                        or node not in proxy_map[group].get('all', [])):
                    continue
                if not cfg['allow_cross_country'] and (not slot.get('country_code') or
                        candidate.get('country_code') != slot['country_code']):
                    continue
                changed = False
                try:
                    # Re-probe even cached candidates: a subscription's exit may rotate.
                    probe = self._candidate_probe(cfg, node)
                    if not probe['ok'] or probe['ip'] != candidate['ip'] or probe['ip'] in occupied:
                        with self.lock:
                            for result in self.ranking.get('results', []):
                                if result['node'] == node:
                                    result['ok'] = False
                            self.ranking['ranked'] = rank_exits(self.ranking.get('results', []), cfg['residential_tie_pct'])
                        continue
                    if reason == 'slow' and probe['latency_ms'] > row.get('latency_ms', 0) * (1 - cfg['improvement_pct'] / 100):
                        continue
                    live = self._api(cfg, '/proxies')['proxies']
                    if wb_settings.proxy_slots(self.hub.ACCOUNTS_DIR) != slots or any(
                            live.get(g, {}).get('now') != proxy_map.get(g, {}).get('now') for g in cfg['groups'].values()):
                        row['error'] = 'configuration_changed; retry next round'
                        return False
                    for peer in slots:
                        g = cfg['groups'].get(peer['id'])
                        if peer['id'] != sid and peer['id'] not in rejected and g and rows[peer['id']].get('ip_node') != live.get(g, {}).get('now'):
                            row['error'] = 'peer_exit_unknown'
                            return False
                    row['status'] = 'switching'
                    self._publish(rows)
                    changed = True
                    self._select(cfg, group, node)
                    verify = self._probe(slot['url'], cfg['timeout'])
                    if not verify['ok'] or verify['ip'] != probe['ip'] or verify['ip'] in occupied:
                        self._select(cfg, group, old)
                        row['status'] = original_status
                        continue
                    stamp = time.time()
                    intel = {k: candidate.get(k, '') for k in wb_ipintel.FIELDS}
                    wb_settings.update_proxy_slot(self.hub.ACCOUNTS_DIR, sid,
                        dict(ip=verify['ip'], probed_at=int(stamp), **intel))
                    row.update(verify, status='healthy', failures=0, slow_count=0, node=node,
                               ip_node=node, error='', last_switch=stamp, last_success=stamp,
                               checked_at=stamp, generation=time.time_ns(), manual=reason == 'manual')
                    proxy_map[group]['now'] = node
                    slots[:] = wb_settings.proxy_slots(self.hub.ACCOUNTS_DIR)
                    self._record(sid, reason, old_node=old, node=node, old_ip=slot.get('ip', ''), ip=verify['ip'])
                    self._publish(rows)
                    return True
                except Exception:
                    row['status'] = original_status
                    if changed:
                        try:
                            self._select(cfg, group, old)
                        except Exception:
                            row.update(status='unavailable', error='selector_state_unknown')
                            return False
            return False
        finally:
            if original_spare:
                try:
                    self._select(cfg, cfg['probe_group'], original_spare)
                except Exception:
                    pass

    def _replace(self, cfg, slot, slots, proxy_map, providers, rows):
        sid = slot['id']
        group = cfg['groups'][sid]
        row = rows[sid]
        old_node = proxy_map.get(group, {}).get('now')
        spare = proxy_map.get(cfg['probe_group'], {})
        production = proxy_map.get(group, {})
        if (production.get('type') != 'Selector' or spare.get('type') != 'Selector'
                or cfg['probe_proxy'] in [s['url'] for s in slots]):
            row['error'] = 'dedicated_probe_selector_required'
            return
        # Unknown peer exits cannot be proved unique; fail closed instead of guessing.
        peers = [rows[s['id']] for s in slots if s['id'] != sid]
        if any(not p.get('ip') or not p.get('last_success') for p in peers):
            row['error'] = 'peer_exit_unknown'
            return
        occupied = {p['ip'] for p in peers}
        used_nodes = {proxy_map.get(g, {}).get('now') for g in cfg['groups'].values()}
        candidates = []
        for name, provider in providers.items():
            if provider.get('vehicleType') not in ('HTTP', 'File'):
                continue
            for p in provider.get('proxies', []):
                node = p.get('name')
                if (node and node != old_node and node not in used_nodes
                        and node in production.get('all', []) and node in spare.get('all', [])
                        and node not in candidates):
                    candidates.append(node)
        # Prefer nodes whose provider health check passed; probe results are still authoritative.
        alive = {p.get('name') for v in providers.values() for p in v.get('proxies', []) if p.get('alive')}
        random.shuffle(candidates)
        candidates.sort(key=lambda n: n not in alive)
        old_country = slot.get('country_code', '')
        original_spare = spare.get('now')
        try:
            for node in candidates[:cfg['candidate_limit']]:
                if self.stop_event.is_set() or self.cfg != cfg:
                    return
                changed = False
                try:
                    probe = self._candidate_probe(cfg, node)
                    if not probe['ok'] or probe['ip'] in occupied:
                        continue
                    intel = wb_ipintel.lookup(probe['ip'])
                    if not cfg['allow_cross_country'] and (not old_country or intel['country_code'] != old_country):
                        continue
                    # Reconcile manual selection and slot edits before committing.
                    live_slots = wb_settings.proxy_slots(self.hub.ACCOUNTS_DIR)
                    live = self._api(cfg, '/proxies')['proxies']
                    if live_slots != slots or any(live.get(g, {}).get('now') != proxy_map.get(g, {}).get('now')
                                                  for g in cfg['groups'].values()):
                        row['error'] = 'configuration_changed; retry next round'
                        return
                    for peer in slots:
                        g = cfg['groups'].get(peer['id'])
                        if peer['id'] != sid and g and rows[peer['id']].get('ip_node') != live.get(g, {}).get('now'):
                            row['error'] = 'peer_exit_unknown'
                            return
                    row['status'] = 'switching'
                    self._publish(rows)
                    changed = True  # A timed-out write may still have reached Mihomo.
                    self._select(cfg, group, node)
                    verify = self._probe(slot['url'], cfg['timeout'])
                    if not verify['ok'] or verify['ip'] != probe['ip'] or verify['ip'] in occupied:
                        self._select(cfg, group, old_node)
                        row['status'] = 'unavailable'
                        continue
                    stamp = time.time()
                    wb_settings.update_proxy_slot(self.hub.ACCOUNTS_DIR, sid,
                          dict(ip=verify['ip'], probed_at=int(stamp), **intel))
                    row.update(verify, status='healthy', failures=0, node=node, error='',
                               last_switch=stamp, last_success=stamp, ip_node=node,
                               generation=time.time_ns())
                    proxy_map[group]['now'] = node
                    # The current round's reservations must see committed metadata.
                    slots[:] = wb_settings.proxy_slots(self.hub.ACCOUNTS_DIR)
                    self._record(sid, 'failover', old_node=old_node, node=node,
                                 old_ip=slot.get('ip', ''), ip=verify['ip'])
                    self._publish(rows)
                    return
                except Exception:
                    row['status'] = 'unavailable'
                    # A write could have succeeded before an API timeout: best effort rollback.
                    if changed:
                        try:
                            self._select(cfg, group, old_node)
                        except Exception:
                            row['error'] = 'selector_state_unknown'
                            return
            row['error'] = 'no_unique_healthy_spare'
            self._record(sid, 'no_unique_healthy_spare')
        finally:
            if original_spare:
                try:
                    self._select(cfg, cfg['probe_group'], original_spare)
                except Exception:
                    pass


def manager(hub):
    key = os.path.abspath(hub.ACCOUNTS_DIR)
    with _lock:
        if key not in _managers:
            _managers[key] = Monitor(hub)
        return _managers[key]


def route(hub, handler, method):
    path = handler.path.split('?')[0]
    if path not in ('/proxy/health', '/proxy/health/config', '/proxy/health/check',
                    '/proxy/health/nodes', '/proxy/health/switch',
                    '/proxy/subscriptions', '/proxy/subscriptions/save', '/proxy/subscriptions/refresh'):
        return False
    if not handler._panel_ok():
        handler._error(401, 'panel password required')
        return True
    mon = manager(hub)
    try:
        if method == 'GET' and path == '/proxy/health':
            handler._json(200, mon.view())
        elif method == 'GET' and path == '/proxy/health/nodes':
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
            handler._json(200, mon.node_choices(query.get('id', [''])[0]))
        elif method == 'GET' and path == '/proxy/subscriptions':
            import wb_proxy_subscriptions
            handler._json(200, wb_proxy_subscriptions.view(mon))
        elif method == 'POST':
            payload = handler._payload_or_error()
            if payload is None:
                return True
            if path == '/proxy/health/config':
                mon.configure(payload)
            elif path == '/proxy/health/check':
                mon.trigger(rescan=payload.get('rescan') is True)
            elif path == '/proxy/health/switch':
                result = mon.manual_switch(str(payload.get('id', '')), str(payload.get('node', '')),
                                           automatic=payload.get('automatic') is True)
                handler._json(200, result)
                return True
            elif path in ('/proxy/subscriptions/save', '/proxy/subscriptions/refresh'):
                import wb_proxy_subscriptions
                result = wb_proxy_subscriptions.change(mon, payload, refresh=path.endswith('/refresh'))
                handler._json(200, result)
                return True
            else:
                handler._error(405, 'method not allowed')
                return True
            handler._json(200, mon.view())
        else:
            handler._error(405, 'method not allowed')
    except (ValueError, OSError) as exc:
        handler._error(400, str(exc))
    return True
