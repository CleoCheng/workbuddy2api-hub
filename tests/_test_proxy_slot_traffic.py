"""Deterministic tests for proxy-slot traffic and token statistics."""

import atexit
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.TemporaryDirectory(prefix="wb-slot-traffic-")
atexit.register(_TMP.cleanup)

import wb_slot_traffic as ST

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


print("[1] DB initialization and schema creation")
usage_dir = os.path.join(_TMP.name, "usage")
os.makedirs(usage_dir, exist_ok=True)
db_path = ST.init(usage_dir)
check("db path returned", bool(db_path and os.path.exists(db_path)))

print("[2] Record single and multiple entries in the same hour")
# 2026-11-02 10:15:00 UTC
t1 = 1793614500.0
ST.record(usage_dir, "slot-1", prompt_tokens=1000, completion_tokens=200, total_tokens=1200,
          bytes_in=4000, bytes_out=1500, ts=t1)
ST.record(usage_dir, "slot-1", prompt_tokens=500, completion_tokens=100, total_tokens=600,
          bytes_in=2000, bytes_out=800, ts=t1 + 60)

res_all = ST.query(usage_dir, interval="all")
s1 = res_all.get("totals", {}).get("slot-1", {})
check("slot-1 requests accumulated", s1.get("requests") == 2, s1)
check("slot-1 prompt_tokens accumulated", s1.get("prompt_tokens") == 1500, s1)
check("slot-1 completion_tokens accumulated", s1.get("completion_tokens") == 300, s1)
check("slot-1 total_tokens accumulated", s1.get("total_tokens") == 1800, s1)
check("slot-1 bytes_in accumulated", s1.get("bytes_in") == 6000, s1)
check("slot-1 bytes_out accumulated", s1.get("bytes_out") == 2300, s1)
check("slot-1 bytes_total calculated", s1.get("bytes_total") == 8300, s1)

print("[3] Multiple hours, days, and months aggregation")
# Add slot-2 in the same hour
ST.record(usage_dir, "slot-2", prompt_tokens=800, completion_tokens=150, total_tokens=950,
          bytes_in=3000, bytes_out=1200, ts=t1)
# Add slot-1 in a different hour on the same day (+3 hours)
ST.record(usage_dir, "slot-1", prompt_tokens=2000, completion_tokens=400, total_tokens=2400,
          bytes_in=8000, bytes_out=3000, ts=t1 + 10800)
# Add slot-1 on a different day in a different month (2026-12-01)
t2 = t1 + 30 * 86400
ST.record(usage_dir, "slot-1", prompt_tokens=3000, completion_tokens=500, total_tokens=3500,
          bytes_in=10000, bytes_out=4000, ts=t2)

# Query by interval="day"
res_day = ST.query(usage_dir, interval="day")
check("interval=day has series", len(res_day.get("series", [])) >= 2, len(res_day.get("series", [])))
d1_period = time.strftime("%Y-%m-%d", time.localtime(t1))
d1_item = next((item for item in res_day["series"] if item["period"] == d1_period), None)
check("day series includes t1 period", d1_item is not None)
if d1_item:
    check("day series slot-1 tokens sum", d1_item["slots"].get("slot-1", {}).get("total_tokens") == 4200)
    check("day series slot-2 tokens sum", d1_item["slots"].get("slot-2", {}).get("total_tokens") == 950)

# Query by interval="month"
res_month = ST.query(usage_dir, interval="month")
check("interval=month has series", len(res_month.get("series", [])) >= 2)
m1_period = time.strftime("%Y-%m", time.localtime(t1))
m2_period = time.strftime("%Y-%m", time.localtime(t2))
m1_item = next((item for item in res_month["series"] if item["period"] == m1_period), None)
m2_item = next((item for item in res_month["series"] if item["period"] == m2_period), None)
check("month series includes first month", m1_item is not None)
check("month series includes second month", m2_item is not None)
if m1_item:
    check("first month slot-1 total_tokens", m1_item["slots"].get("slot-1", {}).get("total_tokens") == 4200)
if m2_item:
    check("second month slot-1 total_tokens", m2_item["slots"].get("slot-1", {}).get("total_tokens") == 3500)

# Query single slot filter
res_slot1 = ST.query(usage_dir, interval="all", slot_id="slot-1")
check("filtered query totals only slot-1", list(res_slot1.get("totals", {}).keys()) == ["slot-1"])
check("slot-1 grand total tokens", res_slot1["totals"]["slot-1"]["total_tokens"] == 7700)

