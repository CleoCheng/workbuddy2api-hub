"""Unit tests for account rolling rest, safety floor, free model isolation,
cross-slot dispersion, risk control quarantine and restriction probe backoff.

Run with:
    python tests/_test_rolling_rest.py
"""
import copy
import os
import sys
import tempfile
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import wb_accounts
import wb_proxy_health
import wb_settings


class RollingRestSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.accounts_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_settings_and_validation(self):
        # 1. 默认设置读取
        cfg = wb_settings.rolling_rest_config(self.accounts_dir)
        self.assertEqual(cfg, {"enabled": True, "minutes": 60, "count": 3})

        # 正确持久化更新
        updated = wb_settings.set_rolling_rest_config(
            self.accounts_dir, enabled=False, minutes=30, count=5
        )
        self.assertEqual(updated, {"enabled": False, "minutes": 30, "count": 5})
        reloaded = wb_settings.rolling_rest_config(self.accounts_dir)
        self.assertEqual(reloaded, {"enabled": False, "minutes": 30, "count": 5})

        # 越界值校验：minutes < 5 或 minutes > 1440 抛出 ValueError
        with self.assertRaises(ValueError):
            wb_settings.set_rolling_rest_config(self.accounts_dir, minutes=4)
        with self.assertRaises(ValueError):
            wb_settings.set_rolling_rest_config(self.accounts_dir, minutes=1441)

        # 越界值校验：count < 1 或 count > 20 抛出 ValueError
        with self.assertRaises(ValueError):
            wb_settings.set_rolling_rest_config(self.accounts_dir, count=0)
        with self.assertRaises(ValueError):
            wb_settings.set_rolling_rest_config(self.accounts_dir, count=21)

        # 非法布尔值校验
        with self.assertRaises(ValueError):
            wb_settings.set_rolling_rest_config(self.accounts_dir, enabled="invalid")


class FreeModelExpiringWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.accounts_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_free_model_skips_expiring_window(self):
        pool = wb_accounts.AccountPool(self.accounts_dir)
        acc = wb_accounts.Account({
            "uid": "exp_acc",
            "accessToken": "header.payload.sig",
            "expiresAt": time.time() + 3600,
            "enabled": True,
            "credits": {
                "packages": [{
                    "days_left": 1.5,
                    "remain": 50.0,
                    "package_code": "standard",
                }]
            }
        })
        acc.expiring_window_days = 3
        self.assertTrue(acc.in_expiring_window())

        # 付费模型优先分流到临期账号
        picked_paid = pool._pick_expiring_first([acc], exclude=set(), model="claude-3-5-sonnet")
        self.assertIs(picked_paid, acc)

        # 免费模型（如 hy4free）直接跳过临期加权，返回 None，避免打爆临期账号
        picked_free = pool._pick_expiring_first([acc], exclude=set(), model="hy4free")
        self.assertIsNone(picked_free)


class SlotDispersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.accounts_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_cross_slot_dispersion(self):
        pool = wb_accounts.AccountPool(self.accounts_dir)
        now = time.time()
        a1 = wb_accounts.Account({
            "uid": "acc_slot_a",
            "accessToken": "tok_a",
            "expiresAt": now + 3600,
            "enabled": True,
            "proxySlot": "slot-A"
        })
        a2 = wb_accounts.Account({
            "uid": "acc_slot_b",
            "accessToken": "tok_b",
            "expiresAt": now + 3600,
            "enabled": True,
            "proxySlot": "slot-B"
        })
        pool.accounts = [a1, a2]

        first = pool.pick()
        second = pool.pick()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        # 连续两次选择应打散到不同 slot
        self.assertNotEqual(first.proxy_slot, second.proxy_slot)


class RollingRestSafetyFloorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.accounts_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_rolling_rest_logic_and_safety_floor(self):
        pool = wb_accounts.AccountPool(self.accounts_dir)
        pool.dir = self.accounts_dir
        now = time.time()

        # 配置：启用轮休，最多休眠 3 个，每轮 60 分钟
        wb_settings.set_rolling_rest_config(
            self.accounts_dir, enabled=True, minutes=60, count=3
        )

        # 1. 6 个账号：允许最多 3 个休眠
        accs_six = [
            wb_accounts.Account({
                "uid": f"acc_6_{i}",
                "accessToken": f"tok_6_{i}",
                "expiresAt": now + 3600,
                "enabled": True
            })
            for i in range(6)
        ]
        pool.accounts = accs_six
        pool.update_rolling_rest(self.accounts_dir)
        resting_six = [a for a in accs_six if a.is_resting()]
        self.assertEqual(len(resting_six), 3)

        # 2. 2 个账号：安全底线保护生效，允许 0 个休眠，号池不断流
        accs_two = [
            wb_accounts.Account({
                "uid": f"acc_2_{i}",
                "accessToken": f"tok_2_{i}",
                "expiresAt": now + 3600,
                "enabled": True
            })
            for i in range(2)
        ]
        pool.accounts = accs_two
        pool.update_rolling_rest(self.accounts_dir)
        resting_two = [a for a in accs_two if a.is_resting()]
        self.assertEqual(len(resting_two), 0)

        # 3. 账号休眠到期后正常唤醒
        resting_acc = accs_six[0]
        resting_acc.resting_until = now - 5
        pool.accounts = [resting_acc]
        pool.update_rolling_rest(self.accounts_dir)
        self.assertFalse(resting_acc.is_resting())
        self.assertEqual(resting_acc.resting_until, 0.0)


class RiskControlQuarantineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.accounts_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_risk_control_reporting_and_quarantine(self):
        hub = types.SimpleNamespace(ACCOUNTS_DIR=self.accounts_dir, log=lambda s: None)
        proxy_url = "http://127.0.0.1:10081"
        wb_settings.set_proxy_slots(self.accounts_dir, [
            {"id": "slot-1", "url": proxy_url, "group": "g1"}
        ])

        mon = wb_proxy_health.Monitor(hub)
        mon.cfg.update({"enabled": True, "groups": {"slot-1": "g1"}, "ranked": True})
        mon.rows["slot-1"] = {
            "id": "slot-1",
            "url": proxy_url,
            "node": "exit-node-1",
            "ip": "198.51.100.1",
            "status": "ok"
        }

        # 上报 11140 风险
        res = mon.report_risk_control(proxy_url, reason="11140")
        self.assertTrue(res)
        self.assertIn("exit-node-1", mon.quarantined_exits)
        self.assertIn("198.51.100.1", mon.quarantined_exits)
        self.assertTrue(mon._is_quarantined("exit-node-1"))
        self.assertTrue(mon._is_quarantined("198.51.100.1"))


class RestrictionProbeBackoffTests(unittest.TestCase):
    def test_restriction_probe_result_handling(self):
        now = time.time()
        acc = wb_accounts.Account({
            "uid": "acc_probe_test",
            "accessToken": "tok_valid",
            "expiresAt": now + 3600,
            "enabled": True
        })
        acc.restricted_until = now + 1000
        self.assertTrue(acc.is_restricted(now))

        # 探测失败：触发指数阶梯退避并延长 restricted_until
        acc.note_restriction_probe_result(False, "11140 account restricted")
        self.assertEqual(acc.restricted_probes, 1)
        self.assertGreater(acc.restricted_until, now + 5000)
        self.assertIn("probe backoff", acc.last_error)

        # 探测再次失败：进一步退避
        prev_until = acc.restricted_until
        acc.note_restriction_probe_result(False, "11140 still restricted")
        self.assertEqual(acc.restricted_probes, 2)
        self.assertGreater(acc.restricted_until, prev_until)

        # 探测成功：清除受限状态恢复入池
        acc.note_restriction_probe_result(True)
        self.assertEqual(acc.restricted_probes, 0)
        self.assertEqual(acc.restricted_until, 0.0)
        self.assertFalse(acc.is_restricted())


class FakeNodeFilterTests(unittest.TestCase):
    def test_fake_node_identification(self):
        fake_cases = [
            "[flower] Expire: 2027-04-09",
            "[flower] 剩余流量: 350GB",
            "官网: https://flower.example.com",
            "[VIP] 到期时间: 2026-12-31",
            "[通知] 请及时更新订阅",
            {"name": "test", "server": "127.0.0.1", "port": 80},
        ]
        for case in fake_cases:
            self.assertTrue(wb_proxy_health.is_fake_node(case), msg=f"Should be fake: {case}")

    def test_real_node_not_misidentified(self):
        real_cases = [
            "[flower] 香港 01",
            "[B] 日本 02 - IEPL",
            "HK-01 | IPLC",
            "US-BGP-02",
            "Singapore 03",
        ]
        for case in real_cases:
            self.assertFalse(wb_proxy_health.is_fake_node(case), msg=f"Should be real: {case}")

    def test_subscription_fake_node_filtering(self):
        import wb_proxy_subscriptions
        self.assertTrue(wb_proxy_subscriptions.is_fake_node("[flower] Expire: 2027-04-09"))
        self.assertFalse(wb_proxy_subscriptions.is_fake_node("[flower] 香港 01"))

        sample = (
            "- name: '[flower] Expire: 2027-04-09'\n"
            "  type: ss\n"
            "  server: 1.2.3.4\n"
            "  port: 443\n"
            "- name: '[flower] 香港 01'\n"
            "  type: ss\n"
            "  server: 1.2.3.4\n"
            "  port: 443\n"
            "- name: 'Localhost'\n"
            "  type: ss\n"
            "  server: 127.0.0.1\n"
            "  port: 443\n"
        ).encode('utf-8')
        if wb_proxy_subscriptions.yaml is not None:
            parsed = wb_proxy_subscriptions._parse_subscription(sample)
            names = [p['name'] for p in parsed]
            self.assertEqual(names, ['[flower] 香港 01'])


if __name__ == "__main__":
    unittest.main()
