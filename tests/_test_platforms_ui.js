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
  const document = {
    activeElement:null,
    getElementById:id => elements.get(id),
    querySelectorAll:() => items,
    addEventListener:(name, callback) => { listeners[name] = callback; },
  };
  function element(id) {
    const result = {hidden:false, attributes:{}, dataset:{}, textContent:'',
      setAttribute(name,value){ this.attributes[name] = value; },
      focus(){ document.activeElement = this; }};
    elements.set(id,result);
    return result;
  }
  const trigger = element('gatewayPlatformButton');
  const menu = element('gatewayPlatformMenu'); menu.hidden = true;
  const name = element('gatewayPlatformName');
  const items = [element('wb'),element('qd')];
  items[0].dataset.gatewayPlatform = 'workbuddy';
  items[1].dataset.gatewayPlatform = 'qoder';
  menu.querySelectorAll = () => items;
  menu.querySelector = () => items.find(item => item.attributes['aria-checked'] === 'true') || items[0];
  element('gatewayPlatformSwitcher').contains = target => target === trigger || items.includes(target);
  const context = vm.createContext({document,
    switchMainTab:tab => { navigation.push(tab); context.syncGatewayPlatform(tab); },
    postJSON:() => { throw new Error('Changing views must not change server routing settings'); },
    fetch:() => { throw new Error('Changing views must not send a routing update'); },
  });
  vm.runInContext(block,context);
  context.syncGatewayPlatform('gateway');
  context.toggleGatewayPlatformMenu();
  assert.equal(menu.hidden,false);
  assert.equal(trigger.attributes['aria-expanded'],'true');
  assert.equal(document.activeElement,items[0]);
  listeners.keydown({key:'ArrowDown',preventDefault(){}});
  assert.equal(document.activeElement,items[1]);
  context.selectGatewayPlatform('qoder');
  assert.deepEqual(navigation,['platforms']);
  assert.equal(name.textContent,'Qoder 网关');
  assert.equal(items[1].attributes['aria-checked'],'true');
  assert.equal(menu.hidden,true);
  assert.equal(document.activeElement,trigger);
  context.syncGatewayPlatform('settings');
  assert.equal(name.textContent,'Qoder 网关','shared pages preserve the current platform context');
  context.syncGatewayPlatform('accounts');
  assert.equal(name.textContent,'WorkBuddy 网关');
  context.toggleGatewayPlatformMenu();
  listeners.keydown({key:'Escape',preventDefault(){}});
  assert.equal(menu.hidden,true);
  assert.equal(document.activeElement,trigger);
  context.toggleGatewayPlatformMenu();
  listeners.click({target:{}});
  assert.equal(menu.hidden,true);
  context.selectGatewayPlatform('unrecognised');
  assert.deepEqual(navigation,['platforms']);
}

(async () => {
  checkManagementSwitcher();
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
