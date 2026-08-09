"""Admin dashboard — cron status, API log viewer & reconciliation (对账)"""

import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Query, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from .auth import get_current_user, require_admin
from .storage.db import get_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")


def record_admin_alert(conn, notifier, order_id: int,
                       platform_order_no: str,
                       level: str, category: str, message: str,
                       resolved: bool = False):
    """事务性记录 admin 操作告警

    - 脱敏：消息截断 200 字
    - 写 alert_log + 更新 order_map 聚合字段
    - 调 notifier（fire-and-forget via create_task）
    - resolved=True → 清零该订单告警计数（修复成功后调用）
    """
    import asyncio
    safe_msg = message[:200]
    if resolved:
        conn.execute(
            "UPDATE order_map SET alert_count = 0, last_alert_level = '',"
            "last_alert_time = '', last_alert_message = '' WHERE id = ?",
            (order_id,)
        )
        conn.commit()
        return

    loop = asyncio.get_event_loop()
    if level == "P0":
        loop.create_task(notifier.alert_p0(platform_order_no, safe_msg, order_id, "admin"))
    elif level == "P1":
        loop.create_task(notifier.alert_p1(platform_order_no, safe_msg, 0, order_id))
    elif level == "P2":
        loop.create_task(notifier.alert_p2(level, safe_msg, category=category))

    conn.execute(
        "INSERT INTO alert_log (order_id, platform_order_no, level, category, message) "
        "VALUES (?, ?, ?, ?, ?)",
        (order_id, platform_order_no, level, category, safe_msg)
    )
    conn.execute(
        "UPDATE order_map SET alert_count = alert_count + 1,"
        "last_alert_level = ?,"
        "last_alert_time = strftime('%Y-%m-%d %H:%M:%S','now'),"
        "last_alert_message = ? WHERE id = ?",
        (level, safe_msg, order_id)
    )
    conn.commit()

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
<p class="desc">蓝盟-吉客云云桥接服务 · 运行状态 & API 日志查询 & 订单表</p>

<div class="tab-bar">
  <div class="tab active" onclick="switchTab('recon')">📋 订单表</div>
  <div class="tab" onclick="switchTab('crons')">📊 Cron 状态</div>
  <div class="tab" onclick="switchTab('logs')">📝 API 日志</div>
</div>

<div id="panel-crons" class="panel">
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
    <label>搜索 <input id="f-q" placeholder="订单号/单号/关键字" style="width:160px"></label>
    <label>每页 <select id="f-size"><option>20</option><option selected>50</option><option>100</option></select></label>
    <button onclick="loadLogs(1)" style="background:#238636;border:none;color:#fff;padding:4px 14px;border-radius:4px;cursor:pointer;font-size:12px">查询</button>
  </div>
  <table><thead><tr><th>ID</th><th>来源</th><th>方法</th><th>HTTP</th><th>业务码</th><th>结果</th><th>耗时</th><th>请求体</th><th>响应体</th><th>错误</th><th>时间</th></tr></thead>
  <tbody id="log-rows"></tbody></table>
  <div class="pagination"><span id="log-info"></span><div><button id="log-prev" onclick="loadLogs(curPage-1)">← 上一页</button><span id="log-page" style="margin:0 10px"></span><button id="log-next" onclick="loadLogs(curPage+1)">下一页 →</button></div></div>
</div>

<div id="panel-recon" class="panel active">
  <div class="sub-tab-bar" style="display:flex;gap:4px;margin-bottom:12px">
    <div class="sub-tab active" onclick="switchReconSub('pending')">📋 待处理</div>
    <div class="sub-tab" onclick="switchReconSub('report')">📊 日报</div>
  </div>

  <div id="recon-sub-pending">
    <div class="stat-grid" id="recon-stats"></div>
    <div class="action-bar">
      <select id="recon-filter" style="background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:4px 10px;border-radius:4px;font-size:12px">
        <option value="all">全部状态</option>
        <option value="consistent">✅ 一致</option>
        <option value="inconsistent">❌ 不一致</option>
        <option value="undelivered">📤 未递交</option>
      </select>
      <select id="recon-action" style="background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:4px 10px;border-radius:4px;font-size:12px">
      </select>
      <button id="recon-resubmit" onclick="doAction()" disabled>执行</button>
      <span class="count" id="recon-count">已选 0 条</span>
      <span style="flex:1"></span>
      <button onclick="loadRecon(1)" style="background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px">刷新</button>
    </div>
    <div id="recon-result"></div>
    <table><thead><tr>
      <th style="width:30px"><input type="checkbox" id="recon-select-all" onchange="toggleAll()"></th>
      <th>ID</th><th>平台单号</th><th>🟢 蓝盟</th><th>🔷 桥(DB)</th><th>🔴 吉客云</th><th>三端一致</th><th>吉客云单号</th><th>物流单号</th><th>错误/备注</th><th>🚨 告警</th><th>更新于</th><th style="width:60px">日志</th>
    </tr></thead>
    <tbody id="recon-rows"></tbody></table>
    <div class="pagination" id="recon-pagination" style="margin-top:8px">
      <span id="recon-page-info" class="ttl"></span>
      <div>
        <button id="recon-prev" onclick="loadRecon(reconPage-1)" style="background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:12px">← 上一页</button>
        <span id="recon-page-num" style="margin:0 10px;color:#8b949e;font-size:12px"></span>
        <button id="recon-next" onclick="loadRecon(reconPage+1)" style="background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:12px">下一页 →</button>
      </div>
    </div>
  </div>

  <div id="recon-sub-report" style="display:none">
    <div class="stat-grid" id="report-stats"></div>
    <h2>对账日报</h2>
    <table><thead><tr><th>日期</th><th>蓝盟单数</th><th>DB 追踪</th><th>JKY 创单</th><th>已闭环</th><th>偏差数</th><th>待处理</th></tr></thead>
    <tbody id="report-rows"></tbody></table>
    <h2>差异详情</h2>
    <table><thead><tr><th>平台单号</th><th>DB 状态</th><th>蓝盟态</th><th>JKY 状态</th><th>原因</th></tr></thead>
    <tbody id="report-deviations"></tbody></table>
  </div>
</div>

<div id="body-modal" class="modal" onclick="event.target===this&&closeModal()">
  <div class="modal-content"><span class="close" onclick="closeModal()">&times;</span>
  <pre id="modal-body"></pre></div>
</div>

<script>
let curPage=1, totalPages=1, reconData=[], selectedIds=new Set();

async function apiFetch(url, opts){
  const r=await fetch(url, opts);
  if(r.status===401){window.location.href='/admin/auth/login';return r}
  return r
}

