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

(async () => {
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
