"""
Orbit sync server (personal use).

You type your NetID, password and the portal's CAPTCHA inside Orbit. This server opens the
real portal login page in a headless browser, logs in as you, copies the attendance and
internal-marks tables you can already see, and sends them back to Orbit.

- The CAPTCHA is solved by YOU. Nothing here reads or beats it.
- Your password is held in memory for the one request, then dropped. It is never written
  to disk and never logged.
- Every request needs your private access key (ACCESS_KEY).
"""
import asyncio
import base64
import os
import secrets
import time

from playwright.async_api import async_playwright
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

PORTAL = os.environ.get("PORTAL_URL", "https://sp.srmist.edu.in")
ACCESS_KEY = os.environ.get("ACCESS_KEY", "")
ORIGINS = [o.strip() for o in os.environ.get("ALLOW_ORIGIN", "https://antonybescki.github.io").split(",") if o.strip()]
PAGES = [p.strip() for p in os.environ.get("PORTAL_PAGES", "Attendance Details,Internal Mark Details").split(",") if p.strip()]
SESSION_TTL = 300
MAX_SESSIONS = 3

TABLES_JS = """
() => Array.from(document.querySelectorAll('table'))
  .filter(t => t.getClientRects().length && !t.querySelector('table'))
  .map(t => ({ rows: Array.from(t.rows).map(r => Array.from(r.cells).map(c => (c.innerText || '').replace(/\\s+/g, ' ').trim())) }))
"""

state = {"pw": None, "browser": None, "sessions": {}}
lock = asyncio.Lock()


def authorised(request):
    sent = request.headers.get("x-access-key", "")
    return bool(ACCESS_KEY) and secrets.compare_digest(sent, ACCESS_KEY)


async def get_browser():
    if state["browser"] is None:
        state["pw"] = await async_playwright().start()
        state["browser"] = await state["pw"].chromium.launch(args=["--no-sandbox"])
    return state["browser"]


async def close_session(sid):
    s = state["sessions"].pop(sid, None)
    if s:
        try:
            await s["ctx"].close()
        except Exception:
            pass


async def sweep():
    now = time.time()
    for sid in [k for k, v in state["sessions"].items() if now - v["ts"] > SESSION_TTL]:
        await close_session(sid)


async def captcha_png(page):
    """Picture of the CAPTCHA: the image next to the Captcha box, else the whole login card."""
    img = page.locator(
        'xpath=//input[contains(translate(@placeholder,"CAPTCHA","captcha"),"captcha")]/ancestor::*[.//img][1]//img'
    ).first
    try:
        await img.wait_for(state="visible", timeout=8000)
        return await img.screenshot()
    except Exception:
        card = page.locator("form").first
        try:
            return await card.screenshot()
        except Exception:
            return await page.screenshot()


async def fresh_captcha(sid):
    s = state["sessions"][sid]
    page = s["page"]
    await page.goto(PORTAL, wait_until="domcontentloaded", timeout=45000)
    await page.get_by_placeholder("NetID").wait_for(state="visible", timeout=20000)
    await page.wait_for_timeout(800)
    return base64.b64encode(await captcha_png(page)).decode()


async def health(request):
    return JSONResponse({"ok": True})


async def start(request):
    if not authorised(request):
        return JSONResponse({"error": "Wrong access key."}, status_code=401)
    async with lock:
        await sweep()
        if len(state["sessions"]) >= MAX_SESSIONS:
            oldest = min(state["sessions"], key=lambda k: state["sessions"][k]["ts"])
            await close_session(oldest)
        browser = await get_browser()
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()
        sid = secrets.token_urlsafe(16)
        state["sessions"][sid] = {"ctx": ctx, "page": page, "ts": time.time()}
    try:
        png = await fresh_captcha(sid)
    except Exception:
        await close_session(sid)
        return JSONResponse({"error": "Could not open the portal login page. Try again in a minute."}, status_code=502)
    return JSONResponse({"session": sid, "captcha": png})


async def refresh(request):
    if not authorised(request):
        return JSONResponse({"error": "Wrong access key."}, status_code=401)
    body = await request.json()
    sid = body.get("session", "")
    if sid not in state["sessions"]:
        return JSONResponse({"error": "Session expired. Start again."}, status_code=410)
    state["sessions"][sid]["ts"] = time.time()
    try:
        return JSONResponse({"session": sid, "captcha": await fresh_captcha(sid)})
    except Exception:
        return JSONResponse({"error": "Could not reload the CAPTCHA."}, status_code=502)


async def login(request):
    if not authorised(request):
        return JSONResponse({"error": "Wrong access key."}, status_code=401)
    body = await request.json()
    sid = body.get("session", "")
    s = state["sessions"].get(sid)
    if not s:
        return JSONResponse({"error": "Session expired. Start again."}, status_code=410)
    s["ts"] = time.time()
    netid = str(body.get("netid", "")).strip().split("@")[0]
    password = str(body.get("password", ""))
    captcha = str(body.get("captcha", "")).strip()
    body = None  # drop the request body as early as possible
    page = s["page"]
    if not netid or not password or not captcha:
        return JSONResponse({"error": "Enter NetID, password and the CAPTCHA."}, status_code=400)
    try:
        await page.get_by_placeholder("NetID").fill(netid)
        await page.get_by_placeholder("Password").fill(password)
        await page.get_by_placeholder("Captcha").fill(captcha)
        password = None
        await page.get_by_role("button", name="Login").click()
        menu = page.get_by_text(PAGES[0], exact=False).first
        try:
            await menu.wait_for(state="visible", timeout=25000)
        except Exception:
            # Still on the login page: wrong CAPTCHA or password. Give a new CAPTCHA.
            msg = "Login did not go through. Check the CAPTCHA and your password, then try again."
            try:
                alert = await page.locator(".alert, .error, .text-danger, [role=alert]").first.inner_text(timeout=1500)
                if alert.strip():
                    msg = alert.strip()[:200]
            except Exception:
                pass
            shot = ""
            try:
                shot = base64.b64encode(await page.screenshot(type="jpeg", quality=55)).decode()
            except Exception:
                pass
            try:
                return JSONResponse({"error": msg, "shot": shot, "session": sid, "captcha": await fresh_captcha(sid)}, status_code=401)
            except Exception:
                await close_session(sid)
                return JSONResponse({"error": msg}, status_code=401)

        tables = []
        for label in PAGES:
            link = page.get_by_text(label, exact=False).first
            await link.click()
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(1500)
            tables += await page.evaluate(TABLES_JS)
        try:
            await page.get_by_text("Logout", exact=False).first.click(timeout=3000)
        except Exception:
            pass
        await close_session(sid)
        return JSONResponse({"orbitPortal": 1, "tables": tables})
    except Exception:
        await close_session(sid)
        return JSONResponse({"error": "Something went wrong while reading the portal. Start again."}, status_code=502)


app = Starlette(
    routes=[
        Route("/health", health),
        Route("/session", start, methods=["POST"]),
        Route("/refresh", refresh, methods=["POST"]),
        Route("/login", login, methods=["POST"]),
    ],
    middleware=[Middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["GET", "POST"], allow_headers=["x-access-key", "content-type"])],
)
