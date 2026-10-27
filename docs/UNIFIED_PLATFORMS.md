# 统一平台网关 / Unified platform gateway

个人分支在 WorkBuddy2API-Hub v1.8.0 上集成 Qoder2API-Hub v1.7.7。
Qoder 运行时代码、许可证与 SHA-256 清单位于 `third_party/qoder2api/`。

This personal branch integrates Qoder2API-Hub v1.7.7 into WorkBuddy2API-Hub
v1.8.0. The upstream runtime remains unmodified and is pinned by a hash manifest.

## 配置 / Configuration

点击左上角网关名称，可在下拉菜单切换 WorkBuddy / Qoder 管理平台。
顶部「网关、账号、任务与福利、数据看板、运行日志」跟随所选平台，刷新后保留
平台与页面；「设置、智能体配置」共用统一网关配置。Qoder 福利沿用其账号页，
是否提供取决于账号版本。切换到 WorkBuddy 会释放内嵌 Qoder 页面及其轮询。
客户端统一使用同一 `/v1` 入口，实际请求平台由 API Key 的平台权限、模型匹配
与可用账号决定，面板的平台选择不改变调用策略。

Click the gateway name to select a management platform. Gateway, accounts,
benefits, analytics and logs follow that platform, including after refresh.
Settings and agent setup remain shared. Qoder benefits live on its accounts page
and depend on account region. Switching to WorkBuddy releases the embedded Qoder
page and its polling. Both platforms still share `/v1`; a key's platform scopes,
matching models and ready accounts determine API routing.

1. 在现有面板的「平台管理」中点击「Qoder 国际版授权（支持 Google）」生成带完整
   回调参数的设备授权链接；在常用浏览器打开链接并选择 Google 登录，本地页面会轮询
   并自动加入账号。国内版 OAuth 和 PAT 导入使用内嵌 Qoder 账号管理页。原有
   WorkBuddy 账号继续使用原来的管理页面。
2. 在「设置」编辑 API Key，勾选 WorkBuddy、Qoder，或同时勾选两者。
   旧 Key 默认仅允许 WorkBuddy；不会自动扩大任何已有 Key 的平台权限。
3. 客户端继续使用同一个 `/v1` 地址和配置好的 Key，模型名称不需要加平台前缀。
   可选用 `workbuddy/<模型>` 或 `qoder/<模型>` 显式指定平台，仍受 Key 权限约束。
4. Key 的国内 / 国际区域约束对两个平台都生效。设置允许账号时，WorkBuddy 沿用
   原 UID，Qoder 使用 `qoder/<UID>`，避免两个平台的账号标识冲突。

Enable Qoder under Platform management, authorize an account, then select one
or both platforms when editing a key. Clients keep their existing base URL.
Existing keys remain WorkBuddy-only. Explicit provider prefixes cannot bypass
the key's platform, model or account scope.

## 调度语义 / Routing semantics

- 仅在提供同一个模型的候选平台和区域之间选择，不把 Claude 静默换成其他模型。
  忽略名称中的大小写及标点匹配同名模型，并识别目录中的别名。
- 新会话按每个平台、区域的可用账号数加权随机选择；平台内部继续使用原账号调度器。
- 会话亲和按 Key、模型、协议以及会话 ID（缺失时使用首轮消息的散列）隔离，默认保留
  一小时。最多缓存 4096 项，不保存消息内容；重启后会重新选择。
- 无可用账号、模型不匹配、冷却或禁止的平台不会入选。临时失败在响应头发出前可换
  另一个候选；输出开始后不重放。内容拒绝、鉴权和参数错误不跨平台重试。
- Responses 的 `previous_response_id` 统一在本地返回 400，避免 Qoder 静默忽略它导致
  上下文丢失；客户端应发送完整对话历史。这与原 WorkBuddy 接口的无状态约定一致。
