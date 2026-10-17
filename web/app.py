import os
import secrets
import json
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from core.database import (
    fetch_dashboard_stats, fetch_novels_paginated,
    update_novel_details, bulk_classify_novels, DB_PATH
)
import aiosqlite

load_dotenv()

app = FastAPI(title="Celestial Novel Archive Dashboard", docs_url=None, redoc_url=None)

BASE_DIR = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

DASHBOARD_PASSWORD = str(os.getenv("DASHBOARD_PASSWORD", "cultivation123")).strip()
AUTH_COOKIE_NAME = "sentinel_session_token"
ACTIVE_SESSIONS = set()

discord_bot = None

def verify_authenticated(request: Request):
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token or token not in ACTIVE_SESSIONS:
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return True

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})

@app.post("/login")
async def handle_login(request: Request, response: Response, password: str = Form(None)):
    if not password:
        try:
            body = await request.json()
            password = body.get("password")
        except Exception:
            password = None

    if password and str(password).strip() == DASHBOARD_PASSWORD:
        token = secrets.token_hex(32)
        ACTIVE_SESSIONS.add(token)
        res = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        res.set_cookie(key=AUTH_COOKIE_NAME, value=token, httponly=True, samesite="lax", max_age=86400 * 7)
        return res

    return templates.TemplateResponse(request=request, name="login.html", context={"error": "Invalid Sovereign Access Key!"})

@app.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request, authenticated: bool = Depends(verify_authenticated)):
    stats = await fetch_dashboard_stats()
    guilds = [{"id": g.id, "name": g.name} for g in discord_bot.guilds] if discord_bot else []
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"stats": stats, "guilds": guilds})

@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if token in ACTIVE_SESSIONS:
        ACTIVE_SESSIONS.remove(token)
    res = RedirectResponse(url="/login")
    res.delete_cookie(AUTH_COOKIE_NAME)
    return res

@app.get("/api/novels")
async def api_get_novels(
    search: str = "",
    reading_status: str = "ALL",
    theme: str = "ALL",
    domain: str = "ALL",
    page: int = 1,
    authenticated: bool = Depends(verify_authenticated)
):
    offset = (page - 1) * 20
    query = """
        SELECT n.*, COUNT(s.id) as link_count,
               GROUP_CONCAT(DISTINCT s.domain) as domains
        FROM novels n
        LEFT JOIN novel_sources s ON n.id = s.novel_id
        WHERE 1=1
    """
    params = []
    if search:
        query += " AND (n.title LIKE ? OR n.canonical_slug LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    if reading_status != "ALL":
        query += " AND n.reading_status = ?"
        params.append(reading_status)
    if theme != "ALL":
        query += " AND (n.theme = ? OR n.category = ?)"
        params.extend([theme, theme])
    if domain != "ALL":
        query += " AND s.domain = ?"
        params.append(domain)

    query += " GROUP BY n.id ORDER BY n.id DESC LIMIT ? OFFSET ?"
    params.extend([20, offset])

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            novels = [dict(r) for r in await cur.fetchall()]

        for novel in novels:
            async with db.execute("SELECT * FROM novel_sources WHERE novel_id = ?", (novel["id"],)) as scur:
                novel["sources"] = [dict(s) for s in await scur.fetchall()]

    return {"novels": novels, "page": page}

@app.post("/api/novel/{novel_id}/update")
async def api_update_novel(novel_id: int, payload: dict, authenticated: bool = Depends(verify_authenticated)):
    title = payload.get("title")
    reading_status = payload.get("reading_status")
    theme = payload.get("theme")
    is_fav = 1 if payload.get("is_favorite") else 0
    is_comp = 1 if payload.get("is_completed") else 0

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE novels
            SET title = ?, reading_status = ?, theme = ?, category = ?,
                is_favorite = ?, is_completed = ?, status = 'reviewed', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (title.strip(), reading_status, theme, theme, is_fav, is_comp, novel_id))
        await db.commit()
    return {"success": True}

@app.post("/api/novels/bulk-classify")
async def api_bulk_classify(payload: dict, authenticated: bool = Depends(verify_authenticated)):
    novel_ids = payload.get("novel_ids", [])
    reading_status = payload.get("reading_status")
    theme = payload.get("theme")

    if not novel_ids:
        raise HTTPException(status_code=400, detail="Missing novel_ids")

    async with aiosqlite.connect(DB_PATH) as db:
        placeholders = ",".join("?" for _ in novel_ids)
        await db.execute(f"""
            UPDATE novels
            SET reading_status = ?, theme = ?, category = ?, status = 'reviewed', updated_at = CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
        """, [reading_status, theme, theme] + novel_ids)
        await db.commit()
    return {"success": True, "count": len(novel_ids)}

@app.get("/api/builder/preflight/{guild_id}")
async def api_preflight(guild_id: int, authenticated: bool = Depends(verify_authenticated)):
    guild = discord_bot.get_guild(guild_id) if discord_bot else None
    if not guild:
        raise HTTPException(status_code=404, detail="Destination guild not connected to bot")
    from core.builder import ServerBuilder
    builder = ServerBuilder(discord_bot, guild)
    audit = await builder.preflight_audit()
    return audit