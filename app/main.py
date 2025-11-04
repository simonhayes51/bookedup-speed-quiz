# app/main.py
from __future__ import annotations
import os, time, json, traceback, logging
from typing import Optional
from contextlib import contextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from passlib.hash import bcrypt_sha256 as hasher
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .room_manager import RoomManager, Team, Answer

log = logging.getLogger("bookedup")

# ------------------ App & assets ------------------
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY", "dev-secret-change"))

BASE = os.path.dirname(__file__)
static_dir = os.path.join(BASE, "static")
templates_dir = os.path.join(BASE, "templates")
app.mount("/static", StaticFiles(directory=static_dir), name="static")
env = Environment(loader=FileSystemLoader(templates_dir), autoescape=select_autoescape())

# ------------------ DB boot: prefer PG, fallback to SQLite ------------------
USE_PG = False
get_conn = None

def _boot_db():
    """
    Try Postgres if DATABASE_URL is set AND usable; else fall back to SQLite.
    This prevents 502s due to startup crashes.
    """
    global USE_PG, get_conn
    from importlib import import_module

    db_url = os.getenv("DATABASE_URL")
    if db_url:
        try:
            log.warning("[DB] Attempting Postgres via psycopg_pool")
            db_pg = import_module("app.db_pg")
            db_pg.init_db()
            from app.db_pg import get_conn as _pg_get_conn
            get_conn = _pg_get_conn
            USE_PG = True
            log.warning("[DB] Using Postgres")
            return
        except Exception as e:
            log.error("[DB] Postgres init failed; falling back to SQLite")
            log.error("Reason: %s", e)
            log.debug("Traceback:\n%s", traceback.format_exc())

    # Fallback to SQLite
    log.warning("[DB] Using SQLite fallback")
    db_sqlite = import_module("app.db")
    _sqlite_conn = db_sqlite.init_db()

    @contextmanager
    def _sqlite_get_conn():
        yield _sqlite_conn

    get_conn = _sqlite_get_conn
    USE_PG = False

_boot_db()

manager = RoomManager()

def current_user(request: Request):
    return request.session.get("user")

def require_role(request: Request, role: Optional[str] = None):
    user = current_user(request)
    if not user:
        return None
    if role and user["role"] != role:
        return None
    return user

# ------------------ Health & routes inspector ------------------
@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    # touch DB quickly
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        return "ok"
    except Exception as e:
        return PlainTextResponse(f"db-fail: {e}", status_code=500)

@app.get("/__routes")
def list_routes():
    return [{"path": r.path, "name": getattr(r, "name", None), "methods": list(getattr(r, "methods", []))}
            for r in app.routes]

# ------------------ Admin auto-seed via env ------------------
def ensure_admin_from_env():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1")
        exists = cur.fetchone()
        if exists:
            return
        email = os.getenv("ADMIN_EMAIL")
        password = os.getenv("ADMIN_PASSWORD")
        name = os.getenv("ADMIN_NAME", "Admin")
        if email and password:
            sql = "INSERT INTO users(name,email,password_hash,role) VALUES(%s,%s,%s,%s)" if USE_PG \
                  else "INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)"
            cur.execute(sql, (name, email, hasher.hash(password), "admin"))
            conn.commit()
            log.warning("[INIT] Admin created: %s", email)

# ------------------ Startup: load quizzes + seed admin ------------------
@app.on_event("startup")
def startup_load():
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, title, data_json FROM quizzes")
            rows = cur.fetchall()
        wrapped = [{"id": r[0], "title": r[1], "data_json": r[2]} for r in rows]
        manager.load_quizzes(wrapped)
    except Exception as e:
        log.error("[Startup] Failed to load quizzes: %s", e)
        log.debug("Traceback:\n%s", traceback.format_exc())

    try:
        ensure_admin_from_env()
    except Exception as e:
        log.error("[Startup] ensure_admin_from_env failed: %s", e)
        log.debug("Traceback:\n%s", traceback.format_exc())

# ------------------ Pages ------------------
@app.get("/")
def home():
    return HTMLResponse(env.get_template("home.html").render())

