# app/main.py
from __future__ import annotations
import os, time, json
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from passlib.hash import bcrypt
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .db import init_db
from .room_manager import RoomManager, Team, Answer

# -----------------------------------------------------------------------------
# App setup
# -----------------------------------------------------------------------------
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY", "dev-secret-change"))

BASE = os.path.dirname(__file__)
static_dir = os.path.join(BASE, "static")
templates_dir = os.path.join(BASE, "templates")
app.mount("/static", StaticFiles(directory=static_dir), name="static")
env = Environment(loader=FileSystemLoader(templates_dir), autoescape=select_autoescape())

conn = init_db()
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

# -----------------------------------------------------------------------------
# Admin auto-seed on startup (OPTION A)
# -----------------------------------------------------------------------------
def ensure_admin_from_env():
    """Creates the first admin if none exist, reading ADMIN_EMAIL/ADMIN_PASSWORD."""
    cur = conn.cursor()
    exists = cur.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone()
    if exists:
        return
    email = os.getenv("ADMIN_EMAIL")
    password = os.getenv("ADMIN_PASSWORD")
    name = os.getenv("ADMIN_NAME", "Admin")
    if email and password:
        cur.execute(
            "INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)",
            (name, email, bcrypt.hash(password), "admin"),
        )
        conn.commit()
        print(f"[INIT] Admin created: {email}")

# -----------------------------------------------------------------------------
# Startup: load quizzes into memory + seed admin if needed
# -----------------------------------------------------------------------------
@app.on_event("startup")
def startup_load_quizzes():
    cur = conn.cursor()
    rows = cur.execute("SELECT id, title, data_json FROM quizzes").fetchall()
    manager.load_quizzes(rows)
    ensure_admin_from_env()

# -----------------------------------------------------------------------------
# Pages
# -----------------------------------------------------------------------------
@app.get("/")
def home():
    return HTMLResponse(env.get_template("home.html").render())

@app.get("/host")
def host_console(request: Request):
    user = require_role(request, "host")
    if not user:
        return RedirectResponse("/admin/login?next=/host", status_code=302)
    return HTMLResponse(env.get_template("host_gate.html").render())

# -----------------------------------------------------------------------------
# API
# -----------------------------------------------------------------------------
@app.get("/api/me")
def api_me(request: Request):
    return current_user(request) or {}