- Qoder 支持自身的 Anthropic 转换入口；WorkBuddy 的 Anthropic 入口继续受原来的显式
  启用开关约束。网关响应头 `X-Gateway-Platform` 标明实际处理平台。

New sessions choose a provider/realm with account-count weights. Existing
sessions prefer their original provider. Only transient failures before response
headers may fail over. No retry occurs after output starts, nor for policy,
authentication or validation failures. Send full history for portable sessions;
`previous_response_id` is rejected locally instead of silently dropping context.

## 代理与数据 / Proxies and state

Qoder 使用独立的本机子进程，只监听随机回环端口。所有入口需要网关内部随机密钥，
对外继续使用原面板密码和 API Key。客户端 Key 不会传给 Qoder 子进程。

「平台管理」中可把 Qoder 账号绑定到现有代理槽。已绑定的槽不存在或停用时，账号
请求失败，不回退直连。未绑定账号沿用 Qoder 的环境变量代理配置。OAuth 发起时账号
尚未建立，授权阶段使用环境变量出口；添加账号后再绑定。

Qoder state lives under `<accounts-dir>/qoder-state/`, including its independent
`accounts/`, `usage/`, worker log and proxy metadata. Persist the entire parent
accounts directory. These files are ignored by Git and never belong in commits.
Inference, refresh, quota and check-in requests inherit an account's proxy slot.
OAuth initialization uses the worker's environment proxy until an account exists.

额度与用量的权威数据仍按平台分别记录：WorkBuddy 在原数据看板，Qoder 在内嵌管理页。
两家积分不相加，也不视为同一种计费单位。

## Qoder 积分与任务 / Qoder credits and tasks

网关的签到按钮只能领取上游为当前账号开放的活动，不能代做官方桌面端任务。
活动列表里的「查看详情」或套餐促销不代表有积分可领；Pro 福利包也不是 Free
账号的固定赠送额度。未领取或当前不可领取与「已经到账」含义不同。

截至 2026-10-27，国际版[每日 100 Credits](https://docs.qoder.com/events/100credits)
需要在官方 Qoder 桌面端手动领取，每日 10:00（UTC+8）开启新一轮，领取后 30 天
有效。仅通过网关 OAuth 添加账号不等于完成桌面端领取或获得新人试用权益。

[DeepSeek-Flash 限免](https://docs.qoder.com/events/deepseek-flash-free)与每日积分
是两项活动：当前限免至 2026-10-31 24:00（UTC+8），有每日免费次数限制，Free / 零
积分账号也可使用。调用模型不会增加积分余额。当前目录的模型名为 `DeepSeek-Flash`；
可用 `qoder/DeepSeek-Flash` 指定 Qoder，前提是 Key 勾选 Qoder 并允许国际区。
模型、活动与资格始终以官方当前返回为准，网关不保证永久限免。

Check-in only claims rewards made available to the account; it does not complete
desktop tasks. Detail links and subscription promotions are not claimable credit
rewards, and a Pro pack is not a guaranteed Free-plan benefit. As of 2026-10-27,
the daily 100-credit promotion requires claiming in the official desktop app.
DeepSeek-Flash has a separate daily free-use allowance through October 31, even
with zero credits; using it does not increase the credit balance. Request
`qoder/DeepSeek-Flash` with a key that permits Qoder and the international realm.
Availability and eligibility follow the current official rules.

## 更新与回滚 / Updates and rollback

迁移分支应从自己的 GitHub fork 更新，勿直接用上游官方发行包覆盖个人扩展。
关闭平台页的 Qoder 开关并把 Key 改成只允许 WorkBuddy，即可停止混合调度。
回退代码前先备份整个账号目录；Qoder 凭证与用量无需删除。

Update this personal fork rather than overwriting it with an upstream release.
Set `WB_UPDATE_REPO` to your own `owner/repository` when publishing private-fork
releases; source checkouts can use their personal Git remote directly.
To disable integration, stop Qoder and restrict keys to WorkBuddy. Preserve all
account state when rolling code back.
