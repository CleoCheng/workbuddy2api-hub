/* 本轮 UI 修正的结构契约：账号告警/积分、用量明细列配置与单一滚动上下文。 */
'use strict';
const assert = require('assert');
const {dashboardHtml, dashboardScript} = require('./_dashboard_source.js');
const html = dashboardHtml();
const script = dashboardScript();

assert.ok(/\.cool-list\{display:flex;flex-direction:column/.test(html), '账号告警应纵向排列');
assert.ok(/table\.account-cards td\[data-label="账号"\] \.cool-pill\{display:block;width:100%/.test(html), '告警 pill 应在账号列内收敛');
assert.ok(/\.acct-actions-row\{flex-wrap:nowrap;max-width:none\}/.test(html), '桌面操作区应保持原来的两行紧凑布局');
assert.ok(/table\.account-cards\{table-layout:fixed;min-width:1262px\}/.test(html), '桌面账号表应使用固定布局');
assert.ok(/td\.acct-actions\{white-space:normal;min-width:232px;width:232px;position:sticky/.test(html), '操作列应固定在可视区域右侧');
assert.ok(/@media \(max-width:640px\)[\s\S]*?td\.acct-actions\{position:static/.test(html), '移动端应取消操作列 sticky');

assert.ok(html.includes('class="credit-stack"'), '积分应使用分层容器');
assert.ok(html.includes('class="credit-main"'), '积分主值应单独一行');
assert.ok(html.includes('class="credit-meta"'), '到期/明细应位于积分下一层');
assert.ok(script.indexOf('credit-main') < script.indexOf('credit-meta'), '积分主值应先于明细层渲染');
assert.ok(script.includes('7天内到期'), '保留七天到期徽章');
assert.ok(script.includes('openCreditsDetail(&quot;'), '保留积分明细入口');

assert.ok(script.includes("const DIM_LAYOUT_KEY = 'wb-analytics-dim-layout-v1'"), '列配置应使用版本化本地存储 key');
assert.ok(script.includes("localStorage.getItem(DIM_LAYOUT_KEY + ':' + dim)"), '列配置应按维度读取');
assert.ok(script.includes('function visibleDimColumns(dim)'), '应按稳定字段生成可见列');
assert.ok(script.includes('data-dim-col'), '表头/列配置应使用稳定字段 id');
assert.ok(script.includes('dimColumnsMenu'), '应存在列设置菜单');
assert.ok(script.includes('hidden.delete(\'who\')'), '账号/首列不可隐藏');
assert.ok(!/section\.panel \.pbody\{overflow-y:auto;max-height:calc/.test(html), '不应再有 pbody 纵向嵌套滚动');
assert.ok(/#pageAnalytics \.recent-list\{display:flex;flex-direction:column;max-height:260px;overflow-y:auto[^}]*\}/.test(html), '最近请求应带 max-height + 内部纵向滚动');
assert.ok(/#pageAnalytics \.maingrid\{[^}]*align-items:stretch\}/.test(html), '主区两栏底部对齐');
assert.ok(/#modelsTable\{table-layout:fixed\}/.test(html), '网关模型清单应固定布局占满宽度');
assert.ok(/#modelsTable th,#modelsTable td\{white-space:nowrap;overflow:hidden;text-overflow:ellipsis\}/.test(html), '模型清单单元格不应换行');

// 设置分类子导航与账号过滤条
assert.ok(html.includes('class="settings-cat-bar"'), '设置页应有分类子导航栏');
assert.ok(html.includes('data-cat="apikey"'), '设置分类应包含 API Key');
assert.ok(html.includes('data-cat="proxy"'), '设置分类应包含代理网络');
assert.ok(html.includes('data-cat="model"'), '设置分类应包含模型调度');
assert.ok(html.includes('data-cat="security"'), '设置分类应包含安全隐私');
assert.ok(html.includes('data-cat="system"'), '设置分类应包含系统高级');
assert.ok(script.includes('function switchSettingsCategory('), '应定义 switchSettingsCategory 函数');
assert.ok(script.includes('function restoreSettingsCategory('), '应定义 restoreSettingsCategory 函数');

assert.ok(html.includes('class="acct-filter-bar"'), '账号页应有搜索与状态筛选栏');
assert.ok(html.includes('id="acctSearchInput"'), '应有账号搜索框');
assert.ok(html.includes('id="acctStatusFilters"'), '应有状态过滤按钮组');
assert.ok(script.includes('function filterAccountsByStatus('), '应定义 filterAccountsByStatus 函数');
assert.ok(script.includes('function filterAccountsList('), '应定义 filterAccountsList 函数');

console.log('_test_ui_layout_refinements.js: all assertions passed');