@app.get("/api/my_venues")
def my_venues(request: Request):
    user = require_role(request, "host")
    if not user:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT v.id, v.name, v.logo_url
        FROM venues v
        JOIN hosts_venues hv ON hv.venue_id = v.id
        WHERE hv.host_id = ?
    """, (user["id"],)).fetchall()
    return [{"id": r["id"], "name": r["name"], "logo_url": r["logo_url"]} for r in rows]

@app.post("/api/create_room")
def create_room(request: Request, venue_id: int = Form(...), venue_title: str = Form(""), venue_logo: str = Form("")):
    user = require_role(request, "host")
    if not user:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    room = manager.create_room(user["id"], venue_title, venue_logo, venue_id)
    return {"roomId": room.id}

@app.get("/api/quizzes")
def list_quizzes():
    return manager.list_quizzes()

@app.get("/api/export/{room_id}")
def export_scores(room_id: str):
    room = manager.get_room(room_id)
    if not room:
        return JSONResponse({"error": "Room not found"}, status_code=404)
    import io, csv
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Team", "Score"])
    for t in sorted(room.teams.values(), key=lambda x: x.score, reverse=True):
        writer.writerow([t.name, t.score])
    return Response(
        output.getvalue().encode("utf-8"),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={room_id}_scores.csv"},
    )

# -----------------------------------------------------------------------------
# WebSocket game hub
# -----------------------------------------------------------------------------
@app.websocket("/ws")
async def ws(ws: WebSocket):
    await ws.accept()
    try:
        init = await ws.receive_json()
        role = init.get("role")
        room_id = init.get("roomId")
        room = None

        if role == "host":
            if not room_id:
                await ws.send_json({"type": "error", "message": "missing roomId"}); await ws.close(); return
            room = manager.get_room(room_id)
            if not room:
                await ws.send_json({"type": "error", "message": "room not found"}); await ws.close(); return
            room.host_connections.append(ws)
            await ws.send_json({"type":"room:init","roomId":room.id,"state":room.state})

        elif role == "team":
            if not room_id:
                await ws.send_json({"type": "error", "message": "missing roomId"}); await ws.close(); return
            room = manager.get_room(room_id)
            if not room:
                await ws.send_json({"type": "error", "message": "room not found"}); await ws.close(); return
            team_name = init.get("teamName","Team")
            team_id = f"t{int(time.time()*1000)%100000}_{len(room.teams)+1}"
            room.teams[team_id] = Team(id=team_id, name=team_name, score=0)
            room.team_connections[team_id] = ws
            await manager.broadcast(room, {"type":"teams:update","teams":[{"id":t.id,"name":t.name,"score":t.score} for t in room.teams.values()]})
            await ws.send_json({"type":"team:joined","teamId":team_id,"roomId":room.id})

        elif role == "display":
            if not room_id:
                await ws.send_json({"type": "error", "message": "missing roomId"}); await ws.close(); return
            room = manager.get_room(room_id)
            if not room:
                await ws.send_json({"type": "error", "message": "room not found"}); await ws.close(); return
            room.display_connections.append(ws)
            await ws.send_json({"type":"branding","venueTitle":room.venue_title,"venueLogo":room.venue_logo})

        else:
            await ws.send_json({"type": "error", "message": "invalid role"}); await ws.close(); return

        # messages
        while True:
            data = await ws.receive_json()
            t = data.get("type")

            if t == "host:set_quiz":
                qid = int(data["quizId"])
                if not manager.get_quiz(qid):
                    await ws.send_json({"type":"error","message":"quiz not found"}); continue
                room.quiz_id = qid; room.current_index = -1; room.state = "lobby"
                await manager.broadcast(room, {"type":"quiz:set","quizId":qid})

            elif t == "host:set_brand":
                room.venue_title = data.get("title",""); room.venue_logo = data.get("logo","")
                await manager.broadcast(room, {"type":"branding","venueTitle":room.venue_title,"venueLogo":room.venue_logo})

            elif t == "host:start_question":
                quiz = manager.get_quiz(room.quiz_id) if room.quiz_id else None
                if not quiz:
                    await ws.send_json({"type":"error","message":"no quiz set"}); continue
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
                await manager.broadcast(room, {
                    "type":"question:prompt",
                    "questionId":q.id,
                    "text":q.text,
                    "options":q.options,
                    "imageUrl":q.imageUrl,
                    "questionEndAt":room.question_end_at
                })

            elif t == "host:lock":
                room.state = "locked"
                quiz = manager.get_quiz(room.quiz_id)
                q = quiz.questions[room.current_index]
                await manager.broadcast(room, {"type":"question:locked","questionId":q.id})

            elif t == "host:reveal":
                quiz = manager.get_quiz(room.quiz_id)
                q = quiz.questions[room.current_index]
                ansmap = room.answers.get(q.id, {})
                counts = [0,0,0,0]
                for a in ansmap.values():
                    counts[a.option] += 1
                    if a.option == q.answer:
                        team = room.teams.get(a.team_id)
                        if team:
                            team.score += manager.score_answer(True, q.timeLimit, a.ms_remaining)
                room.state = "revealed"
                leaderboard = sorted([{"teamId":t.id,"name":t.name,"score":t.score} for t in room.teams.values()], key=lambda x:x["score"], reverse=True)
                await manager.broadcast(room, {
                    "type":"results:summary",
                    "questionId":q.id,
                    "correctIndex":q.answer,
                    "counts":counts,
                    "leaderboard":leaderboard
                })

            elif t == "host:finish":
                room.state = "finished"
                winners = sorted(room.teams.values(), key=lambda t: t.score, reverse=True)[:3]
                await manager.broadcast(room, {"type":"quiz:finished","winners":[{"name":w.name,"score":w.score} for w in winners]})

            elif t == "team:answer":
                quiz = manager.get_quiz(room.quiz_id) if room.quiz_id else None
                if not quiz or room.current_index < 0: 
                    continue
                q = quiz.questions[room.current_index]
                now = int(time.time()*1000)
                if now > room.question_end_at or room.state not in ("asking",):
                    await ws.send_json({"type":"answer:rejected","reason":"late"}); continue
                team_id = None
                for tid, conn_ws in room.team_connections.items():
                    if conn_ws is ws:
                        team_id = tid; break
                if not team_id:
                    continue
                bucket = room.answers.setdefault(q.id, {})
                if team_id in bucket:
                    await ws.send_json({"type":"answer:rejected","reason":"already answered"}); continue
                remaining = max(0, room.question_end_at - now)
                bucket[team_id] = Answer(team_id=team_id, question_id=q.id, option=int(data["option"]), submitted_at=now, ms_remaining=remaining)
                await ws.send_json({"type":"answer:accepted","remainingMs":remaining})
                counts = [0,0,0,0]
                for a in bucket.values():
                    counts[a.option] += 1
                await manager.push_hosts(room, {"type":"answers:progress","questionId":q.id,"counts":counts,"answered":len(bucket),"teamsTotal":len(room.teams)})
    except WebSocketDisconnect:
        # best-effort cleanup
        for r in manager.rooms.values():
            if ws in r.host_connections: r.host_connections.remove(ws)
            if ws in r.display_connections: r.display_connections.remove(ws)
            for tid, conn_ws in list(r.team_connections.items()):
                if conn_ws is ws:
                    del r.team_connections[tid]
                    if tid in r.teams: del r.teams[tid]
                    break
    except Exception as e:
        try: await ws.send_json({"type":"error","message":str(e)})
        except: pass
        try: await ws.close()
        except: pass

# -----------------------------------------------------------------------------
# Admin pages (minimal)
# -----------------------------------------------------------------------------
@app.get("/admin/login")
def login_page(request: Request, next: str = "/admin"):
    return HTMLResponse(env.get_template("login.html").render(next=next, error=None))

@app.post("/admin/login")
def do_login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/admin")):
    cur = conn.cursor()
    row = cur.execute("SELECT id, email, name, password_hash, role FROM users WHERE email=?", (email,)).fetchone()
    if not row or not bcrypt.verify(password, row["password_hash"]):
        return HTMLResponse(env.get_template("login.html").render(next=next, error="Invalid credentials"), status_code=401)
    request.session["user"] = {"id":row["id"],"email":row["email"],"name":row["name"],"role":row["role"]}
    return RedirectResponse(next, status_code=302)

@app.get("/admin/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=302)

@app.get("/admin")
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
    hosts = conn.cursor().execute("SELECT id, name, email FROM users WHERE role='host' ORDER BY id DESC").fetchall()
    return HTMLResponse(env.get_template("hosts.html").render(hosts=hosts))

@app.post("/admin/hosts/add")
def hosts_add(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...)):
    user = require_role(request, "admin")
    if not user:
        return RedirectResponse("/admin/login", status_code=302)
    conn.cursor().execute("INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)",(name,email,bcrypt.hash(password),"host"))
    conn.commit()
    return RedirectResponse("/admin/hosts", status_code=302)

@app.get("/admin/venues")
def venues_page(request: Request):
    user = require_role(request, "admin")
    if not user:
        return RedirectResponse("/admin/login", status_code=302)
    cur = conn.cursor()
    venues = cur.execute("SELECT id, name, logo_url FROM venues ORDER BY id DESC").fetchall()
    hosts = cur.execute("SELECT id, name FROM users WHERE role='host' ORDER BY name").fetchall()
    return HTMLResponse(env.get_template("venues.html").render(venues=venues, hosts=hosts))

@app.post("/admin/venues/add")
def venues_add(request: Request, name: str = Form(...), logo_url: str = Form("")):
    user = require_role(request, "admin")
    if not user:
        return RedirectResponse("/admin/login", status_code=302)
    conn.cursor().execute("INSERT INTO venues(name, logo_url) VALUES(?,?)",(name, logo_url))
    conn.commit()
    return RedirectResponse("/admin/venues", status_code=302)

@app.post("/admin/venues/assign")
def venues_assign(request: Request, host_id: int = Form(...), venue_id: int = Form(...)):
    user = require_role(request, "admin")
    if not user:
        return RedirectResponse("/admin/login", status_code=302)
    conn.cursor().execute("INSERT OR IGNORE INTO hosts_venues(host_id, venue_id) VALUES(?,?)",(host_id, venue_id))
    conn.commit()
    return RedirectResponse("/admin/venues", status_code=302)

# Quizzes list + builder from your simple build
@app.get("/admin/quizzes")
def quizzes_page(request: Request):
    user = require_role(request, "admin")
    if not user:
        return RedirectResponse("/admin/login", status_code=302)
    quizzes = conn.cursor().execute("SELECT id, title FROM quizzes ORDER BY id DESC").fetchall()
    return HTMLResponse(env.get_template("quizzes_list.html").render(quizzes=quizzes))

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
    row = conn.cursor().execute("SELECT id, title, data_json FROM quizzes WHERE id=?", (qid,)).fetchone()
    if not row:
        return HTMLResponse("Not found", status_code=404)
    payload = json.loads(row["data_json"])
    return HTMLResponse(env.get_template("quiz_builder.html").render(quiz={"id":row["id"],"title":payload.get("title", row["title"])}, questions=payload.get("questions", [])))

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
    cur = conn.cursor()
    if qid:
        cur.execute("UPDATE quizzes SET title=?, data_json=? WHERE id=?", (title, data_json, qid))
    else:
        cur.execute("INSERT INTO quizzes(title, data_json) VALUES(?,?)", (title, data_json))
    conn.commit()
    # reload memory cache
    rows = conn.cursor().execute("SELECT id, title, data_json FROM quizzes").fetchall()
    manager.load_quizzes(rows)
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

# -----------------------------------------------------------------------------
# (OPTION B) One-time Bootstrap endpoint (use if you don't want env seeding)
# -----------------------------------------------------------------------------
@app.post("/admin/bootstrap")
def admin_bootstrap(token: str = Form(None), email: str = Form(None), password: str = Form(None), name: str = Form("Admin")):
    # Block if an admin already exists
    cur = conn.cursor()
    exists = cur.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone()
    if exists:
        return JSONResponse({"error":"admin already exists"}, status_code=409)

    expected = os.getenv("BOOTSTRAP_TOKEN")
    if not expected or token != expected:
        return JSONResponse({"error":"unauthorised"}, status_code=401)

    if not email or not password:
        return JSONResponse({"error":"email & password required"}, status_code=400)

    cur.execute("INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)",
                (name, email, bcrypt.hash(password), "admin"))
    conn.commit()
    return {"status":"ok"}
