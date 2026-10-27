/* The proxy-slot editor must survive the panel's polling (issue #79).
 *
 * loadAccounts() runs every 15 seconds and used to refresh the slot list with
 * it. That refresh replaces the whole list and re-renders the table, so a row
 * the operator had just added with 「+ 添加槽位」 - not saved to the server yet -
 * was wiped before they could type anything into it. The editor now refuses to
 * be refreshed while it holds unsaved changes, and says so in its header.
 *
 * Run with Node: node tests/_test_slot_editor.js
 */
const assert = require('assert');
const {dashboardScript} = require('./_dashboard_source.js');

const script = dashboardScript();

// One shared fake DOM for every dashboard suite: tests/_dom_stub.js.
const dom = require('./_dom_stub.js');

// What the server would return for GET /proxy/slots; the test swaps it.
let serverSlots = [];
let health = {config:{enabled:false,interval:90,failures:3,cooldown:600,timeout:5,
                     candidate_limit:12,groups:{}}, slots:{}, history:[]};
let healthSaved;
dom.installDom({
  confirm: () => true,
  fetch: (url, options) => {
    if(url === '/proxy/health/config') healthSaved = JSON.parse(options.body);
    const payload = String(url).startsWith('/proxy/health') ? health : {current: 'intl', accounts: [], slots: serverSlots, data: [],
                     results: [], byAccount: []};
    return Promise.resolve({status: 200, ok: true,
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload))});
  },
});

const api = new Function(script + `
  window.updateUI = updateUI;
  window.toast = toast;
  return {
    loadProxySlots,
    addProxySlotRow,
    importDiscovered,
    discovered: rows => { SLOT_DISCOVER = rows; },
    saveProxySlots,
    loadProxyHealth,
    saveProxyHealth,
    healthDirty: value => { PROXY_HEALTH_DIRTY = value; },
    rows: () => PROXY_SLOTS,
    dirty: () => SLOTS_DIRTY,
    state: () => (document.getElementById('slotState') || {}).textContent,
  };`)();

(async () => {
  // 1. A clean editor follows the server.
  serverSlots = [{id: 'a', name: 'a', url: 'http://127.0.0.1:1', enabled: true}];
  await api.loadProxySlots();
  assert.equal(api.rows().length, 1, 'a clean editor loads the server list');

  // 2. Adding a row marks the editor dirty, and the header says so.
  api.addProxySlotRow();
  assert.equal(api.rows().length, 2, 'the new row is in the working copy');
  assert.equal(api.dirty(), true, 'adding a row marks the editor dirty');
  assert.ok(api.state().includes('未保存'), 'the header admits the list is unsaved: ' + api.state());

  // 3. The periodic refresh must not wipe it (this is the reported bug).
  serverSlots = [{id: 'a', name: 'a', url: 'http://127.0.0.1:1', enabled: true},
                 {id: 'b', name: 'b', url: 'http://127.0.0.1:2', enabled: true}];
  await api.loadProxySlots();
  assert.equal(api.rows().length, 2, 'the poll must not replace the working copy');
  // The new row is the one with no id yet - the server rows above all have
  // one - so a poll that replaced the working copy would fail this.
  assert.equal(api.rows()[1].id, '',
               'the row being filled in is still there: ' + JSON.stringify(api.rows()[1]));

  // 4. An explicit refresh still can - the polling path is the only one held.
  await api.loadProxySlots(true);
  assert.equal(api.rows().map(r => r.id).join(','), 'a,b',
               'force reloads from the server: ' + JSON.stringify(api.rows()));

  // 5. Saving clears the flag, so polling resumes afterwards.
  api.addProxySlotRow();
  assert.equal(api.dirty(), true, 'the new row is unsaved again');
  await api.saveProxySlots(null).catch(() => {});
  assert.equal(api.dirty(), false, 'a successful save clears the dirty flag');
  assert.ok(!api.state().includes('未保存'),
            'the header stops saying unsaved: ' + api.state());

  // 6. Discovered exits are edits too: the next poll must not erase imports.
  const discovered = {url: 'http://127.0.0.1:17901', exit_ip: '203.0.113.1',
                      country: 'Example', country_code: 'ZZ', reachable: true};
  api.discovered([discovered]);
  const discoverBox = document.getElementById('slotDiscover');
  discoverBox.querySelectorAll = () => [{dataset: {idx: '0'}}];
  api.importDiscovered();
  assert.equal(api.dirty(), true, 'imported exits remain unsaved edits');
  assert.ok(api.state().includes('未保存'), 'imports show the unsaved indicator');
  await api.loadProxySlots();
  const imported = api.rows().find(row => row.url === discovered.url);
  assert.ok(imported, 'polling must retain the imported exit until saved');
  assert.equal(imported.ip, discovered.exit_ip, 'polling preserves probe metadata');

  // Re-importing an existing endpoint must not create a duplicate.
  api.importDiscovered();
  assert.equal(api.rows().filter(row => row.url === discovered.url).length, 1);

  // Health controls share the polling safeguard and persist the actual values.
  await api.loadProxyHealth();
  const interval = document.getElementById('proxyHealthInterval');
  interval.value = '120';
  api.healthDirty(true);
  await api.loadProxyHealth();
  assert.equal(interval.value, '120', 'health polling preserves edited controls');
  document.getElementById('proxyHealthGroups').value = 'slot-1=Exit 01';
  await api.saveProxyHealth({disabled:false});
  assert.equal(healthSaved.interval, 120);
  assert.equal(healthSaved.groups['slot-1'], 'Exit 01');
  health.slots['slot-1'] = {id:'slot-1', node:'<script>bad</script>', status:'healthy', ip:'203.0.113.1'};
  await api.loadProxyHealth();
  assert.ok(!document.getElementById('proxyHealthRows').innerHTML.includes('<script>'));

  console.log('slot editor assertions passed');
})().catch(error => { console.error(error); process.exit(1); });