function switchTab(name){
  document.querySelectorAll('.tab,.panel').forEach(e=>e.classList.remove('active'));
  const idx={recon:1,crons:2,logs:3,users:4}[name];
  document.querySelector(`.tab:nth-child(${idx})`).classList.add('active');
  document.getElementById(`panel-${name}`).classList.add('active');
  if(name==='crons')loadCrons();
  if(name==='recon'){loadRecon();selectedIds.clear();updateCount()}
  if(name==='users')loadUsers();
}
function showBody(t){document.getElementById('body-modal').classList.add('show');document.getElementById('modal-body').textContent=t}
function closeModal(){document.getElementById('body-modal').classList.remove('show')}
function switchToLogs(orderNo){
  switchTab('logs');
  document.getElementById('f-q').value=orderNo;
  loadLogs(1);
}
function statusBadge(s){if(s>=200&&s<300)return'<span class="badge badge-ok">'+s+'</span>';if(s)return'<span class="badge badge-err">'+s+'</span>';return'-'}
function apiCodeBadge(c){if(c===0||c===200)return'<span class="badge badge-ok">'+c+'</span>';if(c)return'<span class="badge badge-err">'+c+'</span>';return'-'}
function stateBadge(s){
  const m={'init':'badge-init','failed':'badge-err','jky_created':'badge-warn','audited':'badge-warn',
    'jky_shipped':'badge-ok','synced':'badge-ok','done':'badge-ok',
    'skipped':'badge-warn','cancelled':'badge-warn','jky_cancelled':'badge-ok',
    'static':'badge-ok'}
  return'<span class="badge '+(m[s]||'badge-warn')+'">'+s+'</span>'
}
function unifiedBadge(label){
  const clss={'待发货':'badge-init','部分发货':'badge-warn','已发货':'badge-ok','已完成':'badge-ok','已取消/退款':'badge-err'};
  return'<span class="badge '+(clss[label]||'badge-warn')+'">'+(label||'?')+'</span>'
}
function stateLabelBadge(label, raw){
  // 根据原始值决定颜色: 取消/异常/失败 = 红色, 初始/待处理 = 黄色, done/ok = 绿色
  const n=parseInt(raw); let cls='badge-ok';
  if(raw==='init'||raw==='failed'||(n<0))cls='badge-err';
  else if(raw==='jky_created'||raw==='audited'||raw==='待发货'||raw==='初始')cls='badge-warn';
  else if(label==='未创建')cls='badge-init';
  return'<span class="badge '+cls+'">'+label+'</span>'
}
function driftBadge(d){
  const m={'lanmeng_cancel':'badge-err','stale_24h':'badge-warn','stale_48h':'badge-err','failed':'badge-err','pending':'badge-warn','init':'badge-init'}
  return'<span class="badge '+(m[d]||'badge-warn')+'">'+d+'</span>'
}
function durStr(ms){if(!ms)return'-';if(ms<1000)return ms+'ms';return(ms/1e3).toFixed(1)+'s'}
function resultBadge(ok){return ok?'<span class="badge badge-ok">✅ 成功</span>':'<span class="badge badge-err">❌ 失败</span>'}
function timeStr(t){
  if(!t)return'-';
  const d=new Date((t.replace(' ','T')||'')+'Z');
  if(isNaN(d.getTime()))return t.slice(0,19);
  return d.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}).replace(/\//g,'-');
}
function trunc(s,l){if(!s)return'-';s=s.slice(0,l);return s}
function alertBadge(count, level){
  if(!count||count===0)return'';
  const colors={P0:'badge-err',P1:'badge-warn',P2:'badge-init'};
  return'<span class="badge '+(colors[level]||'badge-warn')+'" style="cursor:pointer" onclick="event.stopPropagation();showAlertLog('+count+')" title="'+level+' · 点击查看详情">🔔'+count+'</span>'
}
async function showAlertLog(orderId){
  try{
    const r=await(await apiFetch('/admin/api/alerts?order_id='+orderId+'&limit=20')).json();
    const html=r.alerts.map(a=>'<div style="padding:4px 0;border-bottom:1px solid #30363d;font-size:12px">'+
      '<span class="badge '+(a.level==='P0'?'badge-err':a.level==='P1'?'badge-warn':'badge-init')+'">'+a.level+'</span> '+
      '<span class="ttl">'+a.category+'</span> '+
      '<span style="color:#c9d1d9">'+a.message.slice(0,200)+'</span>'+
      ' <span class="ttl">'+timeStr(a.created_at)+'</span>'+
      '</div>').join('')||'<div class="empty">无告警</div>';
    document.getElementById('modal-body').innerHTML=html;
    document.getElementById('body-modal').classList.add('show');
  }catch(e){alert('加载告警失败: '+e.message)}
}

