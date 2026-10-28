"""Opt-in, panel-only Mihomo provider editing. No subscription secrets leave here.

WB_MIHOMO_CONFIG must point to an operator-provisioned JSON-form Mihomo config
(JSON is valid YAML). Arbitrary YAML is never parsed or rewritten heuristically.
The gateway only edits HTTP providers and use-lists of its mapped selectors.
"""
import copy
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import time
import urllib.parse
from pathlib import Path


def _load():
    value = os.environ.get('WB_MIHOMO_CONFIG', '')
    if not value:
        raise ValueError('subscription_management_not_configured')
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('subscription_management_not_configured')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('managed_mihomo_config_required') from None
    if not isinstance(data, dict) or not isinstance(data.get('proxy-providers'), dict):
        raise ValueError('managed_mihomo_config_required')
    return path, data


def _url(value):
    value = str(value).strip()
    try:
        u = urllib.parse.urlsplit(value)
        if (len(value) > 8192 or u.scheme != 'https' or not u.hostname or u.username or
                u.password or u.fragment or any(c.isspace() or ord(c) < 32 for c in value)):
            raise ValueError()
        host = u.hostname.lower().rstrip('.')
        if host == 'localhost' or host.endswith(('.localhost', '.local')):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and not address.is_global:
            raise ValueError()
        u.port  # Validate malformed ports without disclosing the input.
    except ValueError:
        raise ValueError('public_https_subscription_required') from None
    return value


def _groups(mon):
    return set(mon.cfg['groups'].values()) | {mon.cfg['probe_group']}


def extract_group_tag(name_or_node):
    if not name_or_node:
        return ""
    s = str(name_or_node).strip()
    m = re.match(r"^(\[[^\]]+\]|\([^\)]+\)|【[^】]+】)", s)
    if m:
        inner = m.group(1)[1:-1].strip()
        return f"[{inner}]" if inner else ""
    p_match = re.search(r"(?:subscription|sub)[-_ ]?([a-zA-Z0-9]+)", s, re.IGNORECASE)
    if p_match:
        return f"[{p_match.group(1).upper()}]"
    return ""


def view(mon):
    try:
        _, data = _load()
    except ValueError as exc:
        return dict(available=False, error=str(exc), providers=[])
    groups = _groups(mon)
    active = {n for g in data.get('proxy-groups', []) if g.get('name') in groups for n in g.get('use', [])}
    try:
        live = mon._api(mon.cfg, '/providers/proxies')['providers']
    except Exception:
        live = {}
    rows = []
    for name, provider in data['proxy-providers'].items():
        if provider.get('type') != 'http':
            continue
        # Tokens may be in query strings OR paths. Only the hostname is returned.
        host = urllib.parse.urlsplit(str(provider.get('url', ''))).hostname or ''
        status = live.get(name, {})
        proxies = status.get('proxies', [])
        tag = ''
        for p in proxies[:5]:
            t = extract_group_tag(p.get('name', ''))
            if t:
                tag = t
                break
        if not tag:
            tag = extract_group_tag(name)
        rows.append(dict(name=name, tag=tag, source='https://' + host + '/••••',
                         enabled=name in active, interval=provider.get('interval', 3600),
                         nodes=len(proxies), updated_at=status.get('updatedAt', ''),
                         connected=bool(proxies)))
    return dict(available=True, providers=rows)


def _write(path, data, attributes):
    pending = path.with_name(path.name + '.gateway-pending')
    try:
        with pending.open('w', encoding='utf-8', newline='\n') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.chmod(str(pending), stat.S_IMODE(attributes.st_mode))
        if hasattr(os, 'chown'):
            os.chown(str(pending), attributes.st_uid, attributes.st_gid)
        os.replace(str(pending), str(path))
    finally:
        try:
            pending.unlink()
        except FileNotFoundError:
            pass


