# app/room_manager.py
from __future__ import annotations
import time, random, string
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

def _code(n=6):
    return ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(n))

@dataclass
class Team:
    id: str
    name: str
    score: int = 0

@dataclass
class Answer:
    team_id: str
    question_id: str
    option: int
    submitted_at: int
    ms_remaining: int

@dataclass
class Question:
    id: str
    text: str
    options: List[str]
    answer: int
    timeLimit: int = 20000
    imageUrl: Optional[str] = None

@dataclass
class Quiz:
    id: int
    title: str
    questions: List[Question]

@dataclass
class Room:
    id: str
    quiz_id: Optional[int] = None
    state: str = "lobby"
    current_index: int = -1
    question_end_at: int = 0
    venue_title: str = ""
    venue_logo: str = ""
    venue_id: Optional[int] = None
    host_user_id: Optional[int] = None
    teams: Dict[str, Team] = field(default_factory=dict)
    answers: Dict[str, Dict[str, Answer]] = field(default_factory=lambda: {})
    host_connections: List[Any] = field(default_factory=list)
    team_connections: Dict[str, Any] = field(default_factory=dict)
    display_connections: List[Any] = field(default_factory=list)

class RoomManager:
    def __init__(self):
        self.rooms: Dict[str, Room] = {}
        self.quizzes: Dict[int, Quiz] = {}

    def load_quizzes(self, rows):
        self.quizzes.clear()
        import json
        for r in rows:
            payload = json.loads(r["data_json"])
            questions = []
            for i, q in enumerate(payload.get("questions", [])):
                questions.append(Question(
                    id=q.get("id", f"q{i+1}"),
                    text=q["text"],
                    options=q["options"],
                    answer=q["answer"],
                    timeLimit=q.get("timeLimit", 20000),
                    imageUrl=q.get("imageUrl")
                ))
            self.quizzes[r["id"]] = Quiz(id=r["id"], title=payload.get("title", r["title"]), questions=questions)

    def list_quizzes(self):
        return [{"id": qid, "title": q.title, "count": len(q.questions)} for qid, q in self.quizzes.items()]

    def create_room(self, host_user_id: int, venue_title: str, venue_logo: str, venue_id: int) -> Room:
        rid = _code(6)
        while rid in self.rooms:
            rid = _code(6)
        room = Room(id=rid, venue_title=venue_title, venue_logo=venue_logo, venue_id=venue_id, host_user_id=host_user_id)
        self.rooms[rid] = room
        return room

    def get_room(self, room_id: str) -> Optional[Room]:
        return self.rooms.get(room_id)

    def get_quiz(self, quiz_id: int) -> Optional[Quiz]:
        return self.quizzes.get(quiz_id)

    async def broadcast(self, room: Room, payload: dict):
        for ws in list(room.host_connections):
            try: await ws.send_json(payload)
            except: pass
        for ws in list(room.team_connections.values()):
            try: await ws.send_json(payload)
            except: pass
        for ws in list(room.display_connections):
            try: await ws.send_json(payload)
            except: pass

    async def push_hosts(self, room: Room, payload: dict):
        for ws in list(room.host_connections):
            try: await ws.send_json(payload)
            except: pass

    def ensure_answer_bucket(self, room: Room, qid: str):
        if qid not in room.answers:
            room.answers[qid] = {}

    def score_answer(self, is_correct: bool, total_ms: int, remaining_ms: int) -> int:
        if not is_correct: return 0
        base = 100
        speed = 0.5 + 0.5 * max(0.0, min(1.0, remaining_ms / max(1, total_ms)))
        return int(base * speed)
