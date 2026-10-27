'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const source = scripts.find(s => s.includes('async function startQoderLogin'));
assert(source, 'Google authorization entry must be executable');

function fixture(authUrl) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {style:{}, textContent:'', href:''});
    return elements.get(id);
  };
  const calls = [];
  const timers = [];
  const context = vm.createContext({
    URL, Date, console,
    document: {getElementById:element},
    postJSON: async (url, body) => {
      calls.push({url, body});
      return {state:'opaque-test-state', authUrl};
    },
    getJSON: async url => { calls.push({url}); return {status:'ok', account:{uid:'q-test'}}; },
    setTimeout: fn => { timers.push(fn); return timers.length; },
    clearTimeout: () => {},
    toast: () => {},
  });
  vm.runInContext(source, context);
  vm.runInContext('loadPlatforms = async function(){ windowLoaded = true; }', context);
  return {context, element, calls, timers};
}

function checkManagementSwitcher() {
  const block = html.split('// ==== 平台切换菜单开始 ====')[1].split('// ==== 平台切换菜单结束 ====')[0];
  const listeners = {};
  const elements = new Map();
  const navigation = [];
  const workbuddyLoads = [];
  const storage = new Map();
  const document = {
    activeElement:null,
    getElementById:id => elements.get(id),
    querySelectorAll:() => items,
    addEventListener:(name, callback) => { listeners[name] = callback; },
  };
  function element(id) {
    const classes = new Set();
    const result = {hidden:false, style:{}, attributes:{}, dataset:{}, textContent:'',
      classList:{toggle(name,on){ if(on) classes.add(name); else classes.delete(name); },contains:name=>classes.has(name)},
      setAttribute(name,value){ this.attributes[name] = value; },
      focus(){ document.activeElement = this; }};
    elements.set(id,result);
    return result;
  }
  const trigger = element('gatewayPlatformButton');
  const menu = element('gatewayPlatformMenu'); menu.hidden = true;
  const name = element('gatewayPlatformName');
  const pageNames = ['gateway','accounts','tasks','analytics','agents','settings','logs','platforms'];
  for(const page of pageNames){
    const suffix = page[0].toUpperCase()+page.slice(1);
    element('btnNav'+suffix); element('page'+suffix);
  }
  element('navGatewayLabel'); element('qoderPlatformControls'); element('qoderFrame');
  const items = [element('wb'),element('qd')];
  items[0].dataset.gatewayPlatform = 'workbuddy';
  items[1].dataset.gatewayPlatform = 'qoder';
  menu.querySelectorAll = () => items;
  menu.querySelector = () => items.find(item => item.attributes['aria-checked'] === 'true') || items[0];
  element('gatewayPlatformSwitcher').contains = target => target === trigger || items.includes(target);
  const context = vm.createContext({document,
    URL,URLSearchParams,
    window:{location:{href:'https://example.test/?tab=gateway',search:'?tab=gateway'}},
    history:{replaceState(_a,_b,url){context.window.location.href=url;context.window.location.search=new URL(url).search;}},
    localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
    AGENTS_AVAILABLE:true,_accountsSeparateTab:false,currentMainTab:'gateway',MAIN_TAB_STORE:'wb-proxy-main-tab',
    pageHidden:()=>false,firstVisiblePage:()=>'gateway',initPageNav:()=>{},
    stopLogPolling:()=>{},restorePageNavHash:()=>{},resetQoderPanel:()=>{},
    showQoderView:tab=>navigation.push(tab),loadPlatforms:()=>{},
    ...Object.fromEntries(['loadGrowthTasks','loadActivityBoard','loadAnalytics','loadAnalyticsMatrix',
      'loadSettings','loadProxySlots','loadAgents','startLogPolling','fetchNewLogs'].map(key=>[key,()=>workbuddyLoads.push(key)])),
    postJSON:() => { throw new Error('Changing views must not change server routing settings'); },
    fetch:() => { throw new Error('Changing views must not send a routing update'); },
  });
  vm.runInContext(block,context);
  const switchSource = scripts.join('\n').split('function switchMainTab(tab){')[1].split('/* ---- 智能体一键配置模块 ----')[0];
  vm.runInContext('function switchMainTab(tab){'+switchSource,context);
  context.syncGatewayPlatform();
  context.toggleGatewayPlatformMenu();
  assert.equal(menu.hidden,false);
  assert.equal(trigger.attributes['aria-expanded'],'true');
  assert.equal(document.activeElement,items[0]);
  listeners.keydown({key:'ArrowDown',preventDefault(){}});
  assert.equal(document.activeElement,items[1]);
  context.selectGatewayPlatform('qoder');
  assert.deepEqual(navigation,['gateway']);
  assert.equal(name.textContent,'Qoder 网关');
  assert.equal(items[1].attributes['aria-checked'],'true');
  assert.equal(menu.hidden,true);
  assert.equal(document.activeElement,trigger);
  for(const tab of ['accounts','analytics','logs','tasks','gateway']){
    context.switchMainTab(tab);
    assert.equal(name.textContent,'Qoder 网关','navigation must retain the selected platform');
    assert.equal(elements.get('pagePlatforms').classList.contains('active'),true);
    assert.equal(elements.get('page'+tab[0].toUpperCase()+tab.slice(1)).classList.contains('active'),false);
    assert.equal(elements.get('btnNav'+tab[0].toUpperCase()+tab.slice(1)).classList.contains('active'),true);
    assert.equal(new URL(context.window.location.href).searchParams.get('platform'),'qoder');
  }
  assert.deepEqual(workbuddyLoads,[],'Qoder navigation must not invoke WorkBuddy page loaders');
  context.switchMainTab('settings');
  assert.equal(name.textContent,'Qoder 网关','shared pages preserve the current platform context');
  assert.equal(elements.get('pageSettings').classList.contains('active'),true);
  assert.equal(storage.get('wb-proxy-management-platform'),'qoder');
  context.restoreGatewayPlatform();
  context.switchMainTab('accounts');
  assert.equal(name.textContent,'Qoder 网关','restore must retain Qoder even for an accounts URL');
  assert.equal(elements.get('btnNavAccounts').style.display,'','Qoder accounts remain visible when WorkBuddy merges accounts');
  context.toggleGatewayPlatformMenu();
  listeners.keydown({key:'Escape',preventDefault(){}});
  assert.equal(menu.hidden,true);
  assert.equal(document.activeElement,trigger);
  context.toggleGatewayPlatformMenu();
  listeners.click({target:{}});
  assert.equal(menu.hidden,true);
  const before = navigation.slice();
  context.selectGatewayPlatform('unrecognised');
  assert.deepEqual(navigation,before);
  context.selectGatewayPlatform('workbuddy');
  assert.equal(name.textContent,'WorkBuddy 网关');
  assert.equal(elements.get('pageGateway').classList.contains('active'),true);
  context.switchMainTab('accounts');
  assert.equal(context.currentMainTab,'gateway','WorkBuddy account merging remains unchanged');
  assert.equal(context.isQoderView('accounts'),false);
}

