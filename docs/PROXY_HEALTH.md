# 代理健康检查与自动切换 / Proxy health and failover

此功能默认关闭，在「设置 → 账号代理槽 → 代理健康检查与自动切换」启用。
适用于 Mihomo 的固定代理端口和手动选择组；账号始终绑定槽位，切换只改变组内节点。
WorkBuddy 和 Qoder 共用健康状态。未绑定代理的账号继续原有行为。

Disabled by default. Enable it under Settings → Account proxy slots → Proxy health
checks and failover. Accounts stay bound to fixed proxy listeners; only the node
inside a Mihomo selector changes. WorkBuddy and Qoder share health decisions.
Unbound accounts keep their existing behavior.

## 配置 / Configuration

1. Mihomo 控制接口只监听回环地址，准备独立的 `select` 探测组及 HTTP/mixed
   监听端口（例如 17990）。探测组引用现有订阅，不能供账号使用。
2. 将控制接口的 secret 保存为 `<accounts-dir>/proxy-health/secret`，限制文件权限；
   不将该文件提交到 Git。面板只显示是否已配置，不返回其内容。
3. 在面板填写控制地址、独立探测地址/组名，以及槽位 ID 到生产代理组的映射。
   每个槽位须对应不同组；探测组与生产组也须不同。生产组与探测组均须是 `select`。
4. 建议初始值：每 90 秒检查，连续失败 3 次，探测超时 5 秒，切换冷却 600 秒，
   每轮最多检查 12 个备用节点。默认只允许同国家切换，国家未知时不自动跨国。
5. 启用后点击「立即检查代理」。修改配置时若有检查正在运行，等待结束后重试。

Prepare a loopback-only controller and a dedicated probe selector/listener using
the existing subscriptions. Keep its secret in the private file described above.
Map each slot ID to its own production selector. The probe selector must be
distinct and must never be assigned to an account. Suggested defaults: 90-second
checks, three consecutive failures, five-second timeouts, a ten-minute switch
cooldown and at most 12 candidate checks per round. Cross-country changes are
off by default; unknown geography does not authorize a country change.

## 判定与切换 / Health decisions and switching

- 定时检查的是连接、出口 IP 和小响应延迟，不下载大文件测带宽，也不调用模型。
- 连接超时、连接重置、DNS 失败、异常 EOF 及 HTTP 502/503/504 触发异步复检。
  一个代理地址 20 秒内只接收一次请求故障信号，不阻塞模型请求执行探测。
  401/403、429、额度不足、模型业务错误和证书验证错误不会被直接视为代理故障。
- 达到阈值后还须通过第二个独立 IP 查询地址复核。任一地址成功即避免本轮换节点；
  上游模型超时而出口探测正常时保留原节点。
- 单一后台检查器串行占用探测端口、分配备用。通过该端口实测候选 IP，排除其它
  槽位的 IP 与已选节点。停用但仍配置的槽位也保留其出口占用。其它出口未知时
  不宣称候选唯一。IP 会变化，这是一轮观测的保证，不是供应商永久独享承诺。
- 切换前重新读取配置和选择，检测手动修改；切换后复测生产端口。失败则尝试回滚，
  未能确认选择状态时保持槽位不可用。控制器不可用、订阅无可用备用或冷却期未过
  时，不回退直连，也不强行与其它槽位共用 IP。
- 新连接使用新出口。WorkBuddy 的连接池按出口代次隔离，旧的在途流继续完成，
  不会强制断开，也不会把旧出口连接用于后续新请求。
- 本功能不重放模型请求。网关现有重试仍受协议与输出边界限制，已输出内容不重放。

Checks measure small-response connectivity and actual exit IP, not bandwidth or
model performance. Request transport errors only enqueue a debounced recheck.
Authentication, policy, quota and rate-limit responses are separate concerns.
After the failure threshold, a second endpoint must confirm the outage. One
controller serializes spare probing and commits, rejects occupied IPs, preserves
disabled slots' reservations and reconciles manual selector changes. A failed
post-switch probe triggers rollback. Missing spares/controller access leave a
slot unavailable rather than silently routing directly. New connections follow
the new exit generation while existing responses finish. This feature never
replays inference requests.

## 状态、恢复与回滚 / State, recovery and rollback

代理槽列表的「延迟」列复用最近探测结果：≤300 ms 绿色、301–800 ms 黄色、
>800 ms 红色。连接异常和不可用时不沿用上次成功延迟；超过三个检查周期
（至少 120 秒）的结果置灰显示已过期，悬停显示探测时间。此值包含代理连接和
小型 HTTPS 响应耗时，不是 ICMP ping、带宽或模型响应速度。手动测试结果也会
立即更新；页面重载后由服务器持久化的健康状态恢复显示。

The slot list colors recent HTTPS probe latency green at ≤300 ms, amber at
301–800 ms and red above 800 ms. Failures replace the old latency with an explicit
status. Results older than three check intervals (at least 120 seconds) become
gray/stale; hover to see the probe timestamp. This includes proxy setup and a
tiny HTTPS response, not ICMP ping, bandwidth or model response time. Manual
tests update immediately; reloading restores server-persisted monitor state.

私有状态、最多 100 条切换记录与 Qoder 故障信号保存在
`<accounts-dir>/proxy-health/`。进程重启后恢复故障槽位和出口代次，首次检查重新确认。
停用功能会恢复原代理调度行为，但不会将 Mihomo 自动切回旧节点。删除/禁用槽位仍
保留项目原有的手工操作语义；自动故障处理不会删除、禁用或解绑账号。

State and bounded history live beside the private secret. Restart restores known
outages until fresh checks succeed. Disabling monitoring restores legacy dispatch
without changing selected Mihomo nodes. Manual slot deletion/disable retains the
project's original semantics; automated failover never unbinds accounts.