@app.get("/host")
def host_console(request: Request):
    user = require_role(request, "host")
    if not user:
        return RedirectResponse("/admin/login?next=/host", status_code=302)
    return HTMLResponse(env.get_template("host_gate.html").render())

# ------------------ API ------------------
@app.get("/api/me")
def api_me(request: Request):
    return current_user(request) or {}

@app.get("/api/my_venues")
def my_venues(request: Request):
    user = require_role(request, "host")
    if not user:
        return JSONResponse({"error":"unauthenticated"}, status_code=401)
    with get_conn() as conn:
        cur = conn.cursor()
        sql = f"""
            SELECT v.id, v.name, v.logo_url
            FROM venues v JOIN hosts_venues hv ON hv.venue_id = v.id
            WHERE hv.host_id = {'%s' if USE_PG else '?'}
        """
        cur.execute(sql, (user["id"],))
        rows = cur.fetchall()
    return [{"id": r[0], "name": r[1], "logo_url": r[2]} for r in rows]

@app.post("/api/create_room")
def create_room(request: Request, venue_id: int = Form(...), venue_title: str = Form(""), venue_logo: str = Form("")):
    user = require_role(request, "host")
    if not user:
        return JSONResponse({"error":"unauthenticated"}, status_code=401)
    room = manager.create_room(user["id"], venue_title, venue_logo, venue_id)
    return {"roomId": room.id}

@app.get("/api/quizzes")
def list_quizzes():
    return manager.list_quizzes()

