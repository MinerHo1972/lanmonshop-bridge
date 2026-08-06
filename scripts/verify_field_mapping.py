"""
验证脚本：拉兰盟 3 单 → 新字段映射构建 JKY 请求 → 提交 → 检查 API 日志

用法: scp 到 ECS → cd /opt/lanmonshop-bridge && 
  LANMONSHOP_DB_PATH=/root/.hermes/data/lanmonshop-bridge.db \
  SETTINGS_PATH=/opt/lanmonshop-bridge/config/settings.yaml \
  CREDENTIALS_PATH=/root/.hermes/data/credentials.yaml \
  python3.11 scripts/verify_field_mapping.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lanmeng_bridge.config import load_settings
from lanmeng_bridge.clients.lanmonshop import create_lanmong_client
from lanmeng_bridge.clients.jky import create_jky_client
from lanmeng_bridge.storage.db import get_connection


ORDER_NOS = [
    "FYY202607031150372",
    "FYY202607031152273",
    "FYY202607031155554",
]


async def main():
    settings = load_settings()
    lanmong = create_lanmong_client(settings)
    jky = create_jky_client(settings)
    conn = get_connection()
    now = datetime.now()

    # 预加载产品缓存
    print(">>> 加载产品缓存...")
    cache = {}
    rows = conn.execute(
        "SELECT jky_goods_no, jky_barcode, jky_goods_name FROM jky_product_cache"
    ).fetchall()
    for r in rows:
        cache[r["jky_goods_no"]] = {
            "barcode": r["jky_barcode"],
            "name": r["jky_goods_name"],
        }
    print(f"    缓存 {len(cache)} 条")

    for order_no in ORDER_NOS:
        print(f"\n{'='*80}")
        print(f"处理订单: {order_no}")
        print(f"{'='*80}")

        # ---- Step 1: 从蓝盟拉取 ----
        print(f"\n[Step 1] 拉取蓝盟订单...")
        resp = await lanmong.get_deliver_orders(
            order_no=order_no,
            state="",  # 不限状态
            page_num=1, page_size=10,
        )
        print(f"  蓝盟响应 code={resp.get('code')}")
        if resp.get("code") != 0:
            print(f"  失败: {resp}")
            continue

        data = resp.get("data", {})
        orders = (
            data.get("orderList", [])
            if isinstance(data, dict)
            else (data if isinstance(data, list) else [])
        )
        if not orders:
            print(f"  未找到订单")
            continue

        order = orders[0]
        print(f"  蓝盟原始数据:")
        print(f"    orderNo={order.get('orderNo')} state={order.get('state')}")
        print(f"    name={order.get('name','')} mobile={order.get('mobile','')}")
        print(f"    province={order.get('province','')} city={order.get('city','')}")
        print(f"    district={order.get('district','')} address={order.get('address','')}")
        print(f"    remark={order.get('remark','')} expressPrice={order.get('expressPrice')}")

        # 商品明细
        products = order.get("orderProducts", [])
        order_total = 0.0
        trade_order_details = []
        for i, item in enumerate(products):
            product_no = item.get("productNo", "")
            cost_price = float(item.get("costPrice", 0) or 0)
            qty = item.get("num") or item.get("number") or 1
            sell_total = round(cost_price * qty, 2)
            order_total += sell_total

            prod_cache = cache.get(product_no, {})
            barcode = prod_cache.get("barcode", "")
            goods_name = prod_cache.get("name", item.get("productName", ""))

            print(f"    商品[{i}]: productNo={product_no} costPrice={cost_price} "
                  f"num={qty} → sellTotal={sell_total} barcode={barcode}")

            if not barcode:
                print(f"    ❌ 条码为空! 无法向 JKY 提交")
                trade_order_details = []
                break

            trade_order_details.append({
                "goodsNo": product_no,
                "barcode": barcode,
                "goodsName": goods_name,
                "specName": "默认",
                "unit": "件",
                "sellPrice": cost_price,
                "sellCount": qty,
                "sellTotal": sell_total,
            })

        if not trade_order_details:
            print(f"  跳过: 商品数据不完整")
            continue

        # ---- Step 2: 构建 JKY 请求 ----
        print(f"\n[Step 2] 构建 JKY trade.create 请求体...")
        receiver_mobile = order.get("mobile", "")
        create_biz = {
            "tradeOrder": {
                "onlineTradeNo": order_no,
                "shopName": "特渠分销对接",
                "shopCode": "0125",
                "warehouseCode": "02",
                "tradeTime": now.strftime("%Y-%m-%d %H:%M:%S"),
                "tradeType": 1,
                "totalFee": order_total,
                "payment": order_total,
                "chargeCurrency": "人民币",
                "receiverName": order.get("name", ""),
                "mobile": receiver_mobile,
                "phone": receiver_mobile,
                "state": order.get("province", ""),
                "city": order.get("city", ""),
                "district": order.get("district", ""),
                "address": order.get("address", ""),
                "logisticCode": "STO",
                "logisticName": "申通快递",
                "logisticType": 1,
                "payStatus": 9,
                "chargeType": 3,
                "customerName": "上海逸享云创电子商务有限公司",
                "customerAccount": "C202606231285",
                "buyerMemo": order.get("remark", ""),
                "tradeOrderDetails": trade_order_details,
            }
        }
        print(f"  JKY 请求体 (截断):")
        req_json = json.dumps(create_biz, ensure_ascii=False)
        print(f"  {req_json[:600]}...")
        print(f"  总金额: totalFee={order_total} payment={order_total}")

        # ---- Step 3: 提交 JKY ----
        print(f"\n[Step 3] 提交 JKY trade_create...")
        try:
            create_resp = await jky.trade_create(create_biz)
            jky_code = create_resp.get("code", -1)
            jky_msg = create_resp.get("msg", "")
            print(f"  JKY 响应: code={jky_code} msg={jky_msg}")
            print(f"  subCode={create_resp.get('subCode','')}")
            print(f"  完整响应: {json.dumps(create_resp, ensure_ascii=False, indent=2)}")

            if "重复" in jky_msg or "已存在" in jky_msg:
                print(f"\n  ✅ 正确: 错误是 '网店订单重复'，字段映射正确")
            elif jky_code == 200:
                print(f"\n  ⚠️ 创建成功? 但订单已在 JKY 存在，按理应报重复")
            else:
                print(f"\n  ⚠️ 其他错误: 非重复错误，检查字段映射")
        except Exception as e:
            print(f"  ❌ 提交异常: {e}")

    await lanmong.close()
    await jky.close()

    # ---- Step 4: 检查 API 日志 ----
    print(f"\n{'='*80}")
    print(f"[Step 4] 检查 API 调用日志 (最近 30 条)")
    print(f"{'='*80}")
    log_rows = conn.execute(
        "SELECT id, source, method, request_body, response_body, "
        "       api_code, error, created_at "
        "FROM api_call_log "
        "WHERE source IN ('jky_direct', 'jky') AND method = 'trade_create' "
        "ORDER BY id DESC LIMIT 30"
    ).fetchall()
    print(f"  找到 {len(log_rows)} 条 JKY trade_create 日志:")
    for r in log_rows:
        print(f"\n  --- 日志 id={r['id']} ({r['created_at']}) ---")
        print(f"  source={r['source']} method={r['method']} api_code={r['api_code']}")
        req = r['request_body']
        if req:
            req_trunc = str(req)[:1000]
            print(f"  request_body: {req_trunc}")
        resp = r['response_body']
        if resp:
            resp_trunc = str(resp)[:800]
            print(f"  response_body: {resp_trunc}")
        if r['error']:
            print(f"  error: {r['error']}")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
