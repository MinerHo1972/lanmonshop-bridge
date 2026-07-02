"""Admin dashboard — cron status, API log viewer & reconciliation (对账)"""

import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from .storage.db import get_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bridge Admin</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#c9d1d9;padding:20px}
  h1{color:#58a6ff;margin-bottom:8px;font-size:22px}
  h2{color:#f0f6fc;font-size:16px;margin:20px 0 10px;border-bottom:1px solid #30363d;padding-bottom:6px}
  .desc{color:#8b949e;font-size:13px;margin-bottom:20px}
  .tab-bar{display:flex;gap:4px;margin-bottom:16px}
  .tab{background:#21262d;border:1px solid #30363d;color:#8b949e;padding:8px 18px;border-radius:6px 6px 0 0;cursor:pointer;font-size:13px}
  .tab.active{background:#161b22;color:#f0f6fc;border-bottom-color:#161b22}
  .panel{display:none}
  .panel.active{display:block}
  table{width:100%;border-collapse:collapse;font-size:12px}
  th,td{padding:6px 10px;text-align:left;border-bottom:1px solid #21262d;white-space:nowrap}
  th{background:#161b22;color:#8b949e;font-weight:600;position:sticky;top:0}
  tr:hover{background:#1c2128}
  tr.selected{background:#1a2332}
  .badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:500}
  .badge-ok{background:#1b4332;color:#3fb950}
  .badge-err{background:#4d1a1a;color:#f85149}
  .badge-warn{background:#4d351a;color:#d29922}
  .badge-init{background:#1a3a4d;color:#58a6ff}
  .code{font-family:'SF Mono','Cascadia Code',monospace;font-size:11px;color:#7ee787;max-width:300px;overflow:hidden;text-overflow:ellipsis}
  .filters{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
  .filters select,.filters input{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:4px 10px;border-radius:4px;font-size:12px}
  .filters label{font-size:12px;color:#8b949e}
  .pagination{display:flex;justify-content:space-between;align-items:center;margin-top:10px;font-size:12px;color:#8b949e}
  .pagination button{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:12px}
  .pagination button:disabled{opacity:.4;cursor:default}
  .pagination button:hover:not(:disabled){background:#30363d}
  .stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:16px}
  .stat-card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:12px}
  .stat-card .num{font-size:24px;font-weight:600;color:#58a6ff}
  .stat-card .label{font-size:11px;color:#8b949e;margin-top:4px}
  .stat-card .red{color:#f85149}
  .stat-card .yellow{color:#d29922}
  .ttl{color:#8b949e;font-size:11px}
  .empty{color:#8b949e;font-style:italic;padding:20px;text-align:center}
  .expand-btn{background:none;border:none;color:#58a6ff;cursor:pointer;font-size:11px;text-decoration:underline}
  .modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);z-index:1000}
  .modal.show{display:flex;align-items:center;justify-content:center}
  .modal-content{background:#161b22;border:1px solid #30363d;border-radius:8px;max-width:800px;width:90%;max-height:80vh;overflow:auto;padding:20px}
  .modal-content pre{background:#0d1117;padding:12px;border-radius:4px;overflow:auto;font-size:11px;max-height:50vh;color:#7ee787}
  .modal-content .close{float:right;cursor:pointer;color:#8b949e;font-size:20px}
  .action-bar{display:flex;gap:10px;margin:10px 0;align-items:center}
  .action-bar button{background:#238636;border:none;color:#fff;padding:6px 16px;border-radius:4px;cursor:pointer;font-size:12px;font-weight:500}
  .action-bar button:disabled{opacity:.4;cursor:default}
  .action-bar .count{color:#8b949e;font-size:12px}
  .result-msg{padding:8px 14px;border-radius:4px;margin:10px 0;font-size:12px}
  .result-msg.ok{background:#1b4332;border:1px solid #3fb950;color:#3fb950}
  .result-msg.err{background:#4d1a1a;border:1px solid #f85149;color:#f85149}
  input[type=checkbox]{width:14px;height:14px;cursor:pointer;accent-color:#238636}
</style>
</head>
<body>
<h1>🔧 Bridge Admin</h1>
<p class="desc">蓝盟-吉客云云桥接服务 · 运行状态 & API 日志查询 & 三态对账</p>

<div class="tab-bar">
  <div class="tab active" onclick="switchTab('crons')">📊 Cron 状态</div>
  <div class="tab" onclick="switchTab('logs')">📝 API 日志</div>
  <div class="tab" onclick="switchTab('recon')">🔄 对账</div>
</div>

<div id="panel-crons" class="panel active">
  <div class="stat-grid" id="cron-stats"></div>
  <h2>Cron 游标</h2>
  <table><thead><tr><th>Key</th><th>值</th><th>更新于</th></tr></thead>
  <tbody id="cron-cursors"></tbody></table>
  <h2>最近 API 调用</h2>
  <table><thead><tr><th>来源</th><th>方法</th><th>状态</th><th>业务码</th><th>耗时</th><th>时间</th></tr></thead>
  <tbody id="cron-recent"></tbody></table>
</div>

<div id="panel-logs" class="panel">
  <div class="filters">
    <label>来源 <select id="f-source"><option value="">全部</option><option>lanmong</option><option>jky_gateway</option><option>jky_direct</option></select></label>
    <label>方法 <input id="f-method" placeholder="method 关键词" style="width:140px"></label>
    <label>结果 <select id="f-status"><option value="">全部</option><option value="ok">成功</option><option value="err">失败</option></select></label>
    <label>每页 <select id="f-size"><option>20</option><option selected>50</option><option>100</option></select></label>
    <button onclick="loadLogs(1)" style="background:#238636;border:none;color:#fff;padding:4px 14px;border-radius:4px;cursor:pointer;font-size:12px">查询</button>
  </div>
  <table><thead><tr><th>ID</th><th>来源</th><th>方法</th><th>HTTP</th><th>业务码</th><th>耗时</th><th>请求体</th><th>响应体</th><th>时间</th></tr></thead>
  <tbody id="log-rows"></tbody></table>
  <div class="pagination"><span id="log-info"></span><div><button id="log-prev" onclick="loadLogs(curPage-1)">← 上一页</button><span id="log-page" style="margin:0 10px"></span><button id="log-next" onclick="loadLogs(curPage+1)">下一页 →</button></div></div>
</div>

<div id="panel-recon" class="panel">
  <div class="stat-grid" id="recon-stats"></div>
  <div class="action-bar">
    <button id="recon-resubmit" onclick="resubmitSelected()" disabled>🔄 重新提交选中</button>
    <span class="count" id="recon-count">已选 0 条</span>
    <span style="flex:1"></span>
    <button onclick="loadRecon()" style="background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px">刷新</button>
  </div>
  <div id="recon-result"></div>
  <table><thead><tr>
    <th style="width:30px"><input type="checkbox" id="recon-select-all" onchange="toggleAll()"></th>
    <th>ID</th><th>平台单号</th><th>Bridge 状态</th><th>平台态</th><th>吉客云单号</th><th>物流单号</th><th>差异标记</th><th>错误/备注</th><th>更新于</th>
  </tr></thead>
  <tbody id="recon-rows"></tbody></table>
</div>

<div id="body-modal" class="modal" onclick="event.target===this&&closeModal()">
  <div class="modal-content"><span class="close" onclick="closeModal()">&times;</span>
  <pre id="modal-body"></pre></div>
</div>

<script>
let curPage=1, totalPages=1, reconData=[], selectedIds=new Set();

function switchTab(name){
  document.querySelectorAll('.tab,.panel').forEach(e=>e.classList.remove('active'));
  const idx={crons:1,logs:2,recon:3}[name];
  document.querySelector(`.tab:nth-child(${idx})`).classList.add('active');
  document.getElementById(`panel-${name}`).classList.add('active');
  if(name==='crons')loadCrons();
  if(name==='recon'){loadRecon();selectedIds.clear();updateCount()}
}
function showBody(t){document.getElementById('body-modal').classList.add('show');document.getElementById('modal-body').textContent=t}
function closeModal(){document.getElementById('body-modal').classList.remove('show')}
function statusBadge(s){if(s>=200&&s<300)return'<span class="badge badge-ok">'+s+'</span>';if(s)return'<span class="badge badge-err">'+s+'</span>';return'-'}
function apiCodeBadge(c){if(c===0||c===200)return'<span class="badge badge-ok">'+c+'</span>';if(c)return'<span class="badge badge-err">'+c+'</span>';return'-'}
function stateBadge(s){
  const m={'init':'badge-init','failed':'badge-err','jky_created':'badge-warn','audited':'badge-warn',
    'jky_shipped':'badge-ok','synced':'badge-ok','done':'badge-ok',
    'skipped':'badge-warn','cancelled':'badge-warn','jky_cancelled':'badge-ok',
    'static':'badge-ok'}
  return'<span class="badge '+(m[s]||'badge-warn')+'">'+s+'</span>'
}
function driftBadge(d){
  const m={'lanmeng_cancel':'badge-err','stale_24h':'badge-warn','stale_48h':'badge-err','failed':'badge-err','pending':'badge-warn','init':'badge-init'}
  return'<span class="badge '+(m[d]||'badge-warn')+'">'+d+'</span>'
}
function durStr(ms){if(!ms)return'-';if(ms<1000)return ms+'ms';return(ms/1e3).toFixed(1)+'s'}
function timeStr(t){if(!t)return'-';return t.replace('T',' ').slice(0,19)}
function trunc(s,l){if(!s)return'-';s=s.slice(0,l);return s}

async function loadCrons(){
  try{
    const r=await(await fetch('/admin/api/crons')).json();
    document.getElementById('cron-stats').innerHTML='<div class="stat-card"><div class="num">'+r.cursors.length+'</div><div class="label">游标数</div></div><div class="stat-card"><div class="num">'+r.recent_calls+'</div><div class="label">今日 API 调用</div></div>';
    document.getElementById('cron-cursors').innerHTML=r.cursors.map(c=>'<tr><td>'+c.key+'</td><td class="code">'+c.value+'</td><td class="ttl">'+timeStr(c.updated)+'</td></tr>').join('')||'<tr><td colspan="3" class="empty">暂无数据</td></tr>';
    document.getElementById('cron-recent').innerHTML=r.recent.map(l=>'<tr><td>'+l.source+'</td><td class="code">'+l.method+'</td><td>'+statusBadge(l.http_status)+'</td><td>'+apiCodeBadge(l.api_code)+'</td><td>'+durStr(l.duration_ms)+'</td><td class="ttl">'+timeStr(l.created_at)+'</td></tr>').join('')||'<tr><td colspan="6" class="empty">暂无数据</td></tr>';
  }catch(e){document.getElementById('cron-recent').innerHTML='<tr><td colspan="6" class="empty">加载失败: '+e.message+'</td></tr>'}
}

async function loadLogs(page){
  curPage=page||1;
  const src=document.getElementById('f-source').value;
  const method=document.getElementById('f-method').value;
  const status=document.getElementById('f-status').value;
  const size=document.getElementById('f-size').value;
  const params=new URLSearchParams({page:curPage,page_size:size});
  if(src)params.set('source',src);
  if(method)params.set('method',method);
  if(status)params.set('status',status);
  try{
    const r=await(await fetch('/admin/api/logs?'+params)).json();
    totalPages=r.total_pages;
    document.getElementById('log-rows').innerHTML=r.rows.map(l=>'<tr>'+
      '<td class="ttl">'+l.id+'</td>'+
      '<td>'+l.source+'</td>'+
      '<td class="code">'+l.method+'</td>'+
      '<td>'+statusBadge(l.http_status)+'</td>'+
      '<td>'+apiCodeBadge(l.api_code)+'</td>'+
      '<td>'+durStr(l.duration_ms)+'</td>'+
      '<td><button class="expand-btn" onclick="showBody('+JSON.stringify(trunc(l.request_body,200)).replace(/\"/g,'&quot;')+')">查看</button></td>'+
      '<td><button class="expand-btn" onclick="showBody('+JSON.stringify(trunc(l.response_body,200)).replace(/\"/g,'&quot;')+')">查看</button></td>'+
      '<td class="ttl">'+timeStr(l.created_at)+'</td>'+
    '</tr>').join('')||'<tr><td colspan="9" class="empty">无匹配日志</td></tr>';
    document.getElementById('log-info').textContent='共 '+r.total+' 条';
    document.getElementById('log-page').textContent='第 '+curPage+'/'+totalPages+' 页';
    document.getElementById('log-prev').disabled=curPage<=1;
    document.getElementById('log-next').disabled=curPage>=totalPages;
  }catch(e){document.getElementById('log-rows').innerHTML='<tr><td colspan="9" class="empty">加载失败: '+e.message+'</td></tr>'}
}

async function loadRecon(){
  try{
    const r=await(await fetch('/admin/api/reconciliation')).json();
    reconData=r.orders;
    // stats
    document.getElementById('recon-stats').innerHTML=
      '<div class="stat-card"><div class="num">'+r.total+'</div><div class="label">订单总数 (30d)</div></div>'+
      '<div class="stat-card"><div class="num '+('red' in r.summary?'yellow':'')+'">'+r.summary.pending+'</div><div class="label">待处理</div></div>'+
      '<div class="stat-card"><div class="num '+('red' in r.summary?'red':'')+'">'+r.summary.inconsistent+'</div><div class="label">差异数</div></div>'+
      '<div class="stat-card"><div class="num">'+r.summary.terminal+'</div><div class="label">已完成</div></div>';
    // rows
    document.getElementById('recon-rows').innerHTML=r.orders.map((o,i)=>'<tr id="recon-tr-'+o.id+'" onclick="toggleRow('+o.id+')">'+
      '<td><input type="checkbox" class="recon-cb" data-id="'+o.id+'" onchange="toggleRow('+o.id+')" '+(selectedIds.has(o.id)?'checked':'')+'></td>'+
      '<td class="ttl">'+o.id+'</td>'+
      '<td class="code">'+o.platform_order_no+'</td>'+
      '<td>'+stateBadge(o.state)+'</td>'+
      '<td>'+o.platform_state+'</td>'+
      '<td class="code">'+(o.jky_trade_no||'-')+'</td>'+
      '<td class="code">'+(o.logistic_no||'-')+'</td>'+
      '<td>'+(o.drift_flags||[]).map(driftBadge).join(' ')+'</td>'+
      '<td class="ttl" style="max-width:200px;overflow:hidden;text-overflow:ellipsis">'+(o.last_error||'')+'</td>'+
      '<td class="ttl">'+timeStr(o.updated_at)+'</td>'+
    '</tr>').join('')||'<tr><td colspan="10" class="empty">无待处理订单</td></tr>';
  }catch(e){document.getElementById('recon-rows').innerHTML='<tr><td colspan="10" class="empty">加载失败: '+e.message+'</td></tr>'}
}

function toggleRow(id){
  const cb=document.querySelector('.recon-cb[data-id="'+id+'"]');
  if(cb){cb.checked=!cb.checked}
  if(cb.checked)selectedIds.add(id);else selectedIds.delete(id);
  updateCount()
}
function toggleAll(){
  const all=document.getElementById('recon-select-all').checked;
  document.querySelectorAll('.recon-cb').forEach(cb=>{cb.checked=all;const id=parseInt(cb.dataset.id);all?selectedIds.add(id):selectedIds.delete(id)});
  updateCount()
}
function updateCount(){
  document.getElementById('recon-count').textContent='已选 '+selectedIds.size+' 条';
  document.getElementById('recon-resubmit').disabled=selectedIds.size===0;
}

async function resubmitSelected(){
  if(selectedIds.size===0)return;
  const ids=Array.from(selectedIds);
  document.getElementById('recon-resubmit').disabled=true;
  document.getElementById('recon-result').innerHTML='<div class="result-msg ok">提交中 ('+ids.length+' 条)...</div>';
  try{
    const r=await(await fetch('/admin/api/reconciliation/resubmit',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ids}),
    })).json();
    const ok=r.results.filter(x=>x.success).length;
    const fail=r.results.filter(x=>!x.success).length;
    document.getElementById('recon-result').innerHTML=
      '<div class="result-msg '+(fail?'err':'ok')+'">处理完成: '+ok+' 成功, '+fail+' 失败</div>';
    selectedIds.clear();
    updateCount();
    setTimeout(loadRecon,1000);
  }catch(e){
    document.getElementById('recon-result').innerHTML='<div class="result-msg err">请求异常: '+e.message+'</div>';
    document.getElementById('recon-resubmit').disabled=false;
  }
}

document.addEventListener('DOMContentLoaded',()=>{loadCrons();loadLogs(1)});
</script>
</body>
</html>"""


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_index():
    return DASHBOARD_HTML


@router.get("/api/crons")
async def api_cron_status():
    """Cron status: cursors + recent API calls"""
    conn = get_connection()
    cursors = conn.execute(
        "SELECT cursor_key, cursor_value, updated_at FROM cron_cursor ORDER BY cursor_key"
    ).fetchall()

    # Recent API calls (last 20 across all sources)
    today = datetime.now().strftime("%Y-%m-%d")
    recent = conn.execute(
        """SELECT source, method, http_status, api_code, duration_ms, created_at
           FROM api_call_log
           WHERE created_at >= ?
           ORDER BY id DESC LIMIT 20""",
        (today,),
    ).fetchall()

    today_count = conn.execute(
        "SELECT COUNT(*) FROM api_call_log WHERE created_at >= ?",
        (today,),
    ).fetchone()[0]

    return {
        "cursors": [
            {"key": r["cursor_key"], "value": r["cursor_value"],
             "updated": r["updated_at"]}
            for r in cursors
        ],
        "recent": [
            {
                "source": r["source"],
                "method": r["method"],
                "http_status": r["http_status"],
                "api_code": r["api_code"],
                "duration_ms": r["duration_ms"],
                "created_at": r["created_at"],
            }
            for r in recent
        ],
        "recent_calls": today_count,
    }


@router.get("/api/logs")
async def api_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    source: str = Query("", max_length=32),
    method: str = Query("", max_length=128),
    status: str = Query("", pattern="^(ok|err|)$"),
    days: int = Query(7, ge=1, le=90),
):
    """Paginated API call log query"""
    conn = get_connection()
    where = ["created_at >= datetime('now', ?)"]
    params = [f"-{days} days"]

    if source:
        where.append("source = ?")
        params.append(source)
    if method:
        where.append("method LIKE ?")
        params.append(f"%{method}%")
    if status == "ok":
        where.append("http_status >= 200 AND http_status < 300 AND (api_code = 0 OR api_code = 200)")
    elif status == "err":
        where.append("http_status = 0 OR http_status >= 400 OR (api_code != 0 AND api_code != 200)")

    where_clause = " AND ".join(where)

    count = conn.execute(
        f"SELECT COUNT(*) FROM api_call_log WHERE {where_clause}", params
    ).fetchone()[0]

    total_pages = max(1, (count + page_size - 1) // page_size)
    offset = (page - 1) * page_size

    rows = conn.execute(
        f"""SELECT id, source, method, http_status, api_code, api_sub_code,
                   substr(request_body, 1, 200) AS request_body,
                   substr(response_body, 1, 200) AS response_body,
                   duration_ms, created_at
            FROM api_call_log
            WHERE {where_clause}
            ORDER BY id DESC
            LIMIT ? OFFSET ?""",
        params + [page_size, offset],
    ).fetchall()

    return {
        "total": count,
        "total_pages": total_pages,
        "page": page,
        "page_size": page_size,
        "rows": [
            {
                "id": r["id"],
                "source": r["source"],
                "method": r["method"],
                "http_status": r["http_status"],
                "api_code": r["api_code"],
                "api_sub_code": r["api_sub_code"],
                "request_body": r["request_body"],
                "response_body": r["response_body"],
                "duration_ms": r["duration_ms"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


# ---- 对账部分 ----

def _classify_drift(row: dict) -> tuple[list[str], str]:
    """Orders in non-terminal states, flagging various drifts."""
    state = row["state"]
    platform_state = row["platform_state"]
    updated_at = row["updated_at"]
    last_error = row["last_error"] or ""

    if updated_at:
        if isinstance(updated_at, str):
            try:
                dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00").replace(" ", "T"))
                age_hours = (datetime.now() - dt).total_seconds() / 3600
            except Exception:
                age_hours = 0
        else:
            age_hours = 0
    else:
        age_hours = 0

    flags = []
    priority = "terminal"

    # Terminal states → low priority, only show if stale
    if state in ("done", "skipped", "jky_cancelled", "cancelled"):
        return (["static"], "terminal")

    # Lanmeng cancelled but bridge not cancelled
    if platform_state is not None and platform_state < 0:
        flags.append("lanmeng_cancel")
        priority = "critical"

    # Failed state
    if state == "failed":
        flags.append("failed")
        priority = "high"
    elif state == "init":
        flags.append("init")
        priority = "medium"
    elif state in ("jky_created", "audited", "jky_shipped", "synced"):
        flags.append("pending")
        priority = "medium"
    else:
        flags.append("pending")
        priority = "low"

    # Staleness
    if age_hours > 48:
        flags.append("stale_48h")
        priority = "high"
    elif age_hours > 24:
        flags.append("stale_24h")
        if priority == "low":
            priority = "medium"

    return (flags, priority)


@router.get("/api/reconciliation")
async def api_reconciliation():
    """三态对账：列出不一致/待处理的订单"""
    conn = get_connection()
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        """SELECT id, platform_order_no, platform_state, jky_trade_no,
                  logistic_no, state, retry_count, last_error, updated_at
           FROM order_map
           WHERE updated_at >= ?
           ORDER BY
             CASE
               WHEN state IN ('init','failed') THEN 0
               WHEN platform_state < 0 AND state NOT IN ('jky_cancelled','cancelled') THEN 1
               WHEN state IN ('jky_created','audited') THEN 2
               ELSE 3
             END,
             updated_at DESC
           LIMIT 200""",
        (cutoff,),
    ).fetchall()

    orders = []
    summary = {"pending": 0, "inconsistent": 0, "terminal": 0}
    for r in rows:
        flags, priority = _classify_drift(r)
        entry = {
            "id": r["id"],
            "platform_order_no": r["platform_order_no"],
            "platform_state": r["platform_state"],
            "jky_trade_no": r["jky_trade_no"],
            "logistic_no": r["logistic_no"],
            "state": r["state"],
            "retry_count": r["retry_count"],
            "last_error": r["last_error"],
            "updated_at": str(r["updated_at"]) if r["updated_at"] else None,
            "drift_flags": flags,
            "drift_priority": priority,
        }
        orders.append(entry)
        if priority == "terminal":
            summary["terminal"] += 1
        else:
            summary["pending"] += 1
            if "lanmeng_cancel" in flags or "failed" in flags:
                summary["inconsistent"] += 1

    return {
        "total": len(rows),
        "orders": orders,
        "summary": summary,
    }


@router.post("/api/reconciliation/resubmit")
async def api_reconciliation_resubmit(request: Request):
    """重新提交选中订单 — 根据当前状态执行相应恢复操作"""
    body = await request.json()
    ids = body.get("ids", [])
    if not ids:
        return {"success": False, "error": "no_ids", "results": []}

    conn = get_connection()
    results = []

    for order_id in ids:
        row = conn.execute(
            """SELECT id, platform_order_no, platform_state, jky_trade_no,
                      logistic_no, state, last_error, order_items_json
               FROM order_map WHERE id = ?""",
            (order_id,),
        ).fetchone()

        if not row:
            results.append({"id": order_id, "success": False, "action": "not_found"})
            continue

        state = row["state"]
        args = dict(row)
        result = await _resubmit_one(args, request.app.state)
        results.append({
            "id": order_id,
            "platform_order_no": row["platform_order_no"],
            "success": result["success"],
            "action": result["action"],
            "message": result.get("msg", ""),
        })

    return {"success": True, "results": results}


async def _resubmit_one(row: dict, app_state) -> dict:
    """Single order reprocess logic based on current state"""
    from .core.state_machine import transition, STATE_INIT, STATE_JKY_CANCELLED, STATE_FAILED

    order_id = row["id"]
    state = row["state"]
    platform_state = row["platform_state"]
    jky_trade_no = row["jky_trade_no"]
    platform_order_no = row["platform_order_no"]

    # Terminal states → skip
    if state in ("done", "skipped", "jky_cancelled", "cancelled"):
        return {"success": False, "action": "skipped_terminal", "msg": "终态无需处理"}

    try:
        # Case 1: Lanmeng cancelled → cancel in JKY
        if platform_state is not None and platform_state < 0:
            if jky_trade_no:
                jky_direct = app_state.jky_direct
                if jky_direct:
                    resp = await jky_direct.trade_cancel(jky_trade_no, "420001")
                    if resp.get("code") != 0:
                        return {"success": False, "action": "cancel",
                                "msg": f"JKY 取消失败: {resp.get('msg','')}"}
                    transition(order_id, STATE_JKY_CANCELLED, "admin_resubmit")
                    return {"success": True, "action": "cancel",
                            "msg": f"已取消 JKY {jky_trade_no}"}
                else:
                    return {"success": False, "action": "cancel", "msg": "jky_direct 不可用"}
            else:
                # No JKY trade → just mark cancelled
                transition(order_id, "cancelled", "admin_resubmit",
                           f"platform_state={platform_state}, 无 JKY 单")
                return {"success": True, "action": "mark_cancelled", "msg": "标记取消"}

        # Case 2: init or failed → re-create
        if state in ("init", "failed"):
            return await _resubmit_create(row, app_state)

        # Case 3: jky_created or audited → re-audit
        if state in ("jky_created", "audited"):
            if jky_trade_no:
                return await _resubmit_audit(row, app_state)
            else:
                # No JKY trade → re-create
                return await _resubmit_create(row, app_state)

        # Case 4: jky_shipped or synced → already in pipeline, inform
        return {"success": False, "action": "in_pipeline", "msg": f"状态 {state} 已在流程中"}

    except Exception as e:
        logger.exception(f"[resubmit] {order_id} 处理异常: {e}")
        transition(order_id, STATE_FAILED, "admin_resubmit", str(e))
        return {"success": False, "action": "error", "msg": str(e)}


async def _resubmit_create(row: dict, app_state) -> dict:
    """Re-create: fetch from lanmong, create in JKY, audit"""
    from .core.state_machine import transition, STATE_AUDITED, STATE_FAILED, STATE_JKY_CREATED

    order_id = row["id"]
    platform_order_no = row["platform_order_no"]
    jky_trade_no = row["jky_trade_no"]
    order_items_json = row.get("order_items_json", "")

    jky_direct = app_state.jky_direct
    lanmong = app_state.lanmong_client
    notifier = app_state.notifier

    if not jky_direct or not lanmong:
        return {"success": False, "action": "create", "msg": "客户端不可用"}

    # If already has a JKY trade, re-audit instead
    if jky_trade_no:
        return await _resubmit_audit(row, app_state)

    # Step 1: Re-fetch order from lanmong by orderNo
    try:
        lanmong_resp = await lanmong.get_deliver_orders(order_no=platform_order_no)
        lanmong_data = lanmong_resp.get("data", {})
        if isinstance(lanmong_data, dict):
            orders_list = lanmong_data.get("orderList", [])
        else:
            orders_list = lanmong_data if isinstance(lanmong_data, list) else []

        if not orders_list:
            return {"success": False, "action": "create",
                    "msg": "蓝盟未返回该订单数据（可能已过期或不存在）"}

        order = orders_list[0]
    except Exception as e:
        return {"success": False, "action": "create",
                "msg": f"蓝盟拉单失败: {e}"}

    # Step 2: Auto-review on lanmong
    try:
        review_resp = await lanmong.review_order(platform_order_no)
        if review_resp.get("code") == 0:
            transition(order_id, STATE_AUDITED, "admin_resubmit")
        else:
            logger.warning(f"[resubmit] {platform_order_no} 过审失败: {review_resp}")
    except Exception as e:
        logger.warning(f"[resubmit] {platform_order_no} 过审异常: {e}")

    # Step 3: Parse products → tradeOrderDetails
    conn = get_connection()
    products = order.get("orderProducts", [])
    trade_order_details = []
    for item in products:
        product_no = item.get("productNo", "")
        qty = item.get("number", 1)
        if not product_no:
            continue
        prod_row = conn.execute(
            "SELECT jky_barcode, jky_goods_name FROM jky_product_cache WHERE jky_goods_no = ?",
            (product_no,),
        ).fetchone()
        trade_order_details.append({
            "goodsNo": product_no,
            "barcode": prod_row["jky_barcode"] if prod_row else "",
            "goodsName": prod_row["jky_goods_name"] if prod_row else "",
            "specName": "默认",
            "unit": "件",
            "sellPrice": 0,
            "sellCount": qty,
            "sellTotal": 0,
        })

    if not trade_order_details:
        return {"success": False, "action": "create", "msg": "无有效商品明细"}

    # Step 4: Create JKY trade via direct client
    receiver_mobile = order.get("mobile", "")
    create_biz = {
        "tradeOrder": {
            "onlineTradeNo": platform_order_no,
            "shopName": "特渠分销对接",
            "shopCode": "0125",
            "warehouseCode": "02",
            "tradeTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tradeType": 1,
            "totalFee": 0,
            "payment": 0,
            "chargeCurrency": "人民币",
            "receiverName": order.get("name", ""),
            "receiverMobile": receiver_mobile,
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
            "expressPrice": order.get("expressPrice", 0),
            "buyerMemo": order.get("remark", ""),
            "tradeOrderDetails": trade_order_details,
        }
    }

    try:
        create_resp = await jky_direct.trade_create(create_biz["tradeOrder"])
        jky_code = create_resp.get("code", -1)
        if jky_code != 0:
            return {"success": False, "action": "create",
                    "msg": f"JKY 创单失败: {create_resp.get('msg','')}"}
        new_trade_no = create_resp.get("data", {}).get("tradeNo", "")
        if not new_trade_no:
            return {"success": False, "action": "create",
                    "msg": f"JKY 创单返回但缺 tradeNo: {json.dumps(create_resp, ensure_ascii=False)}"}
        conn.execute(
            "UPDATE order_map SET jky_trade_no = ?, order_items_json = ? WHERE id = ?",
            (new_trade_no, json.dumps(products, ensure_ascii=False, default=str), order_id),
        )
        conn.commit()
    except Exception as e:
        transition(order_id, STATE_FAILED, "admin_resubmit", str(e))
        return {"success": False, "action": "create", "msg": f"JKY 创单异常: {e}"}

    # Step 5: Audit
    try:
        audit_resp = await jky_direct.trade_audit(new_trade_no)
        audit_code = audit_resp.get("code", -1)
        if audit_code != 0:
            msg = audit_resp.get("msg", "")
            transition(order_id, STATE_JKY_CREATED, "admin_resubmit",
                       f"创单成功但审核失败: {msg}")
            if notifier:
                await notifier.alert_p1(platform_order_no,
                                        f"人工重提创单成功但审核失败: {msg}", 0, order_id)
            return {"success": True, "action": "create_no_audit",
                    "msg": f"创单成功({new_trade_no})但审核失败: {msg}"}
    except Exception as e:
        transition(order_id, STATE_JKY_CREATED, "admin_resubmit",
                   f"创单成功但审核异常: {e}")
        return {"success": True, "action": "create_no_audit",
                "msg": f"创单成功({new_trade_no})但审核异常: {e}"}

    transition(order_id, STATE_JKY_CREATED, "admin_resubmit")
    return {"success": True, "action": "create_audit",
            "msg": f"创单+审核成功: {new_trade_no}"}


async def _resubmit_audit(row: dict, app_state) -> dict:
    """Re-audit: just re-call trade_audit for existing JKY trade"""
    from .core.state_machine import transition, STATE_JKY_CREATED

    order_id = row["id"]
    jky_trade_no = row["jky_trade_no"]
    platform_order_no = row["platform_order_no"]

    jky_direct = app_state.jky_direct
    if not jky_direct:
        return {"success": False, "action": "audit", "msg": "jky_direct 不可用"}

    try:
        audit_resp = await jky_direct.trade_audit(jky_trade_no)
        if audit_resp.get("code") != 0:
            return {"success": False, "action": "audit",
                    "msg": f"JKY {jky_trade_no} 审核失败: {audit_resp.get('msg','')}"}
        transition(order_id, STATE_JKY_CREATED, "admin_resubmit")
        return {"success": True, "action": "audit",
                "msg": f"JKY {jky_trade_no} 审核成功"}
    except Exception as e:
        return {"success": False, "action": "audit", "msg": str(e)}