@app.get("/api/export/{room_id}")
def export_scores(room_id: str):
    room = manager.get_room(room_id)
    if not room:
        return JSONResponse({"error":"Room not found"}, status_code=404)
    import io, csv
    output = io.StringIO(); writer = csv.writer(output)
    writer.writerow(["Team","Score"])
    for t in sorted(room.teams.values(), key=lambda x: x.score, reverse=True):
        writer.writerow([t.name, t.score])
    return Response(output.getvalue().encode("utf-8"), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={room_id}_scores.csv"})

# ------------------ WebSocket hub ------------------
@app.websocket("/ws")
async def ws(ws: WebSocket):
    await ws.accept()
    try:
        init = await ws.receive_json()
        role = init.get("role"); room_id = init.get("roomId")
        room = None

        if role == "host":
            if not room_id: await ws.send_json({"type":"error","message":"missing roomId"}); await ws.close(); return
            room = manager.get_room(room_id)
            if not room: await ws.send_json({"type":"error","message":"room not found"}); await ws.close(); return
            room.host_connections.append(ws)
            await ws.send_json({"type":"room:init","roomId":room.id,"state":room.state})

        elif role == "team":
            if not room_id: await ws.send_json({"type":"error","message":"missing roomId"}); await ws.close(); return
            room = manager.get_room(room_id)
            if not room: await ws.send_json({"type":"error","message":"room not found"}); await ws.close(); return
            team_name = init.get("teamName","Team")
            team_id = f"t{int(time.time()*1000)%100000}_{len(room.teams)+1}"
            room.teams[team_id] = Team(id=team_id, name=team_name, score=0)
            room.team_connections[team_id] = ws
            await manager.broadcast(room, {"type":"teams:update","teams":[{"id":t.id,"name":t.name,"score":t.score} for t in room.teams.values()]})
            await ws.send_json({"type":"team:joined","teamId":team_id,"roomId":room.id})

        elif role == "display":
            if not room_id: await ws.send_json({"type":"error","message":"missing roomId"}); await ws.close(); return
            room = manager.get_room(room_id)
            if not room: await ws.send_json({"type":"error","message":"room not found"}); await ws.close(); return
            room.display_connections.append(ws)
            await ws.send_json({"type":"branding","venueTitle":room.venue_title,"venueLogo":room.venue_logo})

        else:
            await ws.send_json({"type":"error","message":"invalid role"}); await ws.close(); return

        while True:
            data = await ws.receive_json(); t = data.get("type")

            if t == "host:set_quiz":
                qid = int(data["quizId"])
                if not manager.get_quiz(qid): await ws.send_json({"type":"error","message":"quiz not found"}); continue
                room.quiz_id = qid; room.current_index=-1; room.state="lobby"
                await manager.broadcast(room, {"type":"quiz:set","quizId":qid})

            elif t == "host:set_brand":
                room.venue_title = data.get("title",""); room.venue_logo = data.get("logo","")
                await manager.broadcast(room, {"type":"branding","venueTitle":room.venue_title,"venueLogo":room.venue_logo})

            elif t == "host:start_question":
                quiz = manager.get_quiz(room.quiz_id) if room.quiz_id else None
                if not quiz: await ws.send_json({"type":"error","message":"no quiz set"}); continue
                idx = data.get("index")
                if idx is None: room.current_index += 1
                else: room.current_index = int(idx)
                if room.current_index < 0 or room.current_index >= len(quiz.questions):
                    await ws.send_json({"type":"error","message":"no more questions"}); continue
                q = quiz.questions[room.current_index]
                ttl = int(data.get("timeLimitMs", q.timeLimit))
                room.question_end_at = int(time.time()*1000) + ttl
                room.state = "asking"
                manager.ensure_answer_bucket(room, q.id)
                await manager.broadcast(room, {"type":"question:prompt","questionId":q.id,"text":q.text,"options":q.options,"imageUrl":q.imageUrl,"questionEndAt":room.question_end_at})

            elif t == "host:lock":
                room.state = "locked"
                quiz = manager.get_quiz(room.quiz_id); q = quiz.questions[room.current_index]
                await manager.broadcast(room, {"type":"question:locked","questionId": q.id})

            elif t == "host:reveal":
                quiz = manager.get_quiz(room.quiz_id); q = quiz.questions[room.current_index]
                ansmap = room.answers.get(q.id, {}); counts=[0,0,0,0]
                for a in ansmap.values():
                    counts[a.option]+=1
                    if a.option == q.answer:
                        team = room.teams.get(a.team_id)
                        if team: team.score += manager.score_answer(True, q.timeLimit, a.ms_remaining)
                room.state = "revealed"
                leaderboard = sorted([{"teamId":t.id,"name":t.name,"score":t.score} for t in room.teams.values()], key=lambda x:x["score"], reverse=True)
                await manager.broadcast(room, {"type":"results:summary","questionId":q.id,"correctIndex":q.answer,"counts":counts,"leaderboard":leaderboard})

            elif t == "host:finish":
                room.state = "finished"
                winners = sorted(room.teams.values(), key=lambda t: t.score, reverse=True)[:3]
                await manager.broadcast(room, {"type":"quiz:finished","winners":[{"name":w.name,"score":w.score} for w in winners]})

            elif t == "team:answer":
                quiz = manager.get_quiz(room.quiz_id) if room.quiz_id else None
                if not quiz or room.current_index < 0: continue
                q = quiz.questions[room.current_index]
                now = int(time.time()*1000)
                if now > room.question_end_at or room.state not in ("asking",):
                    await ws.send_json({"type":"answer:rejected","reason":"late"}); continue
                team_id = None
                for tid, conn in room.team_connections.items():
                    if conn is ws: team_id=tid; break
                if not team_id: continue
                bucket = room.answers.setdefault(q.id, {})
                if team_id in bucket:
                    await ws.send_json({"type":"answer:rejected","reason":"already answered"}); continue
                remaining = max(0, room.question_end_at - now)
                bucket[team_id] = Answer(team_id=team_id, question_id=q.id, option=int(data["option"]), submitted_at=now, ms_remaining=remaining)
                await ws.send_json({"type":"answer:accepted","remainingMs":remaining})
                counts=[0,0,0,0]
                for a in bucket.values(): counts[a.option]+=1
                await manager.push_hosts(room, {"type":"answers:progress","questionId":q.id,"counts":counts,"answered":len(bucket),"teamsTotal":len(room.teams)})
    except WebSocketDisconnect:
        for r in manager.rooms.values():
            if ws in r.host_connections: r.host_connections.remove(ws)
            if ws in r.display_connections: r.display_connections.remove(ws)
            for tid, conn in list(r.team_connections.items()):
                if conn is ws:
                    del r.team_connections[tid]
                    if tid in r.teams: del r.teams[tid]
                    break
    except Exception as e:
        try: await ws.send_json({"type":"error","message":str(e)})
        except: pass
        try: await ws.close()
        except: pass

# ------------------ Admin pages & quiz builder ------------------
@app.get("/admin/login")
def login_page(request: Request, next: str = "/admin"):
    return HTMLResponse(env.get_template("login.html").render(next=next, error=None))

@app.post("/admin/login")
def do_login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/admin")):
    with get_conn() as conn:
        cur = conn.cursor()
        sql = f"SELECT id, email, name, password_hash, role FROM users WHERE email = {'%s' if USE_PG else '?'}"
        cur.execute(sql, (email,))
        row = cur.fetchone()
    if (not row) or (not hasher.verify(password, row[3])):
        return HTMLResponse(env.get_template("login.html").render(next=next, error="Invalid credentials"), status_code=401)
    request.session["user"] = {"id":row[0],"email":row[1],"name":row[2],"role":row[4]}
    return RedirectResponse(next, status_code=302)

@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/", response_class=HTMLResponse)
def admin_home(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(env.get_template("admin_home.html").render(user=user))

@app.get("/admin/hosts")
def hosts_page(request: Request):
    user = require_role(request, "admin")
    if not user:
        return RedirectResponse("/admin/login", status_code=302)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name, email FROM users WHERE role='host' ORDER BY id DESC")
        hosts = cur.fetchall()
    return HTMLResponse(env.get_template("hosts.html").render(hosts=[{"id":h[0],"name":h[1],"email":h[2]} for h in hosts]))

@app.post("/admin/hosts/add")
def hosts_add(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...)):
    user = require_role(request, "admin")
    if not user:
        return RedirectResponse("/admin/login", status_code=302)
    with get_conn() as conn:
        cur = conn.cursor()
        sql = "INSERT INTO users(name,email,password_hash,role) VALUES(%s,%s,%s,%s)" if USE_PG else "INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)"
        cur.execute(sql, (name, email, hasher.hash(password), "host"))
        conn.commit()
    return RedirectResponse("/admin/hosts", status_code=302)

@app.get("/admin/venues")
def venues_page(request: Request):
    user = require_role(request, "admin")
    if not user:
        return RedirectResponse("/admin/login", status_code=302)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name, logo_url FROM venues ORDER BY id DESC")
        venues = cur.fetchall()
        cur.execute("SELECT id, name FROM users WHERE role='host' ORDER BY name")
        hosts = cur.fetchall()
    return HTMLResponse(env.get_template("venues.html").render(
        venues=[{"id":v[0],"name":v[1],"logo_url":v[2]} for v in venues],
        hosts=[{"id":h[0],"name":h[1]} for h in hosts]
    ))

@app.post("/admin/venues/add")
def venues_add(request: Request, name: str = Form(...), logo_url: str = Form("")):
    user = require_role(request, "admin")
    if not user:
        return RedirectResponse("/admin/login", status_code=302)
    with get_conn() as conn:
        cur = conn.cursor()
        sql = "INSERT INTO venues(name, logo_url) VALUES(%s,%s)" if USE_PG else "INSERT INTO venues(name, logo_url) VALUES(?,?)"
        cur.execute(sql, (name, logo_url))
        conn.commit()
    return RedirectResponse("/admin/venues", status_code=302)

@app.post("/admin/venues/assign")
def venues_assign(request: Request, host_id: int = Form(...), venue_id: int = Form(...)):
    user = require_role(request, "admin")
    if not user:
        return RedirectResponse("/admin/login", status_code=302)
    with get_conn() as conn:
        cur = conn.cursor()
        if USE_PG:
            cur.execute("INSERT INTO hosts_venues(host_id, venue_id) VALUES(%s,%s) ON CONFLICT DO NOTHING", (host_id, venue_id))
        else:
            cur.execute("INSERT OR IGNORE INTO hosts_venues(host_id, venue_id) VALUES(?,?)", (host_id, venue_id))
        conn.commit()
    return RedirectResponse("/admin/venues", status_code=302)

@app.get("/admin/quizzes")
def quizzes_page(request: Request):
    user = require_role(request, "admin")
    if not user:
        return RedirectResponse("/admin/login", status_code=302)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, title FROM quizzes ORDER BY id DESC")
        quizzes = cur.fetchall()
    return HTMLResponse(env.get_template("quizzes_list.html").render(quizzes=[{"id":q[0],"title":q[1]} for q in quizzes]))

@app.get("/admin/quizzes/new")
def quiz_new(request: Request):
    user = require_role(request, "admin")
    if not user:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(env.get_template("quiz_builder.html").render(quiz={"id":None,"title":""}, questions=[]))

@app.get("/admin/quizzes/{qid}/edit")
def quiz_edit(request: Request, qid: int):
    user = require_role(request, "admin")
    if not user:
        return RedirectResponse("/admin/login", status_code=302)
    with get_conn() as conn:
        cur = conn.cursor()
        sql = f"SELECT id, title, data_json FROM quizzes WHERE id = {'%s' if USE_PG else '?'}"
        cur.execute(sql, (qid,))
        row = cur.fetchone()
    if not row:
        return HTMLResponse("Not found", status_code=404)
    payload = json.loads(row[2])
    return HTMLResponse(env.get_template("quiz_builder.html").render(
        quiz={"id":row[0],"title":payload.get("title", row[1])}, questions=payload.get("questions", [])
    ))

@app.post("/admin/quizzes/save")
def quiz_save(request: Request, qid: int = Form(None), title: str = Form(...), questions_json: str = Form(...)):
    user = require_role(request, "admin")
    if not user:
        return RedirectResponse("/admin/login", status_code=302)
    try:
        qlist = json.loads(questions_json)
        data_json = json.dumps({"title": title, "questions": qlist})
    except Exception as e:
        return HTMLResponse(f"Invalid data: {e}", status_code=400)
    with get_conn() as conn:
        cur = conn.cursor()
        if qid:
            sql = "UPDATE quizzes SET title=%s, data_json=%s WHERE id=%s" if USE_PG else "UPDATE quizzes SET title=?, data_json=? WHERE id=?"
            cur.execute(sql, (title, data_json, qid))
        else:
            sql = "INSERT INTO quizzes(title, data_json) VALUES(%s,%s)" if USE_PG else "INSERT INTO quizzes(title, data_json) VALUES(?,?)"
            cur.execute(sql, (title, data_json))
        conn.commit()
        cur.execute("SELECT id, title, data_json FROM quizzes")
        rows = cur.fetchall()
    wrapped = [{"id": r[0], "title": r[1], "data_json": r[2]} for r in rows]
    manager.load_quizzes(wrapped)
    return RedirectResponse("/admin/quizzes", status_code=302)

@app.post("/admin/upload_image")
def upload_image(request: Request, file: UploadFile = File(...)):
    user = require_role(request, "admin")
    if not user:
        return RedirectResponse("/admin/login", status_code=302)
    dest = os.path.join(static_dir, "images", file.filename)
    with open(dest, "wb") as f:
        f.write(file.file.read())
    return JSONResponse({"url": f"/static/images/{file.filename}"})

# ------------------ One-time bootstrap (Option B) ------------------
@app.post("/admin/bootstrap")
def admin_bootstrap(token: str = Form(None), email: str = Form(None), password: str = Form(None), name: str = Form("Admin")):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1")
        exists = cur.fetchone()
        if exists:
            return JSONResponse({"error":"admin already exists"}, status_code=409)
        expected = os.getenv("BOOTSTRAP_TOKEN")
        if not expected or token != expected:
            return JSONResponse({"error":"unauthorised"}, status_code=401)
        if not email or not password:
            return JSONResponse({"error":"email & password required"}, status_code=400)
        sql = "INSERT INTO users(name,email,password_hash,role) VALUES(%s,%s,%s,%s)" if USE_PG else "INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)"
        cur.execute(sql, (name, email, hasher.hash(password), "admin"))
        conn.commit()
    return {"status":"ok"}
