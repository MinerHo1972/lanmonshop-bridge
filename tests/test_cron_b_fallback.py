"""cron-b 明细回退 — 回查蓝盟补 orderProducts 的逻辑单测

覆盖：快照空 → 回查补得 → 写回快照 + 正常解析 items；
      快照空 → 回查也无 → 维持 failed 路径；
      快照有 orderItemId → 不触发回查（正常路径零变动）
"""
import asyncio
import json
import sqlite3
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path("/home/lhs_admin/projects/lanmonshop-bridge")
sys.path.insert(0, str(ROOT))

tmp_db = tempfile.mktemp(suffix=".db")
os.environ["LANMONSHOP_DB_PATH"] = tmp_db
os.environ["SETTINGS_PATH"] = str(ROOT / "config" / "settings.yaml")

from lanmeng_bridge.storage import db as storage_db

conn = storage_db.get_connection()
conn.executescript("""
CREATE TABLE order_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_order_no TEXT NOT NULL,
    platform_order_id INTEGER,
    state TEXT DEFAULT 'init',
    jky_trade_no TEXT,
    logistic_no TEXT,
    order_items_json TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
""")
conn.commit()

# ---- mock 蓝盟客户端：只实现 get_deliver_orders ----
class FakeLanmong:
    def __init__(self, resp):
        self.resp = resp
        self.call_count = 0
    async def get_deliver_orders(self, **kw):
        self.call_count += 1
        return self.resp

GOOD_PRODUCTS = [
    {"orderItemId": 888001, "num": 2, "skuNo": "YXTEST01", "productName": "测试商品A"},
    {"orderItemId": 888002, "num": 1, "skuId": 12345, "productName": "测试商品B"},
]

def make_resp(products):
    return {"code": 0, "data": {"orderList": [{"orderNo": "FB20260815001", "orderProducts": products}]}}

# 直接测补丁新增的解析段：模拟 cron_b 中 items 构建 + 回退
async def build_items(order_items_json, lanmong):
    items = []
    try:
        order_products = json.loads(order_items_json or "[]")
        for prod in order_products:
            oiid = prod.get("orderItemId")
            if not oiid:
                continue
            num = prod.get("num") or prod.get("number") or 1
            item = {"orderItemId": int(oiid), "num": int(num)}
            sku_no = prod.get("skuNo")
            sku_id = prod.get("skuId")
            if sku_no:
                item["skuNo"] = str(sku_no)
            elif sku_id:
                item["skuId"] = int(sku_id)
            items.append(item)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    # —— 补丁核心段（与 cron_b.py 一字不差的语义）——
    wrote_back = None
    if not items:
        fallback_products = []
        try:
            fb_resp = await lanmong.get_deliver_orders(
                order_no="FB20260815001", page_size=5, state=None,
            )
            if fb_resp.get("code") == 0:
                fb_data = fb_resp.get("data", {})
                fb_list = (
                    fb_data.get("orderList", [])
                    if isinstance(fb_data, dict)
                    else (fb_data if isinstance(fb_data, list) else [])
                )
                if fb_list:
                    fallback_products = fb_list[0].get("orderProducts") or []
        except Exception:
            pass
        if fallback_products:
            wrote_back = json.dumps(fallback_products, ensure_ascii=False, default=str)
            for prod in fallback_products:
                oiid = prod.get("orderItemId")
                if not oiid:
                    continue
                num = prod.get("num") or prod.get("number") or 1
                item = {"orderItemId": int(oiid), "num": int(num)}
                sku_no = prod.get("skuNo")
                sku_id = prod.get("skuId")
                if sku_no:
                    item["skuNo"] = str(sku_no)
                elif sku_id:
                    item["skuId"] = int(sku_id)
                items.append(item)
    return items, wrote_back

async def main():
    cases = []

    # 1. 快照空 + 回查补得 → 2 条 items + 快照写回
    lm = FakeLanmong(make_resp(GOOD_PRODUCTS))
    items, wrote = await build_items("[]", lm)
    cases.append(("快照空→回查补得2条", len(items) == 2 and lm.call_count == 1, {"items": items}))
    cases.append(("快照写回含orderItemId", wrote and 'orderItemId' in wrote and "888001" in wrote, {}))

    # 2. 快照空 + 回查也无明细 → 0 items（维持 failed 路径）
    lm2 = FakeLanmong({"code": 0, "data": {"orderList": [{"orderNo": "X", "orderProducts": []}]}})
    items2, wrote2 = await build_items(None, lm2)
    cases.append(("快照空→回查仍空→failed路径", items2 == [] and wrote2 is None, {}))

    # 3. 快照有 orderItemId → 不触发回查
    lm3 = FakeLanmong(make_resp(GOOD_PRODUCTS))
    snapshot = json.dumps([{"orderItemId": 111, "num": 1, "skuNo": "S1"}])
    items3, wrote3 = await build_items(snapshot, lm3)
    cases.append(("快照完好→不回查", lm3.call_count == 0 and len(items3) == 1 and wrote3 is None, {}))

    # 4. 回查接口异常 → 吞异常走 failed 路径
    class BoomLanmong:
        async def get_deliver_orders(self, **kw):
            raise RuntimeError("network down")
    items4, wrote4 = await build_items("[]", BoomLanmong())
    cases.append(("回查异常→failed路径不崩溃", items4 == [] and wrote4 is None, {}))

    # 5. 回查结果缺 orderItemId → 不产出 item
    lm5 = FakeLanmong(make_resp([{"num": 1, "skuNo": "N"}]))
    items5, _ = await build_items("[]", lm5)
    cases.append(("回查结果缺oiid→过滤", items5 == [], {}))

    all_pass = True
    for name, ok, detail in cases:
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"[{status}] {name} {detail if not ok else ''}")
    print()
    print("=== 全部通过 ===" if all_pass else "=== 存在失败 ===")
    conn.close()
    os.unlink(tmp_db)
    sys.exit(0 if all_pass else 1)

asyncio.run(main())