async function loadCrons(){
  try{
    const r=await(await apiFetch('/admin/api/crons')).json();
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
  const q=document.getElementById('f-q').value;
  const size=document.getElementById('f-size').value;
  const params=new URLSearchParams({page:curPage,page_size:size});
  if(src)params.set('source',src);
  if(method)params.set('method',method);
  if(status)params.set('status',status);
  if(q)params.set('q',q);
  try{
    const r=await(await apiFetch('/admin/api/logs?'+params)).json();
    totalPages=r.total_pages;
    document.getElementById('log-rows').innerHTML=r.rows.map(l=>'<tr>'+
      '<td class="ttl">'+l.id+'</td>'+
      '<td>'+l.source+'</td>'+
      '<td class="code">'+l.method+'</td>'+
      '<td>'+statusBadge(l.http_status)+'</td>'+
      '<td>'+apiCodeBadge(l.api_code)+'</td>'+
      '<td>'+resultBadge(l.is_success)+'</td>'+
      '<td>'+durStr(l.duration_ms)+'</td>'+
      '<td>'+(l.request_body?'<button class="expand-btn" onclick="showBody(this.dataset.body)" data-body="'+(l.request_body.slice(0,800).replace(/"/g,'&quot;'))+'">查看</button>':'-')+'</td>'+
      '<td>'+(l.response_body?'<button class="expand-btn" onclick="showBody(this.dataset.body)" data-body="'+(l.response_body.slice(0,800).replace(/"/g,'&quot;'))+'">查看</button>':'-')+'</td>'+
      '<td class="ttl" style="max-width:120px;overflow:hidden;text-overflow:ellipsis">'+(l.error||'')+'</td>'+
      '<td class="ttl">'+timeStr(l.created_at)+'</td>'+
    '</tr>').join('')||'<tr><td colspan="11" class="empty">无匹配日志</td></tr>';
    document.getElementById('log-info').textContent='共 '+r.total+' 条';
    document.getElementById('log-page').textContent='第 '+curPage+'/'+totalPages+' 页';
    document.getElementById('log-prev').disabled=curPage<=1;
    document.getElementById('log-next').disabled=curPage>=totalPages;
  }catch(e){document.getElementById('log-rows').innerHTML='<tr><td colspan="9" class="empty">加载失败: '+e.message+'</td></tr>'}
}

let reconPage=1;
let reconTotalPages=1;

async function loadRecon(page){
  reconPage=page||1;
  try{
    const r=await(await apiFetch('/admin/api/reconciliation?page='+reconPage+'&page_size=50')).json();
    const filter=document.getElementById('recon-filter').value;
    let orders=r.orders;
    if(filter==='consistent')orders=orders.filter(o=>o.consistent);
    else if(filter==='inconsistent')orders=orders.filter(o=>!o.consistent);
    else if(filter==='undelivered')orders=orders.filter(o=>o.state==='init'||o.state==='failed'||o.state==='skipped');
    reconData=r.orders;
    // 五态统计 (以 bridge_unified 为准)
    const FIVE_STATES=['待发货','部分发货','已发货','已完成','已取消/退款'];
    const counts={};let total=r.orders.length;
    FIVE_STATES.forEach(s=>counts[s]={total:0,consistent:0,inconsistent:0});
    let consistentCount=0,inconsistentCount=0;
    r.orders.forEach(o=>{
      const s=o.bridge_unified||'待发货';
      if(!counts[s])counts[s]={total:0,consistent:0,inconsistent:0};
      counts[s].total++;
      if(o.consistent){counts[s].consistent++;consistentCount++}
      else{counts[s].inconsistent++;inconsistentCount++}
    });
    document.getElementById('recon-stats').innerHTML=
      '<div class="stat-card"><div class="num">'+r.total+'</div><div class="label">订单总数</div></div>'+
      FIVE_STATES.map(s=>'<div class="stat-card"><div class="num '+(counts[s]?.inconsistent>0?'red':'')+'">'+(counts[s]?.total||0)+
        '</div><div class="label">'+s+'</div></div>').join('')+
      '<div class="stat-card"><div class="num">'+consistentCount+'</div><div class="label">一致</div></div>'+
      '<div class="stat-card"><div class="num '+(inconsistentCount>0?'red':'')+'">'+inconsistentCount+'</div><div class="label">不一致</div></div>'+
      '<div class="stat-card"><div class="num">'+orders.length+'</div><div class="label">本页</div></div>';
    // rows — 用筛选后的 orders
    document.getElementById('recon-rows').innerHTML=orders.map((o,i)=>'<tr id="recon-tr-'+o.id+'" onclick="toggleRow('+o.id+')">'+
      '<td><input type="checkbox" class="recon-cb" data-id="'+o.id+'" onchange="toggleRow('+o.id+')" '+(selectedIds.has(o.id)?'checked':'')+'></td>'+
      '<td class="ttl">'+o.id+'</td>'+
      '<td class="code">'+o.platform_order_no+'</td>'+
      '<td>'+unifiedBadge(o.platform_unified)+'</td>'+
      '<td>'+unifiedBadge(o.bridge_unified)+'</td>'+
      '<td>'+unifiedBadge(o.jky_unified)+'</td>'+
      '<td>'+(o.consistent
        ?'<span class="badge badge-ok">一致</span>'
        :'<span class="badge badge-err">不一致</span>')+'</td>'+
      '<td class="code">'+(o.jky_trade_no||'-')+'</td>'+
      '<td class="code">'+(o.logistic_no||'-')+'</td>'+
      '<td class="ttl" style="max-width:180px;overflow:hidden;text-overflow:ellipsis">'+(o.last_error||'')+'</td>'+
      '<td style="text-align:center">'+alertBadge(o.alert_count, o.last_alert_level)+'</td>'+
      '<td class="ttl">'+timeStr(o.updated_at)+'</td>'+
      '<td><button class="expand-btn" onclick="event.stopPropagation();switchToLogs(&#39;'+o.platform_order_no+'&#39;)">📋</button></td>'+
    '</tr>').join('')||'<tr><td colspan="13" class="empty">无待处理订单</td></tr>';
    // 分页控件
    reconTotalPages=r.total_pages||1;
    document.getElementById('recon-page-info').textContent='共 '+r.total+' 条 / 第 '+r.page+'/'+r.total_pages+' 页';
    document.getElementById('recon-page-num').textContent=r.page+'/'+r.total_pages;
    document.getElementById('recon-prev').disabled=r.page<=1;
    document.getElementById('recon-next').disabled=r.page>=r.total_pages;
    document.getElementById('recon-prev').style.opacity=r.page<=1?'.4':'1';
    document.getElementById('recon-next').style.opacity=r.page>=r.total_pages?'.4':'1';
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

const ACTION_OPTIONS=[
  {value:'pull-lanmong',label:'📥 从蓝盟重新拉取'},
  {value:'resubmit-jky',label:'🚀 重新提交到吉客云'},
  {value:'pull-jky',label:'📥 从吉客云重新拉取'},
  {value:'resubmit-lanmong',label:'🚀 回传蓝盟（物流同步）'},
];
const ACTION_URLS={
  'pull-lanmong':'/admin/api/reconciliation/pull-lanmong',
  'resubmit-jky':'/admin/api/reconciliation/resubmit',
  'pull-jky':'/admin/api/reconciliation/pull-jky',
  'resubmit-lanmong':'/admin/api/reconciliation/resubmit-lanmong',
};

function updateActionSelect(){
  if(selectedIds.size===0){document.getElementById('recon-action').innerHTML='<option value="">— 请先选择订单 —</option>';return}
  // 统计选中订单的推荐操作
  const counts={};
  reconData.forEach(o=>{if(selectedIds.has(o.id)){const a=o.suggested_action||'pull-lanmong';counts[a]=(counts[a]||0)+1}});
  const best=Object.entries(counts).sort((a,b)=>b[1]-a[1])[0][0];
  // 按推荐顺序生成选项
  const ordered=['pull-lanmong','resubmit-jky','pull-jky','resubmit-lanmong'];
  document.getElementById('recon-action').innerHTML=ordered.map(v=>{
    const label=ACTION_OPTIONS.find(o=>o.value===v)?.label||v;
    return'<option value="'+v+'"'+(v===best?' selected':'')+(counts[v]?'':'')+'>'+label+'</option>'
  }).join('');
}

async function doAction(){
  if(selectedIds.size===0)return;
  const ids=Array.from(selectedIds);
  const action=document.getElementById('recon-action').value;
  if(!action||!ACTION_URLS[action])return;
  const label=ACTION_OPTIONS.find(o=>o.value===action)?.label||action;
  document.getElementById('recon-resubmit').disabled=true;
  document.getElementById('recon-result').innerHTML='<div class="result-msg ok">'+label+'中 ('+ids.length+' 条)...</div>';
  try{
    const r=await(await apiFetch(ACTION_URLS[action],{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ids}),
    })).json();
    const ok=r.results.filter(x=>x.success).length;
    const fail=r.results.filter(x=>!x.success).length;
    const msgs=r.results.filter(x=>x.msg).map(x=>x.msg).slice(0,3).join('; ');
    document.getElementById('recon-result').innerHTML=
      '<div class="result-msg '+(fail?'err':'ok')+'">'+label+'完成: '+ok+' 成功, '+fail+' 失败'+(msgs?'<br><span style="font-size:11px">'+msgs+'</span>':'')+'</div>';
    selectedIds.clear();
    updateCount();
    setTimeout(loadRecon,1000);
  }catch(e){
    document.getElementById('recon-result').innerHTML='<div class="result-msg err">请求异常: '+e.message+'</div>';
    document.getElementById('recon-resubmit').disabled=false;
  }
}

function updateCount(){
  document.getElementById('recon-count').textContent='已选 '+selectedIds.size+' 条';
  document.getElementById('recon-resubmit').disabled=selectedIds.size===0;
  updateActionSelect();
}

// ---- 用户管理 ----

async function loadUsers(){
  try{
    const users=await(await apiFetch('/admin/api/users')).json();
    document.getElementById('users-rows').innerHTML=(users||[]).map(u=>
      '<tr><td class="code" style="max-width:200px;overflow:hidden;text-overflow:ellipsis">'+u.open_id+'</td>'+
      '<td>'+u.name+'</td>'+
      '<td><span class="badge '+(u.role==='admin'?'badge-err':'badge-init')+'">'+u.role+'</span></td>'+
      '<td class="ttl">'+(u.added_by||'-')+'</td>'+
      '<td class="ttl">'+timeStr(u.added_at)+'</td>'+
      '<td>'+(u.role==='admin'?'<span class="ttl">不可删除</span>':'<button class="expand-btn" onclick="deleteUser(&#39;'+u.open_id+'&#39;,&#39;'+u.name+'&#39;)">删除</button>')+'</td>'+
      '</tr>'
    ).join('')||'<tr><td colspan="6" class="empty">无已授权用户</td></tr>';
  }catch(e){document.getElementById('users-rows').innerHTML='<tr><td colspan="6" class="empty">加载失败: '+e.message+'</td></tr>'}

  try{
    const pending=await(await apiFetch('/admin/api/users/pending')).json();
    document.getElementById('pending-rows').innerHTML=(pending||[]).map(p=>
      '<tr><td class="code" style="max-width:200px;overflow:hidden;text-overflow:ellipsis">'+p.open_id+'</td>'+
      '<td>'+p.name+'</td>'+
      '<td class="ttl">'+timeStr(p.created_at)+'</td>'+
      '<td>'+
        '<button class="expand-btn" onclick="approveUser(&#39;'+p.open_id+'&#39;,&#39;'+p.name+'&#39;)" style="color:#3fb950">批准</button> '+
        '<button class="expand-btn" onclick="rejectUser(&#39;'+p.open_id+'&#39;,&#39;'+p.name+'&#39;)" style="color:#f85149">拒绝</button>'+
      '</td></tr>'
    ).join('')||'<tr><td colspan="4" class="empty">无待审批用户</td></tr>';
  }catch(e){document.getElementById('pending-rows').innerHTML='<tr><td colspan="4" class="empty">加载失败: '+e.message+'</td></tr>'}
}

async function approveUser(openId, name){
  if(!confirm('批准 '+name+' 的访问权限？'))return;
  try{
    const r=await(await apiFetch('/admin/api/users/approve',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({open_id:openId}),
    })).json();
    if(r.success){loadUsers();alert('已批准: '+name)}else{alert('批准失败: '+(r.error||''))}
  }catch(e){alert('请求异常: '+e.message)}
}

async function rejectUser(openId, name){
  if(!confirm('拒绝 '+name+' 的审批申请？'))return;
  try{
    const r=await(await apiFetch('/admin/api/users/reject',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({open_id:openId}),
    })).json();
    if(r.success){loadUsers();alert('已拒绝: '+name)}else{alert('拒绝失败: '+(r.error||''))}
  }catch(e){alert('请求异常: '+e.message)}
}

async function deleteUser(openId, name){
  if(!confirm('确认移除成员 '+name+' 的访问权限？（将同时销毁其所有活跃 session）'))return;
  if(!confirm('再次确认：移除后将立即生效，'+name+' 将在下次登录后重新提交审批。'))return;
  try{
    const r=await(await apiFetch('/admin/api/users/'+encodeURIComponent(openId),{method:'DELETE'})).json();
    if(r.success){loadUsers();alert('已移除: '+name+(r.sessions_cleared?' (已清除 '+r.sessions_cleared+' 个 session)':''))}
    else{alert('移除失败: '+(r.error||''))}
  }catch(e){alert('请求异常: '+e.message)}
}

document.addEventListener('DOMContentLoaded',()=>{loadRecon(1);loadCrons();loadLogs(1)});
document.getElementById('recon-filter')?.addEventListener('change',()=>loadRecon(1));

// 对账子 tab
function switchReconSub(name){
  document.querySelectorAll('#panel-recon .sub-tab').forEach(e=>e.classList.remove('active'));
  document.getElementById('recon-sub-pending').style.display='none';
  document.getElementById('recon-sub-report').style.display='none';
  if(name==='pending'){
    document.querySelector('#panel-recon .sub-tab:nth-child(1)').classList.add('active');
    document.getElementById('recon-sub-pending').style.display='block';
    loadRecon(1);
  }else{
    document.querySelector('#panel-recon .sub-tab:nth-child(2)').classList.add('active');
    document.getElementById('recon-sub-report').style.display='block';
    loadReports();
  }
}

async function loadReports(){
  try{
    // Try to load report detail (reports list first to find latest with deviations)
    const list=await(await apiFetch('/admin/api/reconciliation/reports?limit=5')).json();
    // stats from latest report
    if(list.reports&&list.reports.length>0){
      const latest=list.reports[0];
      const s=latest.summary;
      document.getElementById('report-stats').innerHTML=
        '<div class="stat-card"><div class="num">'+s.lanmong_total+'</div><div class="label">蓝盟订单(30d)</div></div>'+
        '<div class="stat-card"><div class="num">'+s.db_total+'</div><div class="label">DB 追踪</div></div>'+
        '<div class="stat-card"><div class="num">'+s.jky_created+'</div><div class="label">JKY 创单</div></div>'+
        '<div class="stat-card"><div class="num '+(s.deviations>0?'red':'')+'">'+s.deviations+'</div><div class="label">偏差</div></div>';
      // report list rows
      document.getElementById('report-rows').innerHTML=list.reports.map(r=>'<tr>'+
        '<td>'+r.report_date+'</td>'+
        '<td>'+r.summary.lanmong_total+'</td>'+
        '<td>'+r.summary.db_total+'</td>'+
        '<td>'+r.summary.jky_created+'</td>'+
        '<td>'+r.summary.done+'</td>'+
        '<td>'+(r.deviation_count>0?'<span class="badge badge-err">'+r.deviation_count+'</span>':'<span class="badge badge-ok">0</span>')+'</td>'+
        '<td>'+r.summary.pending+'</td>'+
      '</tr>').join('')||'<tr><td colspan="7" class="empty">暂无报告</td></tr>';
      // load detail for deviations
      const detail=await(await apiFetch('/admin/api/reconciliation/reports/'+latest.id)).json();
      document.getElementById('report-deviations').innerHTML=(detail.deviations||[]).map(d=>'<tr>'+
        '<td class="code">'+(d.order_no||'-')+'</td>'+
        '<td>'+stateBadge(d.db_state||d.db_current_state||'-')+'</td>'+
        '<td>'+(d.lanmong_state_label||'-')+'</td>'+
        '<td>'+(d.jky_status||'-')+'</td>'+
        '<td class="ttl" style="max-width:300px;overflow:hidden;text-overflow:ellipsis">'+(d.reason||'')+'</td>'+
      '</tr>').join('')||'<tr><td colspan="5" class="empty">无差异</td></tr>';
    }else{
      document.getElementById('report-stats').innerHTML='<div class="stat-card"><div class="num">--</div><div class="label">暂无报告</div></div>';
      document.getElementById('report-rows').innerHTML='<tr><td colspan="7" class="empty">暂无报告（cron-f 每天 03:30 生成）</td></tr>';
      document.getElementById('report-deviations').innerHTML='<tr><td colspan="5" class="empty">暂无数据</td></tr>';
    }
  }catch(e){
    document.getElementById('report-rows').innerHTML='<tr><td colspan="7" class="empty">加载失败: '+e.message+'</td></tr>';
  }
}
</script>
</body>
</html>"""


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_index(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/admin/auth/login")

    role = user.get("role", "")
    if role not in ("admin", "member"):
        return RedirectResponse(url="/admin/auth/pending")

    # 注入用户信息到 dashboard
    html = DASHBOARD_HTML.replace(
        '</h1>',
        f'</h1> <span style="font-size:12px;color:#8b949e;margin-left:12px">'
        f'{user.get("name","")} · {"管理员" if role=="admin" else "成员"} · '
        f'<a href="/admin/auth/logout" '
        f'style="color:#8b949e;text-decoration:underline">退出</a></span>',
        1,
    )

    # member 不可见用户管理 tab，也不可见用户管理面板
    if role == "admin":
        # 注入用户管理 tab（在日志 tab 之后，对账 tab 之前）
        users_tab = '<div class="tab" onclick="switchTab(\'users\')">👥 用户管理</div>'
        html = html.replace(
            '<div class="tab" onclick="switchTab(\'logs\')">📝 API 日志</div>',
            '<div class="tab" onclick="switchTab(\'logs\')">📝 API 日志</div>' + users_tab,
            1,
        )
        # 注入用户管理面板 HTML
        users_panel = (
            '<div id="panel-users" class="panel">\n'
            '  <div class="action-bar">\n'
            '    <h2 style="margin:0;font-size:16px;color:#f0f6fc;border:none">已授权用户</h2>\n'
            '  </div>\n'
            '  <table><thead><tr><th>Open ID</th><th>姓名</th><th>角色</th><th>授权人</th>'
            '<th>授权时间</th><th>操作</th></tr></thead>\n'
            '  <tbody id="users-rows"></tbody></table>\n'
            '  <h2 style="margin-top:24px">待审批用户</h2>\n'
            '  <table><thead><tr><th>Open ID</th><th>姓名</th><th>申请时间</th><th>操作</th></tr></thead>\n'
            '  <tbody id="pending-rows"></tbody></table>\n'
            '</div>'
        )
        html = html.replace('<div id="panel-recon"', users_panel + '<div id="panel-recon"', 1)

    return html


@router.get("/api/crons")
async def api_cron_status(request: Request):
    """Cron status: cursors + recent API calls"""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    conn = get_connection()
    cursors = conn.execute(
        "SELECT cursor_key, cursor_value, updated_at FROM cron_cursor ORDER BY cursor_key"
    ).fetchall()

    # Recent API calls (last 20 across all sources)
    # Use SQLite's datetime('now') which returns UTC, matching DB timestamps
    today = "datetime('now', 'start of day')"
    recent = conn.execute(
        """SELECT source, method, http_status, api_code, duration_ms, created_at
           FROM api_call_log
           WHERE created_at >= """
        + today
        + """
           ORDER BY id DESC LIMIT 20""",
    ).fetchall()

    today_count = conn.execute(
        "SELECT COUNT(*) FROM api_call_log WHERE created_at >= "
        + today
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
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    source: str = Query("", max_length=32),
    method: str = Query("", max_length=128),
    status: str = Query("", pattern="^(ok|err|)$"),
    q: str = Query("", max_length=128),
    days: int = Query(7, ge=1, le=90),
):
    """Paginated API call log query"""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    conn = get_connection()
    where = ["created_at >= datetime('now', ?)"]
    params = [f"-{days} days"]

    if source:
        where.append("source = ?")
        params.append(source)
    if method:
        where.append("method LIKE ?")
        params.append(f"%{method}%")
    if q:
        where.append("(request_body LIKE ? OR response_body LIKE ? OR method LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    if status == "ok":
        where.append("(http_status >= 200 AND http_status < 300 AND (api_code = 0 OR api_code = 200) AND (error IS NULL OR error = ''))")
    elif status == "err":
        where.append("http_status = 0 OR http_status >= 400 OR (api_code != 0 AND api_code != 200) OR (error IS NOT NULL AND error != '')")

    where_clause = " AND ".join(where)

    count = conn.execute(
        f"SELECT COUNT(*) FROM api_call_log WHERE {where_clause}", params
    ).fetchone()[0]

    total_pages = max(1, (count + page_size - 1) // page_size)
    offset = (page - 1) * page_size

    rows = conn.execute(
        f"""SELECT id, source, method, http_status, api_code, api_sub_code,
                   substr(request_body, 1, 800) AS request_body,
                   substr(response_body, 1, 800) AS response_body,
                   duration_ms, error, created_at
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
                "error": r["error"] or "",
                "created_at": r["created_at"],
                "is_success": bool(
                    200 <= (r["http_status"] or 0) < 300
                    and (r["api_code"] or 0) in (0, 200)
                    and not (r["error"] or "")
                ),
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
    if state in ("done", "jky_cancelled", "cancelled"):
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


@router.get("/api/debug")
async def api_debug(request: Request):
    """调试：返回请求的 cookies 和 headers"""
    return {
        "cookies": dict(request.cookies),
        "has_session_cookie": "admin_session" in request.cookies,
    }


@router.get("/api/reconciliation")
async def api_reconciliation(request: Request, page: int = 1, page_size: int = 50):
    """三态对账：列出不一致/待处理的订单

    前置刷新：拉蓝盟近期已取消订单，更新 platform_state
    确保对账页面能看到蓝盟侧最新状态而非首次插入时的快照。
    """
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    page = max(1, page)
    page_size = min(200, max(10, page_size))
    offset = (page - 1) * page_size

    conn = get_connection()
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    # ---- 前置刷新：拉蓝盟近期已取消订单，更新 platform_state ----
    lanmong_actual = {}  # {orderNo: lanmong_state}
    lanmong = getattr(request.app.state, "lanmong_client", None)
    if lanmong:
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            resp = await lanmong.get_deliver_orders(
                state="-2",
                supplier_update_time_start=cutoff,
                supplier_update_time_end=now_str,
                page_size=200,
            )
            resp_data = resp.get("data", {})
            if isinstance(resp_data, dict):
                orders = resp_data.get("orderList", [])
            else:
                orders = resp_data if isinstance(resp_data, list) else []
            for o in orders:
                order_no = o.get("orderNo", "")
                st = o.get("state")
                if order_no and st is not None:
                    lanmong_actual[order_no] = st
            # 更新 DB 中停滞的 platform_state
            if lanmong_actual:
                db_check = conn.execute(
                    "SELECT id, platform_order_no, platform_state, state FROM order_map "
                    "WHERE updated_at >= ?", (cutoff,)
                ).fetchall()
                for r in db_check:
                    no = r["platform_order_no"]
                    if no in lanmong_actual and r["platform_state"] != lanmong_actual[no]:
                        conn.execute(
                            "UPDATE order_map SET platform_state = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (lanmong_actual[no], r["id"]),
                        )
                        logger.info(f"[recon] 刷新 {no} platform_state {r['platform_state']} → {lanmong_actual[no]}")
                conn.commit()
        except Exception as e:
            logger.warning(f"[recon] 蓝盟刷新失败（非致命）: {e}")

    # ---- 查询 DB：先算总数，再做分页查询 ----
    total_row = conn.execute(
        "SELECT COUNT(*) AS n FROM order_map WHERE updated_at >= ?",
        (cutoff,),
    ).fetchone()
    total = total_row["n"]

    rows = conn.execute(
        """SELECT id, platform_order_no, platform_state, jky_trade_no,
                  logistic_no, state, retry_count, last_error, updated_at,
                  platform_unified, jky_unified, jky_effective_unified, bridge_unified, jky_state,
                  alert_count, last_alert_level, last_alert_time, last_alert_message
           FROM order_map
           WHERE updated_at >= ?
           ORDER BY
             CASE
               WHEN state IN ('skipped','init','failed') THEN 0
               WHEN platform_state < 0 AND state NOT IN ('jky_cancelled','cancelled') THEN 1
               WHEN state IN ('jky_created','audited') THEN 2
               ELSE 3
             END,
             alert_count DESC,
             updated_at DESC
           LIMIT ? OFFSET ?""",
        (cutoff, page_size, offset),
    ).fetchall()

    orders = []
    summary = {"pending": 0, "inconsistent": 0, "terminal": 0}
    for r in rows:
        # 用实际蓝盟态（如有）替代 DB 中的 platform_state
        actual_lanmong = lanmong_actual.get(r["platform_order_no"], r["platform_state"])
        row_dict = dict(r)
        row_dict["platform_state"] = actual_lanmong

        flags, priority = _classify_drift(row_dict)
        # 推荐操作
        suggested = _suggest_action(r["state"], actual_lanmong, r["jky_trade_no"], r["logistic_no"])
        entry = {
            "id": r["id"],
            "platform_order_no": r["platform_order_no"],
            "platform_state": r["platform_state"],
            "jky_state": r["jky_state"],
            "jky_trade_no": r["jky_trade_no"],
            "logistic_no": r["logistic_no"],
            "state": r["state"],
            "retry_count": r["retry_count"],
            "last_error": r["last_error"],
            "updated_at": str(r["updated_at"]) if r["updated_at"] else None,
            "alert_count": r["alert_count"] or 0,
            "last_alert_level": r["last_alert_level"] or "",
            "last_alert_time": r["last_alert_time"] or "",
            "last_alert_message": r["last_alert_message"] or "",
            # 统一态字段
            "platform_unified": r["platform_unified"] or _lanmong_label(r["platform_state"]),
            "bridge_unified": r["bridge_unified"] or _bridge_label(r["state"]),
            "jky_unified": r["jky_effective_unified"] or r["jky_unified"] or "待发货",
            "consistent": (
                (r["platform_unified"] or _lanmong_label(r["platform_state"]))
                == (r["bridge_unified"] or _bridge_label(r["state"]))
                == (r["jky_effective_unified"] or r["jky_unified"] or "待发货")
            ),
        }
        orders.append(entry)
        if priority == "terminal":
            summary["terminal"] += 1
        else:
            summary["pending"] += 1
            if "lanmeng_cancel" in flags or "failed" in flags:
                summary["inconsistent"] += 1

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, -(-total // page_size)),
        "orders": orders,
        "summary": summary,
    }


# ---- 三端状态标签 & 一致性判断 ----

_LANMONG_LABELS = {
    1: "待审核", 2: "待发货", 4: "已发货",
    -2: "已取消", -3: "异常", -4: "退款",
}
_BRIDGE_LABELS = {
    "init": "初始", "failed": "失败",
    "jky_created": "已创单", "audited": "待发货",
    "jky_shipped": "JKY已发货", "synced": "已回传", "done": "已完成",
    "cancelled": "已取消", "jky_cancelled": "JKY已取消",
    "skipped": "跳过", "static": "静态",
}


def _lanmong_label(s) -> str:
    """蓝盟状态（数字 → 中文）"""
    try:
        return _LANMONG_LABELS.get(int(s), f"蓝盟({s})")
    except (ValueError, TypeError):
        return "未知"


def _bridge_label(s: str) -> str:
    """Bridge 状态（英文 → 中文）"""
    return _BRIDGE_LABELS.get(s, s)


def _jky_label(state: str, jky_trade_no, logistic_no) -> str:
    """吉客云状态推断"""
    if not jky_trade_no:
        return "未创建"
    if state in ("jky_cancelled", "cancelled"):
        return "已取消"
    if state in ("done", "synced"):
        return "已完成"
    if logistic_no or state in ("jky_shipped",):
        return "已发货"
    if state in ("jky_created", "audited"):
        return "待发货"
    return "已创建"



# ---- 推荐操作逻辑 ----

_ACTION_LABELS = {
    "pull-lanmong": "📥 从蓝盟重新拉取",
    "pull-jky": "📥 从吉客云重新拉取",
    "resubmit-jky": "🚀 重新提交到吉客云",
    "resubmit-lanmong": "🚀 回传蓝盟（物流同步）",
}


def _suggest_action(
    state: str,
    lanmong_state,
    jky_trade_no,
    logistic_no,
) -> str:
    """根据订单当前状态推荐默认操作"""
    # 缺 JKY 单 → 提交到吉客云
    if state in ("init", "failed", "audited", "skipped") and not jky_trade_no:
        return "resubmit-jky"

    # 蓝盟已取消但 bridge 未处理 → 从蓝盟拉取
    if lanmong_state is not None and lanmong_state < 0 and state not in ("jky_cancelled", "cancelled"):
        return "pull-lanmong"

    # 已创建/审核但未发货 → 从吉客云拉取查看状态
    if state in ("jky_created", "audited") and jky_trade_no:
        return "pull-jky"

    # 已闭环但蓝盟不是已发货 → 回传蓝盟
    if state == "done" and lanmong_state != 4:
        return "resubmit-lanmong"

    # 已发货但未回传 → 回传蓝盟
    if state in ("jky_shipped", "synced"):
        return "resubmit-lanmong"

    return "pull-lanmong"


# ---- 操作 Endpoints ----

@router.post("/api/reconciliation/pull-lanmong")
async def api_recon_pull_lanmong(request: Request):
    """从蓝盟重新拉取选中订单的状态"""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    body = await request.json()
    ids = body.get("ids", [])
    if not ids:
        return {"success": False, "error": "no_ids", "results": []}

    conn = get_connection()
    lanmong = getattr(request.app.state, "lanmong_client", None)
    results = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for order_id in ids:
        row = conn.execute(
            "SELECT id, platform_order_no, platform_state, state FROM order_map WHERE id = ?",
            (order_id,),
        ).fetchone()
        if not row:
            results.append({"id": order_id, "success": False, "action": "pull-lanmong", "msg": "未找到"})
            continue

        order_no = row["platform_order_no"]
        if not lanmong:
            results.append({"id": order_id, "success": False, "action": "pull-lanmong", "msg": "蓝盟客户端不可用"})
            continue

        try:
            resp = await lanmong.get_deliver_orders(order_no=order_no, state=None)
            resp_data = resp.get("data", {})
            orders_list = resp_data.get("orderList", []) if isinstance(resp_data, dict) else (
                resp_data if isinstance(resp_data, list) else [])
            if not orders_list:
                results.append({"id": order_id, "success": False, "action": "pull-lanmong", "msg": "蓝盟未返回该订单"})
                continue

            new_state = orders_list[0].get("state", row["platform_state"])
            from .core.shared_unified import platform_to_unified
            new_unified = platform_to_unified(new_state)
            conn.execute(
                "UPDATE order_map SET platform_state = ?, platform_unified = ?, updated_at = ? WHERE id = ?",
                (new_state, new_unified, now_str, order_id),
            )
            conn.commit()
            msg = f"蓝盟实际 state={new_state} → {new_unified}"
            if new_state < 0 and row["state"] not in ("jky_cancelled", "cancelled"):
                msg += "（蓝盟已取消，cron-c 将自动取消 JKY）"
            results.append({"id": order_id, "success": True, "action": "pull-lanmong", "msg": msg})
        except Exception as e:
            logger.exception(f"[pull-lanmong] {order_no} 失败: {e}")
            results.append({"id": order_id, "success": False, "action": "pull-lanmong", "msg": str(e)})

    return {"success": True, "results": results}


@router.post("/api/reconciliation/pull-jky")
async def api_recon_pull_jky(request: Request):
    """从吉客云重新拉取选中订单的状态"""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    body = await request.json()
    ids = body.get("ids", [])
    if not ids:
        return {"success": False, "error": "no_ids", "results": []}

    conn = get_connection()
    jky = getattr(request.app.state, "jky_client", None)
    results = []

    for order_id in ids:
        row = conn.execute(
            "SELECT id, platform_order_no, jky_trade_no, state FROM order_map WHERE id = ?",
            (order_id,),
        ).fetchone()
        if not row:
            results.append({"id": order_id, "success": False, "action": "pull-jky", "msg": "未找到"})
            continue

        trade_no = row["jky_trade_no"]
        if not trade_no:
            results.append({"id": order_id, "success": False, "action": "pull-jky", "msg": "该订单无 JKY 单号"})
            continue
        if not jky:
            results.append({"id": order_id, "success": False, "action": "pull-jky", "msg": "JKY 客户端不可用"})
            continue

        try:
            resp = await jky.trade_list({
                "tradeNos": trade_no,
                "fields": "tradeNo,onlineTradeNo,tradeStatus,tradeStatusExplain,mainPostid,logisticName",
            })
            if resp.get("code") != 200:
                results.append({"id": order_id, "success": False, "action": "pull-jky",
                                "msg": f"JKY 查询失败: {resp.get('msg', '')}"})
                continue
            data = resp.get("result", {}).get("data", {})
            trades = data.get("trades", data.get("list", data.get("rows", [])))
            if not trades:
                results.append({"id": order_id, "success": False, "action": "pull-jky", "msg": "JKY 未返回该订单"})
                continue
            t = trades[0]
            jky_status = t.get("tradeStatusExplain") or t.get("tradeStatus") or ""
            jky_postid = t.get("mainPostid") or ""
            results.append({
                "id": order_id, "success": True, "action": "pull-jky",
                "msg": f"JKY 状态: {jky_status}, 物流单号: {jky_postid or '无'}",
                "data": {"tradeNo": trade_no, "status": jky_status, "mainPostid": jky_postid},
            })
        except Exception as e:
            logger.exception(f"[pull-jky] {trade_no} 失败: {e}")
            results.append({"id": order_id, "success": False, "action": "pull-jky", "msg": str(e)})

    return {"success": True, "results": results}


@router.post("/api/reconciliation/resubmit-lanmong")
async def api_recon_resubmit_lanmong(request: Request):
    """回传蓝盟 — 触发物流同步"""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    body = await request.json()
    ids = body.get("ids", [])
    if not ids:
        return {"success": False, "error": "no_ids", "results": []}

    conn = get_connection()
    lanmong = getattr(request.app.state, "lanmong_client", None)
    jky = getattr(request.app.state, "jky_client", None)
    notifier = getattr(request.app.state, "notifier", None)
    results = []

    for order_id in ids:
        row = conn.execute(
            "SELECT id, platform_order_no, platform_order_id, jky_trade_no, state, "
            "logistic_no, order_items_json FROM order_map WHERE id = ?",
            (order_id,),
        ).fetchone()
        if not row:
            results.append({"id": order_id, "success": False, "action": "resubmit-lanmong", "msg": "未找到"})
            continue
        if not row["jky_trade_no"]:
            msg = "缺 JKY 单号"
            if notifier:
                record_admin_alert(conn, notifier, order_id, row["platform_order_no"],
                    "P1", "resubmit_lanmong_fail", msg)
            results.append({"id": order_id, "success": False, "action": "resubmit-lanmong", "msg": msg})
            continue
        if not row["logistic_no"]:
            msg = "缺物流单号，无法回传"
            if notifier:
                record_admin_alert(conn, notifier, order_id, row["platform_order_no"],
                    "P1", "resubmit_lanmong_fail", msg)
            results.append({"id": order_id, "success": False, "action": "resubmit-lanmong", "msg": msg})
            continue
        if not lanmong:
            msg = "蓝盟客户端不可用"
            if notifier:
                record_admin_alert(conn, notifier, order_id, row["platform_order_no"],
                    "P1", "resubmit_lanmong_fail", msg)
            results.append({"id": order_id, "success": False, "action": "resubmit-lanmong", "msg": msg})
            continue

        try:
            # 直接调蓝盟 syncOrderExpress 回传物流
            order_items = []
            if row["order_items_json"]:
                try:
                    products = json.loads(row["order_items_json"])
                    order_items = []
                    for p in products:
                        oiid = p.get("orderItemId")
                        if not oiid:
                            continue
                        item = {
                            "orderItemId": int(oiid),
                            "num": int(p.get("num") or p.get("number") or 1),
                        }
                        sku_no = p.get("skuNo")
                        sku_id = p.get("skuId")
                        if sku_no:
                            item["skuNo"] = str(sku_no)
                        elif sku_id:
                            item["skuId"] = int(sku_id)
                        order_items.append(item)
                except Exception:
                    pass

            if not order_items:
                msg = "无商品明细（order_items_json 为空或无有效 orderItemId）"
                if notifier:
                    record_admin_alert(conn, notifier, order_id, row["platform_order_no"],
                        "P2", "resubmit_lanmong_fail", msg)
                results.append({"id": order_id, "success": False, "action": "resubmit-lanmong",
                                "msg": msg})
                continue

            resp = await lanmong.sync_order_express(
                order_id=row["platform_order_id"] or 0,
                order_no=row["platform_order_no"],
                express_no=row["logistic_no"],
                express_code="STO",
                express_name="申通快递",
                warehouse_id=2,
                warehouse_name="一号仓",
                items=order_items,
            )

            if resp.get("code") == 0:
                fault_list = (resp.get("data") or {}).get("faultList") or []
                if not fault_list:
                    results.append({"id": order_id, "success": True, "action": "resubmit-lanmong",
                                    "msg": f"物流已回传蓝盟: {row['logistic_no']}"})
                else:
                    msg = f"回传局部失败: {fault_list[0].get('errorMsg','')}"
                    if notifier:
                        record_admin_alert(conn, notifier, order_id, row["platform_order_no"],
                            "P2", "resubmit_lanmong_fail", msg)
                    results.append({"id": order_id, "success": False, "action": "resubmit-lanmong",
                                    "msg": msg})
            else:
                msg = f"蓝盟回传失败: {resp.get('msg','')}"
                if notifier:
                    record_admin_alert(conn, notifier, order_id, row["platform_order_no"],
                        "P1", "resubmit_lanmong_fail", msg)
                results.append({"id": order_id, "success": False, "action": "resubmit-lanmong",
                                "msg": msg})
        except Exception as e:
            logger.exception(f"[resubmit-lanmong] {order_id} 失败: {e}")
            msg = str(e)
            if notifier:
                record_admin_alert(conn, notifier, order_id, row["platform_order_no"],
                    "P1", "resubmit_lanmong_fail", msg)
            results.append({"id": order_id, "success": False, "action": "resubmit-lanmong", "msg": msg})

    return {"success": True, "results": results}


@router.post("/api/reconciliation/resubmit")
async def api_reconciliation_resubmit(request: Request):
    """重新提交选中订单 — 根据当前状态执行相应恢复操作"""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
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
    conn = get_connection()
    notifier = getattr(app_state, "notifier", None)

    # Terminal states → skip
    if state in ("done", "jky_cancelled", "cancelled"):
        return {"success": False, "action": "skipped_terminal", "msg": "终态无需处理"}

    try:
        # Case 1: Lanmeng cancelled → cancel in JKY
        if platform_state is not None and platform_state < 0:
            if jky_trade_no:
                jky_direct = app_state.jky_direct
                if jky_direct:
                    resp = await jky_direct.trade_cancel(jky_trade_no, "420001")
                    if resp.get("code") != 200:
                        msg = f"JKY 取消失败: {resp.get('msg','')}"
                        if notifier:
                            record_admin_alert(conn, notifier, order_id,
                                platform_order_no, "P1", "cancel_fail", msg)
                        return {"success": False, "action": "cancel", "msg": msg}
                    transition(order_id, STATE_JKY_CANCELLED, "admin_resubmit")
                    if notifier:
                        record_admin_alert(conn, notifier, order_id,
                            platform_order_no, "P1", "cancel_success", "已取消 JKY", resolved=True)
                    return {"success": True, "action": "cancel",
                            "msg": f"已取消 JKY {jky_trade_no}"}
                else:
                    msg = "jky_direct 不可用"
                    if notifier:
                        record_admin_alert(conn, notifier, order_id,
                            platform_order_no, "P1", "cancel_fail", msg)
                    return {"success": False, "action": "cancel", "msg": msg}
            else:
                # No JKY trade → just mark cancelled
                transition(order_id, "cancelled", "admin_resubmit",
                           f"platform_state={platform_state}, 无 JKY 单")
                if notifier:
                    record_admin_alert(conn, notifier, order_id,
                        platform_order_no, "P1", "cancel_success", "标记取消", resolved=True)
                return {"success": True, "action": "mark_cancelled", "msg": "标记取消"}

        # Case 2: init or failed → re-create
        if state in ("init", "failed"):
            create_result = await _resubmit_create(row, app_state)
            if not create_result.get("success") and notifier:
                record_admin_alert(conn, notifier, order_id,
                    platform_order_no, "P1", "create_fail",
                    create_result.get("msg", "创单失败"))
            elif create_result.get("success") and notifier:
                record_admin_alert(conn, notifier, order_id,
                    platform_order_no, "P1", "create_success",
                    "创单成功", resolved=True)
            return create_result

        # Case 3: jky_created or audited → 已有 JKY 单, 通知用户在 JKY 后台手动审核
        if state in ("jky_created", "audited"):
            if jky_trade_no:
                return {"success": True, "action": "already_created",
                        "msg": f"JKY 单 {jky_trade_no} 已存在, 请在 JKY 后台手动审核"}
            else:
                # No JKY trade → re-create
                create_result = await _resubmit_create(row, app_state)
                if not create_result.get("success") and notifier:
                    record_admin_alert(conn, notifier, order_id,
                        platform_order_no, "P1", "create_fail",
                        create_result.get("msg", "创单失败"))
                return create_result

        # Case 4: jky_shipped or synced → already in pipeline
        return {"success": False, "action": "in_pipeline", "msg": f"状态 {state} 已在流程中"}

    except Exception as e:
        logger.exception(f"[resubmit] {order_id} 处理异常: {e}")
        transition(order_id, STATE_FAILED, "admin_resubmit", str(e))
        if notifier:
            record_admin_alert(conn, notifier, order_id,
                platform_order_no, "P1", "resubmit_error", str(e))
        return {"success": False, "action": "error", "msg": str(e)}


async def _resubmit_create(row: dict, app_state) -> dict:
    """Re-create: fetch from lanmong, create in JKY (不审核, 用户在 JKY 后台手动处理)"""
    from .core.state_machine import transition, STATE_AUDITED, STATE_FAILED, STATE_JKY_CREATED

    order_id = row["id"]
    platform_order_no = row["platform_order_no"]
    jky_trade_no = row["jky_trade_no"]
    order_items_json = row.get("order_items_json", "")

    jky_direct = app_state.jky_direct
    lanmong = app_state.lanmong_client
    notifier = getattr(app_state, "notifier", None)
    conn = get_connection()

    if not jky_direct or not lanmong:
        return {"success": False, "action": "create", "msg": "客户端不可用"}

    if jky_trade_no:
        # 已有 JKY 单 → 通知用户在 JKY 后台手动审核
        return {"success": True, "action": "already_created",
                "msg": f"JKY 单 {jky_trade_no} 已存在, 请在 JKY 后台手动审核"}

    # Step 1: Re-fetch order from lanmong by orderNo
    try:
        lanmong_resp = await lanmong.get_deliver_orders(order_no=platform_order_no, state=None)
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
    order_total = 0.0
    for item in products:
        product_no = item.get("productNo", "")
        qty = item.get("num") or item.get("number") or 1
        if not product_no:
            continue

        # YX 前缀转换：正式网站 YX 编码 → sku_mapping → jky_goods_no
        from ..core.sku_resolver import SkuResolver as _SkuResolver
        _resolver = _SkuResolver()
        jky_goods_no = product_no
        if product_no.startswith("YX"):
            resolved = _resolver.resolve(product_no)
            if not resolved:
                msg = f"{product_no} 无sku映射（应补 sku_mapping 表）"
                logger.warning(f"[resubmit] {platform_order_no} {msg}")
                return {"success": False, "action": "create", "msg": msg}
            jky_goods_no = resolved
            logger.info(f"[resubmit] {platform_order_no} YX映射: {product_no} → {jky_goods_no}")

        prod_row = conn.execute(
            "SELECT jky_barcode, jky_goods_name, raw_json FROM jky_product_cache WHERE jky_goods_no = ?",
            (jky_goods_no,),
        ).fetchone()
        if not prod_row or not prod_row["jky_barcode"]:
            logger.warning(f"[resubmit] {platform_order_no} {jky_goods_no} 无缓存或条码为空，跳过")
            return {"success": False, "action": "create",
                    "msg": f"货品 {jky_goods_no} 无缓存或条码为空，无法创单"}
        cost_price = float(item.get("costPrice", 0) or 0)
        # 从 JKY 商品缓存 raw_json 取 unitName（如 "瓶"/"套"/"件"），缺失/异常 → fail-closed 拒绝
        unit_name = None
        try:
            raw = prod_row["raw_json"] or ""
            if raw:
                raw_obj = json.loads(raw)
                if isinstance(raw_obj, dict):
                    un = raw_obj.get("unitName")
                    if isinstance(un, str) and un.strip():
                        unit_name = un.strip()
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError, ValueError):
            unit_name = None
        if not unit_name:
            logger.warning(f"[resubmit] {platform_order_no} {jky_goods_no} 缓存缺 unitName，无法创单")
            return {"success": False, "action": "create",
                    "msg": f"货品 {jky_goods_no} 缓存缺 unitName，无法创单"}
        sell_total = round(cost_price * qty, 2)
        order_total = (order_total or 0) + sell_total
        trade_order_details.append({
            "goodsNo": jky_goods_no,
            "barcode": prod_row["jky_barcode"],
            "goodsName": prod_row["jky_goods_name"] or "",
            "specName": "默认",
            "unit": unit_name,
            "sellPrice": cost_price,
            "sellCount": qty,
            "sellTotal": sell_total,
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
            "totalFee": order_total or 0,
            "payment": order_total or 0,
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

    try:
        create_resp = await jky_direct.trade_create(create_biz["tradeOrder"])
        jky_code = create_resp.get("code", -1)
        if jky_code != 200:
            return {"success": False, "action": "create",
                    "msg": f"JKY 创单失败: {create_resp.get('msg','')} (code={jky_code})"}
        new_trade_no = (create_resp.get("result", {})
                       .get("data", {})
                       .get("tradeOrder", {})
                       .get("tradeNo", ""))
        if not new_trade_no:
            msg = f"JKY 创单返回但缺 tradeNo: {json.dumps(create_resp, ensure_ascii=False)}"
            if notifier:
                record_admin_alert(conn, notifier, order_id,
                    platform_order_no, "P0", "create_missing_trade_no", msg)
            return {"success": False, "action": "create", "msg": msg}
        conn.execute(
            "UPDATE order_map SET jky_trade_no = ?, order_items_json = ? WHERE id = ?",
            (new_trade_no, json.dumps(products, ensure_ascii=False, default=str), order_id),
        )
        conn.commit()
    except Exception as e:
        transition(order_id, STATE_FAILED, "admin_resubmit", str(e))
        return {"success": False, "action": "create", "msg": f"JKY 创单异常: {e}"}

    transition(order_id, STATE_JKY_CREATED, "admin_resubmit")
    return {"success": True, "action": "create",
            "msg": f"创单成功: {new_trade_no}, 请在 JKY 后台手动审核"}


# ---- 对账日报 ----

@router.get("/api/reconciliation/reports")
async def api_recon_reports(request: Request, limit: int = Query(10, ge=1, le=90)):
    """列出最近的对账日报"""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    conn = get_connection()
    rows = conn.execute(
        """SELECT id, report_date, run_id, summary_json, deviations_json,
                  daily_trend_json, created_at
           FROM reconciliation_report
           ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return {
        "reports": [
            {
                "id": r["id"],
                "report_date": r["report_date"],
                "run_id": r["run_id"],
                "summary": json.loads(r["summary_json"]),
                "deviation_count": len(json.loads(r["deviations_json"] or "[]")),
                "created_at": str(r["created_at"]) if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/api/reconciliation/reports/{report_id}")
async def api_recon_report_detail(request: Request, report_id: int):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM reconciliation_report WHERE id = ?", (report_id,)
    ).fetchone()
    if not row:
        return {"error": "not found"}
    return dict(row)


@router.get("/api/alerts")
async def api_alerts(request: Request, order_id: int = None,
                     level: str = None, limit: int = Query(50, ge=1, le=200)):
    """查询告警日志"""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    conn = get_connection()
    where = []
    params = []
    if order_id is not None:
        where.append("order_id = ?"); params.append(order_id)
    if level:
        where.append("level = ?"); params.append(level)
    where_clause = " WHERE " + " AND ".join(where) if where else ""
    sql = f"SELECT * FROM alert_log{where_clause} ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return {"alerts": [dict(r) for r in rows]}


# ---- 用户管理 API（仅 admin 可用，require_admin 校验）----


@router.get("/api/users")
async def api_users_list(request: Request):
    """列出已授权的所有用户"""
    await require_admin(request)
    conn = get_connection()
    rows = conn.execute(
        "SELECT open_id, name, avatar, role, added_by, added_at FROM admin_users ORDER BY role DESC, added_at"
    ).fetchall()
    return [
        {
            "open_id": r["open_id"],
            "name": r["name"],
            "avatar": r["avatar"],
            "role": r["role"],
            "added_by": r["added_by"] or "",
            "added_at": str(r["added_at"]) if r["added_at"] else "",
        }
        for r in rows
    ]


@router.get("/api/users/pending")
async def api_users_pending(request: Request):
    """列出待审批用户"""
    await require_admin(request)
    conn = get_connection()
    rows = conn.execute(
        "SELECT open_id, name, avatar, created_at FROM pending_admin_users ORDER BY created_at DESC"
    ).fetchall()
    return [
        {
            "open_id": r["open_id"],
            "name": r["name"],
            "avatar": r["avatar"],
            "created_at": str(r["created_at"]) if r["created_at"] else "",
        }
        for r in rows
    ]


@router.post("/api/users/approve")
async def api_users_approve(request: Request):
    """批准待审批用户

    P0 #2 事务包裹: INSERT admin_users + DELETE pending 在一个事务内。
    """
    await require_admin(request)
    current_user = await require_admin(request)
    body = await request.json()
    open_id = body.get("open_id", "")
    if not open_id:
        return {"success": False, "error": "缺少 open_id"}

    conn = get_connection()
    try:
        conn.execute("BEGIN")
        # 查 pending 记录
        pending = conn.execute(
            "SELECT name, avatar FROM pending_admin_users WHERE open_id = ?",
            (open_id,),
        ).fetchone()
        if not pending:
            conn.execute("ROLLBACK")
            return {"success": False, "error": "该用户不在待审批列表中"}

        # INSERT admin_users
        conn.execute(
            "INSERT OR REPLACE INTO admin_users (open_id, name, avatar, role, added_by) VALUES (?, ?, ?, 'member', ?)",
            (open_id, pending["name"], pending["avatar"], current_user.get("name", "")),
        )

        # DELETE pending
        conn.execute(
            "DELETE FROM pending_admin_users WHERE open_id = ?",
            (open_id,),
        )
        conn.commit()
        logger.info(f"[admin] 管理员 {current_user.get('name')} 批准用户 {pending['name']} ({open_id[:16]}...)")
        return {"success": True}
    except Exception as e:
        conn.rollback()
        logger.exception(f"[admin] 批准用户失败: {e}")
        return {"success": False, "error": str(e)}


@router.post("/api/users/reject")
async def api_users_reject(request: Request):
    """拒绝待审批用户（P1: 审计日志）"""
    await require_admin(request)
    current_user = await require_admin(request)
    body = await request.json()
    open_id = body.get("open_id", "")
    if not open_id:
        return {"success": False, "error": "缺少 open_id"}

    conn = get_connection()
    pending = conn.execute(
        "SELECT name FROM pending_admin_users WHERE open_id = ?",
        (open_id,),
    ).fetchone()
    if not pending:
        return {"success": False, "error": "该用户不在待审批列表中"}

    name = pending["name"]
    conn.execute("DELETE FROM pending_admin_users WHERE open_id = ?", (open_id,))
    conn.commit()
    logger.warning(f"[admin] 管理员 {current_user.get('name')} 拒绝用户 {name} ({open_id[:16]}...) 的审批申请")
    return {"success": True}


@router.delete("/api/users/{open_id}")
async def api_users_delete(request: Request, open_id: str):
    """删除已授权用户

    P0 #3: 不能删自己
    P2 #6: 同时清除该用户所有活跃 session
    """
    await require_admin(request)
    current_user = await require_admin(request)

    if open_id == current_user["open_id"]:
        return {"success": False, "error": "不能删除自己的账号"}

    conn = get_connection()
    user = conn.execute(
        "SELECT open_id, name, role FROM admin_users WHERE open_id = ?",
        (open_id,),
    ).fetchone()
    if not user:
        return {"success": False, "error": "未找到该用户"}

    if user["role"] == "admin":
        return {"success": False, "error": "不能删除管理员账号"}

    name = user["name"]

    # P2 #6: 清除该用户所有活跃 session，使其立即失效
    sessions_cleared = conn.execute(
        "DELETE FROM sessions WHERE user_open_id = ?",
        (open_id,),
    ).rowcount
    conn.execute("DELETE FROM admin_users WHERE open_id = ?", (open_id,))
    conn.commit()

    logger.warning(
        f"[admin] 管理员 {current_user.get('name')} 移除用户 {name} "
        f"({open_id[:16]}...), 已清除 {sessions_cleared} 个 session"
    )
    return {"success": True, "sessions_cleared": sessions_cleared}