def _validate_native(path, data):
    pending = path.with_name(path.name + '.gateway-validate')
    try:
        pending.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        os.chmod(str(pending), 0o600)
        result = subprocess.run([os.environ.get('WB_MIHOMO_BIN', 'mihomo'), '-t',
            '-d', os.environ.get('WB_MIHOMO_HOME', str(path.parent)), '-f', str(pending)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
        if result.returncode:
            raise ValueError('subscription_config_validation_failed')
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('subscription_config_validation_failed') from None
    finally:
        try:
            pending.unlink()
        except FileNotFoundError:
            pass


def _restore_selectors(mon, cfg, selected, rows):
    live = mon._api(cfg, '/proxies')['proxies']
    for group, node in selected.items():
        current = live.get(group, {})
        if current.get('type') != 'Selector':
            continue
        wanted = node if node in current.get('all', []) else 'REJECT'
        if wanted not in current.get('all', []):
            raise ValueError('selector_reject_fallback_required')
        if current.get('now') != wanted:
            mon._select(cfg, group, wanted)
        for sid, mapped in cfg['groups'].items():
            if mapped != group:
                continue
            row = rows.get(sid)
            if row and (wanted != node or wanted == 'REJECT'):
                row.update(status='unavailable', ip='', last_success=0, ip_node='REJECT',
                           node='REJECT', error='subscription_node_removed', generation=time.time_ns())


def change(mon, payload, refresh=False):
    if mon.ranking.get('scanning'):
        mon.cancel_scan()
    if not mon.run_lock.acquire(timeout=0.5):
        raise ValueError('health_check_busy')
    try:
        path, old = _load()
        name = str(payload.get('name', '')).strip()
        if not re.fullmatch(r'[\w-]{1,40}', name):
            raise ValueError('subscription_name_invalid')
        cfg = copy.deepcopy(mon.cfg)
        providers = old['proxy-providers']
        existing = providers.get(name)
        if refresh:
            if not existing or existing.get('type') != 'http':
                raise ValueError('http_subscription_required')
            try:
                selected = {g: v.get('now') for g, v in mon._api(cfg, '/proxies')['proxies'].items()
                            if v.get('type') == 'Selector'}
                mon._api(dict(cfg, timeout=60), '/providers/proxies/' + urllib.parse.quote(name, safe=''), {})
                with mon.lock:
                    rows = copy.deepcopy(mon.rows)
                _restore_selectors(mon, cfg, selected, rows)
                mon._publish(rows)
            except Exception:
                raise ValueError('subscription_refresh_failed') from None
            mon.trigger(rescan=True)
            return view(mon)
        if existing and existing.get('type') != 'http':
            raise ValueError('http_subscription_required')
        enabled = payload.get('enabled', True)
        interval = payload.get('interval', 3600)
        if not isinstance(enabled, bool) or isinstance(interval, bool) or not isinstance(interval, int) or not 300 <= interval <= 2592000:
            raise ValueError('subscription_interval_or_enabled_invalid')
        raw_url = str(payload.get('url', '')).strip()
        url = _url(raw_url) if raw_url else (existing or {}).get('url')
        if not url:
            raise ValueError('public_https_subscription_required')
        data = copy.deepcopy(old)
        entry = data['proxy-providers'].setdefault(name, dict(type='http',
            **{'proxy': 'DIRECT', 'size-limit': 10 * 1024 * 1024,
               'header': {'User-Agent': ['clash.meta']},
               'health-check': dict(enable=True, url='https://www.gstatic.com/generate_204', interval=300),
               'override': {'additional-prefix': '[' + name + '] '}}))
        entry.update(url=url, interval=interval)
        if not existing or url != existing.get('url'):
            key = hashlib.sha256((name + '\n' + url).encode()).hexdigest()[:24]
            entry['path'] = './providers/gateway-' + key + '.yaml'
        managed = _groups(mon)
        found = set()
        for group in data.get('proxy-groups', []):
            if group.get('name') not in managed:
                continue
            found.add(group['name'])
            if group.get('type') != 'select' or 'REJECT' not in group.get('proxies', []):
                raise ValueError('managed_selectors_require_reject')
            uses = list(group.get('use', []))
            if not enabled:
                uses = [n for n in uses if n != name]
            elif name not in uses:
                uses.append(name)
            group['use'] = uses
            if not uses:
                raise ValueError('at_least_one_subscription_required')
        if found != managed:
            raise ValueError('slot_selector_not_configured')
        _validate_native(path, data)
        if json.loads(path.read_text(encoding='utf-8')) != old:
            raise ValueError('subscription_config_changed_retry')
        live = mon._api(cfg, '/proxies')['proxies']
        selected = {g: v.get('now') for g, v in live.items() if v.get('type') == 'Selector'}
        with mon.lock:
            rows = copy.deepcopy(mon.rows)
            old_rows = copy.deepcopy(mon.rows)
        attributes = path.stat()
        # Bounded private rollback snapshots include tokens; never return/log them.
        backups = mon.root / 'config-backups'
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = backups / ('%s.json' % time.time_ns())
        backup.write_text(json.dumps(old, ensure_ascii=False), encoding='utf-8')
        os.chmod(str(backup), 0o600)
        for previous in sorted(backups.glob('*.json'))[:-5]:
            previous.unlink()
        for row in rows.values():
            row['status'] = 'switching'
        mon._publish(rows)
        try:
            _write(path, data, attributes)
            mon._api(dict(cfg, timeout=60), '/configs?force=true', {'payload': json.dumps(data, ensure_ascii=False)})
            live_providers = mon._api(cfg, '/providers/proxies')['providers']
            if not live_providers.get(name, {}).get('proxies'):
                raise ValueError('subscription_empty_or_fetch_failed')
            rows = copy.deepcopy(old_rows)
            _restore_selectors(mon, cfg, selected, rows)
            mon._publish(rows)
        except Exception:
            try:
                _write(path, old, attributes)
                mon._api(dict(cfg, timeout=60), '/configs?force=true', {'payload': json.dumps(old, ensure_ascii=False)})
                _restore_selectors(mon, cfg, selected, old_rows)
                mon._publish(old_rows)
            except Exception:
                # Unknown live state remains blocked; no direct fallback.
                mon.error = 'subscription_rollback_failed'
                raise ValueError('subscription_rollback_failed') from None
            raise ValueError('subscription_apply_failed_rolled_back') from None
        mon._record('subscriptions', 'subscription_saved')
        mon.trigger(rescan=True)
        return view(mon)
    finally:
        mon.run_lock.release()


def delete(mon, payload):
    if mon.ranking.get('scanning'):
        mon.cancel_scan()
    if not mon.run_lock.acquire(timeout=0.5):
        raise ValueError('health_check_busy')
    try:
        path, old = _load()
        name = str(payload.get('name', '')).strip()
        if not name:
            raise ValueError('subscription_name_invalid')
        providers = old.get('proxy-providers', {})
        if name not in providers:
            raise ValueError('subscription_not_found')
        cfg = copy.deepcopy(mon.cfg)
        data = copy.deepcopy(old)
        managed = _groups(mon)
        for group in data.get('proxy-groups', []):
            if group.get('name') not in managed:
                continue
            uses = [n for n in group.get('use', []) if n != name]
            if not uses:
                raise ValueError('at_least_one_subscription_required')
            group['use'] = uses
        del data['proxy-providers'][name]
        _validate_native(path, data)
        attributes = path.stat()
        backups = mon.root / 'config-backups'
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = backups / ('%s.json' % time.time_ns())
        backup.write_text(json.dumps(old, ensure_ascii=False), encoding='utf-8')
        os.chmod(str(backup), 0o600)
        for previous in sorted(backups.glob('*.json'))[:-5]:
            previous.unlink()
        live = mon._api(cfg, '/proxies')['proxies']
        selected = {g: v.get('now') for g, v in live.items() if v.get('type') == 'Selector'}
        with mon.lock:
            rows = copy.deepcopy(mon.rows)
            old_rows = copy.deepcopy(mon.rows)
        for row in rows.values():
            row['status'] = 'switching'
        mon._publish(rows)
        try:
            _write(path, data, attributes)
            mon._api(dict(cfg, timeout=60), '/configs?force=true', {'payload': json.dumps(data, ensure_ascii=False)})
            rows = copy.deepcopy(old_rows)
            _restore_selectors(mon, cfg, selected, rows)
            mon._publish(rows)
        except Exception:
            _write(path, old, attributes)
            mon._api(dict(cfg, timeout=60), '/configs?force=true', {'payload': json.dumps(old, ensure_ascii=False)})
            _restore_selectors(mon, cfg, selected, old_rows)
            mon._publish(old_rows)
            raise ValueError('subscription_apply_failed_rolled_back') from None
        mon._record('subscriptions', 'subscription_deleted')
        mon.trigger(rescan=True)
        return view(mon)
    finally:
        mon.run_lock.release()


def nodes(mon, name):
    name = str(name or '').strip()
    if not name:
        raise ValueError('subscription_name_invalid')
    try:
        live = mon._api(mon.cfg, '/providers/proxies')['providers']
    except Exception:
        live = {}
    provider = live.get(name)
    if not provider:
        raise ValueError('subscription_not_found')
    with mon.lock:
        ranking = copy.deepcopy(mon.ranking)
    tested_map = {}
    for r in ranking.get('ranked', []) + ranking.get('results', []):
        if r.get('node'):
            tested_map[r['node']] = r
    out = []
    for p in provider.get('proxies', []):
        node_name = p.get('name', '')
        history = p.get('history', [])
        latest_delay = history[-1].get('delay') if history else None
        tested = tested_map.get(node_name) or {}
        latency = tested.get('latency_ms') or latest_delay or 0
        ip = tested.get('ip') or ''
        country = tested.get('country') or ''
        ip_type = tested.get('ip_type') or ''
        out.append({
            'name': node_name,
            'type': p.get('type', ''),
            'latency_ms': latency,
            'ip': ip,
            'country': country,
            'ip_type': ip_type,
            'alive': p.get('alive', True),
        })
    return {
        'name': name,
        'count': len(out),
        'nodes': out,
        'scanning': ranking.get('scanning', False),
        'vehicleType': provider.get('vehicleType', ''),
        'updatedAt': provider.get('updatedAt', ''),
    }


def test_nodes(mon, name):
    name = str(name or '').strip()
    if not name:
        raise ValueError('subscription_name_invalid')
    try:
        mon._api(dict(mon.cfg, timeout=60), '/providers/proxies/' + urllib.parse.quote(name, safe='') + '/healthcheck', {})
    except Exception:
        pass
    mon.trigger(rescan=True)
    return nodes(mon, name)
