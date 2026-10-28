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
                     ranked:true,rank_interval:1800,slow_ms:1500,slow_failures:3,improvement_pct:20,residential_tie_pct:10,
                     candidate_limit:12,groups:{}}, slots:{}, history:[]};
let healthSaved;
let testBehavior=null;
let adminBehavior=null;
dom.installDom({
  confirm: () => true,
  fetch: (url, options) => {
    if(adminBehavior && (String(url).startsWith('/proxy/subscriptions') || String(url).startsWith('/proxy/health/nodes') || url==='/proxy/health/switch' || url==='/accounts/set' || url==='/proxy/health/cancel-scan')) return adminBehavior(url,options);
    if(url==='/proxy/slots/test' && testBehavior) return testBehavior(JSON.parse(options.body));
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
    slotLatencyState,slotLatencyHtml,renderProxySlots,testProxySlot,
    testAllProxySlots,
    openProxyNodeModal,chooseProxyNode,closeProxyNodeModal,resumeProxySlotAuto,
    loadProxySubscriptions,openProxySubscription,closeProxySubscription,saveProxySubscription,
    deleteProxySubscription,viewSubscriptionNodes,closeSubNodesModal,loadSubNodesData,renderSubNodesList,
    cancelHealthScan,fmtSubInterval,
    setSlotMaxAccounts,batchSetSlotMaxAccounts,autoAssignSlots,slotPayload,
    slotSelectHtml,slotExitHtml,autoSlotName,slotGroupTag,
    batchFixture: rows => {PROXY_SLOTS=rows;SLOTS_DIRTY=false;},
    manualProbe:(url,row)=>SLOT_MANUAL_PROBES.set(url,row),
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
  assert.equal(healthSaved.ranked, true);
  assert.equal(healthSaved.slow_ms, 1500);
  assert.equal(healthSaved.rank_interval, 1800);
  assert.equal(healthSaved.residential_tie_pct, 10);
  health.ranking={done:47,total:47,assigned:10,target:10,ranked:[{rank:1,node:'<script>rank</script>',ip:'8.8.8.8',latency_ms:100}]};
  health.slots['slot-1'] = {id:'slot-1', node:'<script>bad</script>', status:'healthy', ip:'203.0.113.1'};
  await api.loadProxyHealth();
  assert.ok(!document.getElementById('proxyHealthRows').innerHTML.includes('<script>'));
  assert.ok(!document.getElementById('proxyHealthRanking').innerHTML.includes('<script>'));
  assert.ok(document.getElementById('proxyHealthRanking').innerHTML.includes('47 / 47'));

  // Delay bands include exact boundaries; errors never display an old success.
  const measured={id:'measured',url:'http://127.0.0.1:17903',enabled:true};
  const stamp=Date.now()/1000;
  for(const [ms,tone] of [[0,'ok'],[300,'ok'],[301,'warn'],[800,'warn'],[801,'bad']]){
    api.manualProbe(measured.url,{status:'healthy',checked_at:stamp,latency_ms:ms});
    const state=api.slotLatencyState(measured,stamp);
    assert.equal(state.tone,tone,'delay band for '+ms);
    assert.equal(state.text,ms+' ms');
  }
  for(const [status,tone,text] of [['suspect','warn','连接异常'],['unavailable','bad','不可用'],['switching','warn','切换中']]){
    api.manualProbe(measured.url,{status,checked_at:stamp,latency_ms:10});
    assert.equal(api.slotLatencyState(measured,stamp).tone,tone);
    assert.equal(api.slotLatencyState(measured,stamp).text,text);
    assert(!api.slotLatencyHtml(measured).includes('10 ms'),'failed probes cannot look healthy');
  }
  api.manualProbe(measured.url,{status:'healthy',checked_at:stamp-1000,latency_ms:1});
  assert.equal(api.slotLatencyState(measured,stamp).text,'已过期');
  assert.equal(api.slotLatencyState({...measured,enabled:false},stamp).text,'已停用');
  assert.equal(api.slotLatencyState({...measured,url:'http://127.0.0.1:17904'},stamp).text,'未探测','changed endpoint cannot inherit old latency');
  for(const value of [null,'12',NaN,Infinity,-1]){
    api.manualProbe(measured.url,{status:'healthy',checked_at:stamp,latency_ms:value});
    assert.equal(api.slotLatencyState(measured,stamp).text,'未探测');
  }
  // Background health updates can update the latency while preserving dirty edits.
  const row=api.rows()[0],cell=document.createElement('td');cell.dataset.slotLatencyIndex='0';
  const originalQuery=document.querySelectorAll;
  document.querySelectorAll=selector=>selector==='[data-slot-latency-index]'?[cell]:originalQuery.call(document,selector);
  health.slots[row.id]={id:row.id,url:row.url,status:'healthy',checked_at:stamp,latency_ms:521};
  await api.loadProxyHealth();
  assert(cell.innerHTML.includes('slot-latency-warn')&&cell.innerHTML.includes('521 ms'));
  assert.equal(api.dirty(),true,'latency refresh cannot clear unsaved slot edits');
  health.slots[row.id].status='unavailable';await api.loadProxyHealth();
  assert(cell.innerHTML.includes('不可用')&&!cell.innerHTML.includes('521 ms'));
  api.renderProxySlots();assert(document.getElementById('slotList').innerHTML.includes('data-label="延迟"'));
  document.querySelectorAll=originalQuery;

  // All-port checks use three workers, include disabled ports and keep failures isolated.
  const batchRows=Array.from({length:7},(_,i)=>({id:'batch-'+i,url:'http://127.0.0.1:'+(18000+i),enabled:i!==6,name:''}));
  api.batchFixture(batchRows);
  let inflight=0,peak=0,seen=[],release;
  const barrier=new Promise(resolve=>{release=resolve;});
  testBehavior=async body=>{
    seen.push(body.id);inflight++;peak=Math.max(peak,inflight);await barrier;
    try{
      if(body.id==='batch-1') throw new Error('<bad-port>');
      const payload={ok:body.id!=='batch-2',latency_ms:123,error:'connection failed',slot:{ip:'203.0.113.2'}};
      return {ok:true,status:200,json:async()=>payload,text:async()=>JSON.stringify(payload)};
    }finally{inflight--;}
  };
  const batchButton={disabled:false};
  const pending=api.testAllProxySlots(batchButton);
  await Promise.resolve();await Promise.resolve();
  assert.equal(peak,3,'batch probes cap concurrency at three');
  assert.equal(batchButton.disabled,true);
  await api.testAllProxySlots(batchButton);
  assert.equal(seen.length,3,'repeated click does not start a second batch');
  serverSlots=[];await api.loadProxySlots(true);assert.equal(api.rows().length,7,'polls cannot replace the batch snapshot');
  await api.saveProxySlots(null);assert.equal(api.rows().length,7,'do not save changed endpoints while probes are in flight');
  release();await pending;
  assert.equal(seen.length,7,'one failed request cannot stop remaining ports');
  assert(seen.includes('batch-6'),'test disabled port without enabling it');
  assert.equal(api.rows()[6].enabled,false);
  assert.equal(batchButton.disabled,false);
  const status=document.getElementById('slotBatchState').textContent;
  assert(status.includes('7/7')&&status.includes('5 通过')&&status.includes('2 失败'));
  assert(status.includes('<bad-port>'),'error details are written as text, not HTML');
  assert.equal(api.slotLatencyState(batchRows[0]).text,'123 ms');
  assert.equal(api.slotLatencyState(batchRows[1]).text,'不可用');
  api.addProxySlotRow();const old=seen.length;
  await api.testAllProxySlots(batchButton);
  assert.equal(seen.length,old,'never auto-save dirty rows just to run probes');
  assert(document.getElementById('slotBatchState').textContent.includes('请先保存'));

  // Node choices preserve server ordering, escape untrusted labels and cannot select occupied exits.
  const calls=[];
  adminBehavior=async (url,options)=>{
    let payload={available:true,providers:[{name:'test-sub',source:'https://example.com/••••',enabled:true,interval:3600,nodes:15}]};
    if(url.startsWith('/proxy/health/nodes'))payload={id:'manual-slot',current:'old',manual:false,nodes:[
      {node:'<script>node</script>',ip:'8.8.8.8',latency_ms:100,country:'US',selectable:true},
      {node:'occupied',ip:'8.8.4.4',latency_ms:90,selectable:false,unavailable_reason:'exit_occupied'}]};
    if(url.startsWith('/proxy/subscriptions/nodes'))payload={name:'test-sub',count:1,nodes:[
      {name:'[B] test-node-1',type:'Shadowsocks',latency_ms:120,ip:'1.2.3.4',country:'US',alive:true}]};
    if(url==='/proxy/health/cancel-scan')payload={ok:true,cancelled:true};
    if(url==='/proxy/subscriptions/delete')payload={available:true,providers:[]};
    if(options && options.body) calls.push({url,body:JSON.parse(options.body)});
    return {status:200,ok:true,json:async()=>payload,text:async()=>JSON.stringify(payload)};
  };
  api.batchFixture([{id:'manual-slot',url:'http://127.0.0.1:17901',enabled:true}]);
  await api.openProxyNodeModal(0);
  assert(document.getElementById('proxyNodeModal').classList.contains('show'));
  const choices=document.getElementById('proxyNodeChoices').innerHTML;
  assert(choices.includes('&lt;script&gt;')&&!choices.includes('<script>'));
  assert(choices.includes('已被其他槽位占用'));
  await api.chooseProxyNode(1,{disabled:false});assert.equal(calls.length,0);
  await api.chooseProxyNode(0,{disabled:false});
  assert.deepEqual(calls[0],{url:'/proxy/health/switch',body:{id:'manual-slot',node:'<script>node</script>'}});
  assert(!document.getElementById('proxyNodeModal').classList.contains('show'));

  // Subscription editing never fills an existing secret URL; blank means retain it.
  await api.loadProxySubscriptions();api.openProxySubscription(0);
  assert.equal(document.getElementById('proxySubscriptionURL').value,'');
  assert.equal(document.getElementById('proxySubscriptionName').disabled,true);
  await api.saveProxySubscription({disabled:false});
  const saved=calls.find(c=>c.url==='/proxy/subscriptions/save').body;
  assert.deepEqual(saved,{name:'test-sub',url:'',interval:3600,enabled:true});
  assert.equal(document.getElementById('proxySubscriptionURL').value,'');
  api.openProxySubscription();
  assert.equal(document.getElementById('proxySubscriptionName').disabled,false);
  document.getElementById('proxySubscriptionURL').value='https://private.example/sub?token=secret';
  api.closeProxySubscription();
  assert.equal(document.getElementById('proxySubscriptionURL').value,'','closing the editor clears subscription credentials');

  // Subscription interval units formatting
  assert.equal(api.fmtSubInterval(86400), '1 天');
  assert.equal(api.fmtSubInterval(21600), '6 小时');
  assert.equal(api.fmtSubInterval(1800), '30 分钟');

  // View subscription nodes
  await api.viewSubscriptionNodes(0);
  assert.ok(document.getElementById('proxySubNodesModal').classList.contains('show'));
  assert.ok(document.getElementById('proxySubNodesList').innerHTML.includes('[B] test-node-1'));
  assert.ok(document.getElementById('proxySubNodesList').innerHTML.includes('120 ms'));
  api.closeSubNodesModal();
  assert.ok(!document.getElementById('proxySubNodesModal').classList.contains('show'));

  // Delete subscription
  await api.deleteProxySubscription(0, {disabled: false});
  const delCall = calls.find(c => c.url === '/proxy/subscriptions/delete');
  assert.deepEqual(delCall.body, {name: 'test-sub'});

  // Cancel background health scan
  await api.cancelHealthScan({disabled: false});
  assert.ok(calls.some(c => c.url === '/proxy/health/cancel-scan'));

  // Max accounts column & batch limit configuration
  api.batchFixture([
    {id: 's1', url: 'http://127.0.0.1:18001', enabled: true, bound: 0, max_accounts: 0},
    {id: 's2', url: 'http://127.0.0.1:18002', enabled: true, bound: 0, max_accounts: 0},
  ]);
  api.setSlotMaxAccounts(0, '5');
  assert.equal(api.rows()[0].max_accounts, 5, 'setSlotMaxAccounts sets individual slot limit');
  assert.equal(api.dirty(), true, 'setting max accounts marks dirty');
  api.setSlotMaxAccounts(0, '0');
  assert.equal(api.rows()[0].max_accounts, 0, 'setting 0 clears to unlimited');
  api.setSlotMaxAccounts(0, '-3');
  assert.equal(api.rows()[0].max_accounts, 0, 'negative limit falls back to 0');

  // Batch setting all slot limits via prompt
  const origPrompt = global.window.prompt;
  global.window.prompt = () => '4';
  api.batchSetSlotMaxAccounts();
  assert.equal(api.rows()[0].max_accounts, 4, 'batch limit applies to slot 1');
  assert.equal(api.rows()[1].max_accounts, 4, 'batch limit applies to slot 2');
  assert.equal(api.slotPayload()[0].max_accounts, 4, 'slotPayload includes max_accounts');
  global.window.prompt = origPrompt;

  // autoAssignSlots: capacity limits and rollover
  const assigned = [];
  const prevAdmin = adminBehavior;
  adminBehavior = (url, options) => {
    if (url === '/accounts/set') {
      assigned.push(JSON.parse(options.body));
      return Promise.resolve({status: 200, ok: true, json: () => Promise.resolve({ok: true}), text: () => Promise.resolve('{"ok":true}')});
    }
    return prevAdmin ? prevAdmin(url, options) : null;
  };

  // Configure slots: s1 limit 2, s2 limit 1
  const slotFixtures = [
    {id: 's1', url: 'http://127.0.0.1:18001', enabled: true, bound: 0, max_accounts: 2},
    {id: 's2', url: 'http://127.0.0.1:18002', enabled: true, bound: 0, max_accounts: 1},
  ];
  serverSlots = slotFixtures;
  api.batchFixture(slotFixtures);
  global.window.ACCOUNTS = [
    {uid: 'u1', enabled: true, proxySlot: ''},
    {uid: 'u2', enabled: true, proxySlot: ''},
    {uid: 'u3', enabled: true, proxySlot: ''},
    {uid: 'u4', enabled: true, proxySlot: ''},
  ];

  await api.autoAssignSlots();
  // u1 -> s1 (count 1/2)
  // u2 -> s2 (count 1/1, full)
  // u3 -> candidate s1 (count 2/2, full)
  // u4 -> s2 (full) -> s1 (full) -> cannot assign
  assert.equal(assigned.length, 3, 'only 3 accounts fit in slots');
  assert.deepEqual(assigned, [
    {uid: 'u1', proxySlot: 's1'},
    {uid: 'u2', proxySlot: 's2'},
    {uid: 'u3', proxySlot: 's1'},
  ], 'rollover allocates accounts up to slot caps');

  // Next round: both s1 and s2 are full, so no new assignment can happen
  assigned.length = 0;
  api.batchFixture(slotFixtures);
  global.window.ACCOUNTS = [
    {uid: 'u1', enabled: true, proxySlot: 's1'},
    {uid: 'u2', enabled: true, proxySlot: 's2'},
    {uid: 'u3', enabled: true, proxySlot: 's1'},
    {uid: 'u4', enabled: true, proxySlot: ''},
  ];
  await api.autoAssignSlots();
  assert.equal(assigned.length, 0, 'no assignment when all slots are full');

  adminBehavior = prevAdmin;

  // Account table exit dropdown & IP display
  api.batchFixture([
    {id: 'slot-b', name: '', url: 'http://127.0.0.1:18001', enabled: true, ip: '38.58.56.34', country: '美国', ip_type: 'residential', group: '[B]'},
  ]);
  const acctHtml = api.slotSelectHtml({uid: 'u-ip-test', proxySlot: 'slot-b'});
  assert.ok(acctHtml.includes('38.58.56.34'), 'slotSelectHtml displays exit IP in cell');
  assert.ok(acctHtml.includes('美国 住宅 [B] (38.58.56.34)'), 'slotSelectHtml option displays label with group and IP');

  // Proxy slot exit column & auto name with subscription group
  const exitHtml = api.slotExitHtml(api.rows()[0]);
  assert.ok(exitHtml.includes('38.58.56.34'), 'slotExitHtml displays IP');
  assert.ok(exitHtml.includes('[B]'), 'slotExitHtml displays subscription group badge');
  assert.equal(api.autoSlotName(api.rows()[0]), '美国 住宅 [B]', 'autoSlotName includes subscription group');

  console.log('slot editor assertions passed');
})().catch(error => { console.error(error); process.exit(1); });