print("[4] Historical backfill from usage.jsonl")
accounts_dir = os.path.join(_TMP.name, "accounts")
os.makedirs(accounts_dir, exist_ok=True)
# Account 1 bound to slot-9
with open(os.path.join(accounts_dir, "acct-1.json"), "w", encoding="utf-8") as f:
    json.dump({"uid": "acct-1", "proxySlot": "slot-9"}, f)
# Account 2 bound to slot-10
with open(os.path.join(accounts_dir, "acct-2.json"), "w", encoding="utf-8") as f:
    json.dump({"uid": "acct-2", "proxySlot": "slot-10"}, f)
# Account 3 has no slot
with open(os.path.join(accounts_dir, "acct-3.json"), "w", encoding="utf-8") as f:
    json.dump({"uid": "acct-3", "proxySlot": ""}, f)

# Write mock usage.jsonl
mock_usage = os.path.join(usage_dir, "usage.jsonl")
lines = [
    json.dumps({"at": t1, "account": "acct-1", "prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}),
    json.dumps({"at": t1 + 300, "account": "acct-1", "prompt_tokens": 2000, "completion_tokens": 200, "total_tokens": 2200}),
    json.dumps({"at": t1 + 600, "account": "acct-2", "prompt_tokens": 500, "completion_tokens": 50, "total_tokens": 550}),
    json.dumps({"at": t1 + 900, "account": "acct-3", "prompt_tokens": 9999, "completion_tokens": 999, "total_tokens": 10998}),
]
with open(mock_usage, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")

backfilled = ST.backfill_from_usage(usage_dir, accounts_dir, force=True)
check("backfilled count matches slotted accounts", backfilled == 3, backfilled)

res_backfill = ST.query(usage_dir, interval="all", slot_id="slot-9")
s9 = res_backfill.get("totals", {}).get("slot-9", {})
check("slot-9 requests backfilled", s9.get("requests") == 2, s9)
check("slot-9 tokens backfilled", s9.get("total_tokens") == 3300, s9)
# Estimated bytes check: prompt_tokens * 3.5 + 400 per req, completion * 4.0 + 300 per req
expected_in = int(3000 * 3.5 + 400 * 2)
expected_out = int(300 * 4.0 + 300 * 2)
check("slot-9 estimated bytes_in", s9.get("bytes_in") == expected_in, (s9.get("bytes_in"), expected_in))
check("slot-9 estimated bytes_out", s9.get("bytes_out") == expected_out, (s9.get("bytes_out"), expected_out))

# Idempotency check: run backfill again without force
backfilled_again = ST.backfill_from_usage(usage_dir, accounts_dir, force=False)
check("second backfill skipped (idempotent)", backfilled_again == 0, backfilled_again)
res_after = ST.query(usage_dir, interval="all", slot_id="slot-9")
check("totals did not double count", res_after["totals"]["slot-9"]["total_tokens"] == 3300)

print("[5] wb_proxy proxy_slots_view and /proxy/slots/traffic integration")
import wb_proxy
import wb_settings

# Configure USAGE_DIR and ACCOUNTS_DIR in wb_proxy
wb_proxy.USAGE_DIR = usage_dir
wb_proxy.ACCOUNTS_DIR = accounts_dir
wb_settings.set_proxy_slots(accounts_dir, [
    {"id": "slot-9", "name": "Slot Nine", "url": "http://127.0.0.1:21009"},
    {"id": "slot-10", "name": "Slot Ten", "url": "http://127.0.0.1:21010"},
])

views = wb_proxy.proxy_slots_view()
s9_view = next((v for v in views if v["id"] == "slot-9"), None)
check("proxy_slots_view includes stats", s9_view is not None and "stats" in s9_view)
if s9_view:
    check("slot-9 view stats total_tokens matches backfill", s9_view["stats"]["total_tokens"] == 3300)
    check("slot-9 view stats bytes_total matches backfill", s9_view["stats"]["bytes_total"] > 0)

# Simulate Handler call to _get_proxy_slot_traffic
class DummyHandler:
    def __init__(self):
        self._sent_status = None
        self._sent_json = None
    def _json(self, status, payload):
        self._sent_status = status
        self._sent_json = payload
        return payload
    def _panel_ok(self):
        return True
    _get_proxy_slot_traffic = wb_proxy.Handler._get_proxy_slot_traffic

handler = DummyHandler()
ret = handler._get_proxy_slot_traffic({"interval": ["day"]})
check("Handler _get_proxy_slot_traffic status 200", handler._sent_status == 200)
check("Handler _get_proxy_slot_traffic interval is day", ret.get("interval") == "day")
check("Handler _get_proxy_slot_traffic includes slot-9 in totals", "slot-9" in ret.get("totals", {}))

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
ST.close()
sys.exit(1 if FAIL else 0)
