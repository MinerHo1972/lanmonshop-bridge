"""Admin dashboard — cron status & API call log viewer"""

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from .storage.db import get_connection

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
  .badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:500}
  .badge-ok{background:#1b4332;color:#3fb950}
  .badge-err{background:#4d1a1a;color:#f85149}
  .badge-warn{background:#4d351a;color:#d29922}
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
  .ttl{color:#8b949e;font-size:11px}
  .empty{color:#8b949e;font-style:italic;padding:20px;text-align:center}
  .expand-btn{background:none;border:none;color:#58a6ff;cursor:pointer;font-size:11px;text-decoration:underline}
  .modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);z-index:1000}
  .modal.show{display:flex;align-items:center;justify-content:center}
  .modal-content{background:#161b22;border:1px solid #30363d;border-radius:8px;max-width:800px;width:90%;max-height:80vh;overflow:auto;padding:20px}
  .modal-content pre{background:#0d1117;padding:12px;border-radius:4px;overflow:auto;font-size:11px;max-height:50vh;color:#7ee787}
  .modal-content .close{float:right;cursor:pointer;color:#8b949e;font-size:20px}
</style>
</head>
<body>
<h1>🔧 Bridge Admin</h1>
<p class="desc">蓝盟-吉客云桥接服务 · 运行状态 & API 日志查询</p>

<div class="tab-bar">
  <div class="tab active" onclick="switchTab('crons')">📊 Cron 状态</div>
  <div class="tab" onclick="switchTab('logs')">📝 API 日志</div>
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

<div id="body-modal" class="modal" onclick="event.target===this&&closeModal()">
  <div class="modal-content"><span class="close" onclick="closeModal()">&times;</span>
  <pre id="modal-body"></pre></div>
</div>

<script>
let curPage=1, totalPages=1;
function switchTab(name){document.querySelectorAll('.tab,.panel').forEach(e=>e.classList.remove('active'));document.querySelector(`.tab:nth-child(${name==='crons'?1:2})`).classList.add('active');document.getElementById(`panel-${name}`).classList.add('active');if(name==='crons')loadCrons()}
function showBody(t){document.getElementById('body-modal').classList.add('show');document.getElementById('modal-body').textContent=t}
function closeModal(){document.getElementById('body-modal').classList.remove('show')}
function statusBadge(s){if(s>=200&&s<300)return'<span class="badge badge-ok">'+s+'</span>';if(s)return'<span class="badge badge-err">'+s+'</span>';return'-'}
function apiCodeBadge(c){if(c===0||c===200)return'<span class="badge badge-ok">'+c+'</span>';if(c)return'<span class="badge badge-err">'+c+'</span>';return'-'}
function durStr(ms){if(!ms)return'-';if(ms<1000)return ms+'ms';return(ms/1e3).toFixed(1)+'s'}
function timeStr(t){if(!t)return'-';return t.replace('T',' ').slice(0,19)}
function trunc(s,l){if(!s)return'-';s=s.slice(0,l);return s}

async function loadCrons(){
  try{
    const r=await(await fetch('/admin/api/crons')).json();
    // stats
    document.getElementById('cron-stats').innerHTML='<div class="stat-card"><div class="num">'+r.cursors.length+'</div><div class="label">游标数</div></div><div class="stat-card"><div class="num">'+r.recent_calls+'</div><div class="label">今日 API 调用</div></div>';
    // cursors
    document.getElementById('cron-cursors').innerHTML=r.cursors.map(c=>'<tr><td>'+c.key+'</td><td class="code">'+c.value+'</td><td class="ttl">'+timeStr(c.updated)+'</td></tr>').join('')||'<tr><td colspan="3" class="empty">暂无数据</td></tr>';
    // recent logs
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
      '<td><button class="expand-btn" onclick="showBody('+JSON.stringify(trunc(l.request_body,200)).replace(/"/g,'&quot;')+')">查看</button></td>'+
      '<td><button class="expand-btn" onclick="showBody('+JSON.stringify(trunc(l.response_body,200)).replace(/"/g,'&quot;')+')">查看</button></td>'+
      '<td class="ttl">'+timeStr(l.created_at)+'</td>'+
    '</tr>').join('')||'<tr><td colspan="9" class="empty">无匹配日志</td></tr>';
    document.getElementById('log-info').textContent='共 '+r.total+' 条';
    document.getElementById('log-page').textContent='第 '+curPage+'/'+totalPages+' 页';
    document.getElementById('log-prev').disabled=curPage<=1;
    document.getElementById('log-next').disabled=curPage>=totalPages;
  }catch(e){document.getElementById('log-rows').innerHTML='<tr><td colspan="9" class="empty">加载失败: '+e.message+'</td></tr>'}
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
    where = ["created_at >= datetime('now', ?)", f"'-{days} days'"]
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
