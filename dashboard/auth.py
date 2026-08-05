"""
Authentication: PBKDF2-SHA256 password + HMAC-signed session cookie.
Session tokens are stored in DashboardState.sessions (in-memory, 24h TTL).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import Optional

from aiohttp import web

logger = logging.getLogger(__name__)

SESSION_COOKIE = "session"
SESSION_TTL = 900  # 15 minutes idle


def verify_password(password: str, hash_str: str) -> bool:
    """
    Verify a password against a stored hash.
    Hash format: pbkdf2:sha256:<iterations>:<salt_hex>:<dk_hex>
    Generate with: python3 -m dashboard.setup_password
    """
    if not hash_str:
        return False
    try:
        parts = hash_str.split(":")
        if len(parts) != 5 or parts[0] != "pbkdf2":
            return False
        _, algo, iterations_str, salt_hex, stored_hex = parts
        iterations = int(iterations_str)
        dk = hashlib.pbkdf2_hmac(
            algo, password.encode("utf-8"), salt_hex.encode("utf-8"), iterations
        )
        return hmac.compare_digest(dk.hex(), stored_hex)
    except Exception:
        return False


@web.middleware
async def session_middleware(request: web.Request, handler):
    """Redirect unauthenticated requests to /login."""
    public_paths = {"/login", "/favicon.ico"}
    if request.path in public_paths or request.path.startswith("/static/"):
        return await handler(request)

    token = request.cookies.get(SESSION_COOKIE)
    state = request.app["state"]

    if not token or not state.validate_session(token):
        if request.path.startswith("/api/"):
            raise web.HTTPUnauthorized(reason="Not authenticated")
        raise web.HTTPFound("/login")

    return await handler(request)


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Login — Daily News Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-900 flex items-center justify-center min-h-screen">
  <div class="bg-gray-800 rounded-xl p-8 w-full max-w-sm shadow-2xl">
    <h1 class="text-white text-2xl font-bold mb-6 text-center">Daily News</h1>
    {error}
    <form method="POST" action="/login">
      <div class="mb-4">
        <label class="text-gray-400 text-sm block mb-1">Password</label>
        <input type="password" name="password" autofocus
               class="w-full bg-gray-700 text-white rounded px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500" />
      </div>
      <button type="submit"
              class="w-full bg-blue-600 hover:bg-blue-500 text-white rounded py-2 font-medium transition">
        Sign in
      </button>
    </form>
  </div>
</body>
</html>"""


async def login_get(request: web.Request) -> web.Response:
    return web.Response(
        text=_LOGIN_HTML.format(error=""),
        content_type="text/html",
    )


async def login_post(request: web.Request) -> web.Response:
    data = await request.post()
    password = data.get("password", "")
    config = request.app["config_holder"].current
    state = request.app["state"]

    if verify_password(password, config.dashboard.password_hash):
        token = secrets.token_hex(32)
        state.add_session(token, SESSION_TTL)
        response = web.HTTPFound("/")
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="Strict",
            max_age=SESSION_TTL,
        )
        return response

    error_html = '<p class="text-red-400 text-sm text-center mb-4">Invalid password</p>'
    return web.Response(
        text=_LOGIN_HTML.format(error=error_html),
        content_type="text/html",
        status=401,
    )


async def logout(request: web.Request) -> web.Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        request.app["state"].remove_session(token)
    response = web.HTTPFound("/login")
    response.del_cookie(SESSION_COOKIE)
    return response