async function checkEmbeddedNavigation() {
  const f = fixture('');
  const targets = [];
  const requests = [];
  const tabs = ['gateway','accounts','tasks','analytics','logs'];
  const frame = f.element('qoderFrame');
  frame.contentWindow = {switchMainTab:tab=>targets.push(tab)};
  Object.assign(f.context,{
    QODER_VIEW_TABS:tabs,currentMainTab:'accounts',isQoderView:tab=>tabs.includes(tab),
    authHeaders:()=>({'X-Panel-Token':'test-panel-session'}),
    fetch:async (url,options)=>{requests.push({url,options});return {ok:true,text:async()=>'<head></head><body></body>'};},
  });
  const first = f.context.openQoderPanel('gateway');
  const second = f.context.openQoderPanel('accounts');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(typeof frame.onload,'function');
  frame.onload();
  await Promise.all([first,second]);
  assert.equal(requests.length,1,'rapid page changes share one embedded document');
  assert.equal(targets.at(-1),'accounts','the latest selected page wins an in-flight load');
  frame.contentWindow.switchMainTab('gateway');
  assert.equal(targets.at(-1),'accounts','late child bootstrap must not overwrite the outer selection');
  f.context.currentMainTab='logs';
  await f.context.openQoderPanel('logs');
  assert.equal(targets.at(-1),'logs');
  f.context.currentMainTab='tasks';
  await f.context.openQoderPanel('tasks');
  assert.equal(targets.at(-1),'accounts','Qoder benefits use its accounts page');
  assert(f.element('qoderViewStatus').textContent.includes('账号版本'));
  assert(frame.srcdoc.includes('body>header{display:none!important}'),'only the outer navigation should be visible');
  assert.equal(f.calls.length,0,'opening provider views must not change server settings');
  f.context.resetQoderPanel();
  assert.equal(frame.srcdoc,'','switching to WorkBuddy releases the iframe and its polling');
  assert.equal(frame.style.display,'none');
  assert.equal(frame.onload,null);

  f.context.fetch=async()=>({ok:false,status:503});
  await Promise.all([f.context.openQoderPanel('gateway'),f.context.openQoderPanel('logs')]);
  assert.equal(f.element('qoderPlatformControls').hidden,false,'show enable/retry controls on worker failure');
  assert.equal(f.element('qoderViewStatus').textContent,'Qoder: HTTP 503');
}

(async () => {
  checkManagementSwitcher();
  await checkEmbeddedNavigation();
  const auth = 'https://qoder.com/device/selectAccounts?challenge=test&nonce=n&client_id=c';
  const good = fixture(auth);
  await good.context.startQoderLogin();
  assert.equal(good.element('qoderLoginLink').href, auth, 'preserve OAuth callback parameters');
  assert.equal(good.calls[1].body.realm, 'intl', 'Google login uses international realm');
  assert.equal(good.timers.length, 1, 'start one poll loop');
  await good.timers[0]();
  assert(good.calls.some(c => c.url.includes('state=opaque-test-state')));
  assert.equal(good.element('qoderLoginLink').style.display, 'none');
  assert.equal(good.element('qoderLoginStatus').textContent, 'Qoder 授权成功');

  for (const url of ['https://evil.example/device/selectAccounts', 'http://qoder.com/device/selectAccounts']) {
    const bad = fixture(url);
    await bad.context.startQoderLogin();
    assert.equal(bad.timers.length, 0, 'do not poll an invalid authorization URL');
    assert.equal(bad.element('qoderLoginLink').href, '');
  }
  assert(html.includes("platforms: row.platforms || ['workbuddy']"));
  assert(html.includes('editKeyPlatform_'));
  assert(html.includes('允许平台（可多选）'));
  for (const source of scripts) new vm.Script(source);
  console.log('OK platform OAuth and dashboard syntax checks');
})().catch(error => { console.error(error); process.exitCode = 1; });
