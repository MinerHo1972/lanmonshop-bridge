"""飞书 OAuth 登录 + Session 管理（管理后台身份验证）"""

import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse

from .config import load_credentials

logger = logging.getLogger(__name__)

# 飞书 OAuth 凭证 — 优先从 credentials.yaml 读取，兜底环境变量
_creds = load_credentials().get("feishu", {}) or {}
APP_ID = _creds.get("app_id") or os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = _creds.get("app_secret") or os.environ.get("FEISHU_APP_SECRET", "")

# 回调地址（需在飞书开发者后台配置）
CALLBACK_URL = "https://bridge.minerho1972.ccwu.cc/admin/auth/callback"

# Session 配置
SESSION_TTL = timedelta(hours=24)
SESSION_COOKIE = "admin_session"

# ---------- Session Store ----------

_sessions: dict[str, dict] = {}


def _clean_expired():
    """清理过期 session"""
    now = datetime.now()
    expired = [k for k, v in _sessions.items() if now > v["expires_at"]]
    for k in expired:
        del _sessions[k]


async def get_current_user(request: Request) -> Optional[dict]:
    """从 Cookie 中获取当前登录用户信息"""
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return None
    session = _sessions.get(session_id)
    if not session:
        return None
    if datetime.now() > session["expires_at"]:
        del _sessions[session_id]
        return None
    return session["user"]


async def require_api_auth(request: Request):
    """API 路由用: 未登录返回 401"""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


# ---------- 登录页面 ----------

LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bridge Admin - 登录</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#c9d1d9;height:100vh;display:flex;align-items:center;justify-content:center}
  .card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:40px;text-align:center;max-width:400px;width:90%}
  .logo{font-size:48px;margin-bottom:16px}
  h1{color:#f0f6fc;font-size:20px;margin-bottom:8px}
  p{color:#8b949e;font-size:13px;margin-bottom:24px;line-height:1.5}
  .btn{display:inline-flex;align-items:center;gap:8px;background:#238636;color:#fff;border:none;padding:12px 32px;border-radius:8px;font-size:15px;cursor:pointer;text-decoration:none;transition:background .2s}
  .btn:hover{background:#2ea043}
  .btn svg{width:20px;height:20px}
  .error{background:#4d1a1a;color:#f85149;padding:10px;border-radius:6px;font-size:12px;margin-bottom:16px;display:none}
</style>
</head>
<body>
<div class="card">
  <div class="logo">🔧</div>
  <h1>Bridge Admin</h1>
  <p>蓝盟-吉客云桥接服务<br>管理后台</p>
  <div id="error-msg" class="error"></div>
  <a class="btn" href="/admin/auth/feishu">
    <svg viewBox="0 0 32 32" fill="none"><path d="M6 4h20a2 2 0 012 2v20a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2z" fill="currentColor" opacity=".15"/><path d="M8 8l6 6-6 6M16 22h8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
    飞书账号登录
  </a>
  <p style="margin-top:20px;font-size:11px;color:#484f58">仅限授权管理员登录</p>
</div>
<script>
  const params=new URLSearchParams(window.location.search);
  if(params.get('error'))document.getElementById('error-msg').style.display='block',document.getElementById('error-msg').textContent=params.get('error');
</script>
</body>
</html>"""


# ---------- Router ----------

router = APIRouter(prefix="/admin/auth")


@router.get("/login")
async def login_page(request: Request):
    """登录页面 — 已登录则跳转回 admin"""
    user = await get_current_user(request)
    if user:
        return RedirectResponse(url="/admin")
    return HTMLResponse(LOGIN_HTML)


@router.get("/feishu")
async def feishu_login():
    """重定向到飞书 OAuth 授权页"""
    authorize_url = (
        "https://open.feishu.cn/open-apis/authen/v1/authorize"
        f"?app_id={APP_ID}&redirect_uri={CALLBACK_URL}"
    )
    return RedirectResponse(authorize_url)


@router.get("/callback")
async def auth_callback(code: str, request: Request):
    """飞书 OAuth 回调 — 兑换 token → 获取用户信息 → 创建 session"""
    # 1. 兑换 access_token
    token_url = "https://open.feishu.cn/open-apis/authen/v1/access_token"
    token_body = {
        "app_id": APP_ID,
        "app_secret": APP_SECRET,
        "grant_type": "authorization_code",
        "code": code,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token_resp = await client.post(token_url, json=token_body)
            token_data = token_resp.json()
    except Exception as e:
        logger.error(f"[auth] token 兑换失败: {e}")
        return RedirectResponse(url="/admin/auth/login?error=登录验证失败，请重试")

    if token_data.get("code") != 0:
        err_msg = token_data.get("msg", "未知错误")
        logger.error(f"[auth] token 兑换异常: {err_msg}")
        return RedirectResponse(url=f"/admin/auth/login?error=登录验证失败: {err_msg}")

    access_token = token_data.get("data", {}).get("access_token", "")
    if not access_token:
        return RedirectResponse(url="/admin/auth/login?error=缺少 access_token")

    # 2. 获取用户信息
    user_url = "https://open.feishu.cn/open-apis/authen/v1/user_info"
    user_headers = {"Authorization": f"Bearer {access_token}"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            user_resp = await client.get(user_url, headers=user_headers)
            user_data = user_resp.json()
    except Exception as e:
        logger.error(f"[auth] 用户信息获取失败: {e}")
        return RedirectResponse(url="/admin/auth/login?error=用户信息获取失败")

    if user_data.get("code") != 0:
        err_msg = user_data.get("msg", "未知错误")
        logger.error(f"[auth] 用户信息异常: {err_msg}")
        return RedirectResponse(url=f"/admin/auth/login?error=用户信息获取失败: {err_msg}")

    user_info = user_data.get("data", {})

    # 3. 创建 session
    _clean_expired()
    session_id = secrets.token_hex(32)
    _sessions[session_id] = {
        "user": {
            "open_id": user_info.get("open_id", ""),
            "union_id": user_info.get("union_id", ""),
            "name": user_info.get("name", ""),
            "avatar": user_info.get("avatar_url", ""),
        },
        "expires_at": datetime.now() + SESSION_TTL,
        "created_at": datetime.now(),
    }

    logger.info(f"[auth] 用户 {user_info.get('name')} 登录成功 (session={session_id[:8]}...)")

    # 4. 设置 cookie 并重定向
    resp = RedirectResponse(url="/admin")
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=session_id,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=False,  # CF Tunnel 内部链路可能非 HTTPS，secure=False 避免 cookie 被浏览器拦截
        samesite="lax",
        path="/admin",
    )
    return resp


@router.post("/logout")
async def logout(request: Request):
    """退出登录"""
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id and session_id in _sessions:
        del _sessions[session_id]
        logger.info(f"[auth] 用户退出登录 (session={session_id[:8]}...)")
    resp = RedirectResponse(url="/admin/auth/login")
    resp.delete_cookie(key=SESSION_COOKIE, path="/admin")
    return resp
