from fastapi import FastAPI, HTTPException, Header, Request
from pydantic import BaseModel
from supabase import create_client, Client, ClientOptions
from typing import Optional
import os

app = FastAPI()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
API_SECRET   = os.environ["ROBLOX_API_SECRET"]

supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY,
    options=ClientOptions(auto_refresh_token=False, persist_session=False)
)


# ── Modèles ──────────────────────────────────────────────────────────────────

class CommentEntry(BaseModel):
    text: str
    authorId: int
    authorName: str
    timestamp: int
    rep: int

class CommentsPayload(BaseModel):
    userId: int
    sent: list[CommentEntry]
    received: list[CommentEntry]

class MoneyReputationPayload(BaseModel):
    userId: int
    argent: int
    reputation: int

class ItemsPayload(BaseModel):
    userId: int
    items: dict[str, bool]

class SettingsPayload(BaseModel):
    userId: int
    fishsfx: bool
    ldm: bool
    boombox: bool
    tags: bool
    night: bool
    shadows: bool

class RaisedDonatedPayload(BaseModel):
    userId: int
    raised: Optional[int] = None
    donated: Optional[int] = None
    timeStats: Optional[int] = None

class PlayerSyncPayload(BaseModel):
    userId: int
    argent: int
    reputation: int
    items: dict[str, bool]
    settings: dict[str, bool]
    raised: Optional[int] = None
    donated: Optional[int] = None
    timeStats: Optional[int] = None
    comments_sent: list[CommentEntry] = []
    comments_received: list[CommentEntry] = []


# ── Auth ─────────────────────────────────────────────────────────────────────

def check_auth(x_api_secret: str):
    if x_api_secret != API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Routes ───────────────────────────────────────────────────────────────────

@app.post("/debug")
async def debug(request: Request):
    body = await request.json()
    print(body)
    return body

@app.post("/sync")
def sync_player(payload: PlayerSyncPayload, x_api_secret: str = Header(...)):
    check_auth(x_api_secret)
    uid = str(payload.userId)

    supabase.table("players").upsert({
        "user_id":    uid,
        "argent":     payload.argent,
        "reputation": payload.reputation,
        "raised":     payload.raised,
        "donated":    payload.donated,
        "time_stats": payload.timeStats,
    }).execute()

    if payload.items:
        supabase.table("player_items").upsert({
            "user_id": uid,
            "items":   payload.items,
        }).execute()

    if payload.settings:
        supabase.table("player_settings").upsert({
            "user_id":  uid,
            "settings": payload.settings,
        }).execute()

    if payload.comments_sent:
        rows = [
            {
                "author_id":   uid,
                "target_id":   str(c.authorId),
                "text":        c.text,
                "rep":         c.rep,
                "timestamp":   c.timestamp,
            }
            for c in payload.comments_sent
        ]
        supabase.table("comments").upsert(rows, on_conflict="author_id,target_id,timestamp").execute()

    return {"ok": True}


@app.get("/player/{user_id}")
def get_player(user_id: str, x_api_secret: str = Header(...)):
    check_auth(x_api_secret)

    player   = supabase.table("players").select("*").eq("user_id", user_id).single().execute()
    items    = supabase.table("player_items").select("items").eq("user_id", user_id).maybe_single().execute()
    settings = supabase.table("player_settings").select("settings").eq("user_id", user_id).maybe_single().execute()
    comments = supabase.table("comments").select("*").eq("target_id", user_id).execute()

    return {
        "player":   player.data,
        "items":    items.data["items"] if items.data else {},
        "settings": settings.data["settings"] if settings.data else {},
        "comments": comments.data,
    }


@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/ping")
def ping():
    return {"pong": True}
