from fastapi import FastAPI, HTTPException, Header, Request
from pydantic import BaseModel
from supabase import create_client, Client, ClientOptions
from typing import Optional
import os

app = FastAPI()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
API_SECRET   = os.environ["ROBLOX_API_SECRET"]

def get_client() -> Client:
    return create_client(
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
    rep: int = 10

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

@app.post("/sync")
def sync_player(payload: PlayerSyncPayload, x_api_secret: str = Header(...)):
    check_auth(x_api_secret)
    uid = str(payload.userId)

    get_client().table("players").upsert({
        "user_id":    uid,
        "argent":     payload.argent,
        "reputation": payload.reputation,
        "raised":     payload.raised,
        "donated":    payload.donated,
        "time_stats": payload.timeStats,
    }).execute()

    if payload.items:
        get_client().table("player_items").upsert({
            "user_id": uid,
            "items":   payload.items,
        }).execute()

    if payload.settings:
        get_client().table("player_settings").upsert({
            "user_id":  uid,
            "settings": payload.settings,
        }).execute()

    if payload.comments_sent or payload.comments_received:
        get_client().table("comments").upsert({
            "user_id":  uid,
            "sent":     [c.model_dump() for c in payload.comments_sent],
            "received": [c.model_dump() for c in payload.comments_received],
        }).execute()

    # Snapshot pour la protection anti-perte
    get_client().table("player_snapshots").upsert({
        "user_id":    uid,
        "argent":     payload.argent,
        "reputation": payload.reputation,
        "raised":     payload.raised,
        "donated":    payload.donated,
        "items":      payload.items,
    }).execute()

    return {"ok": True}


@app.get("/snapshot/{user_id}")
def get_snapshot(user_id: str, x_api_secret: str = Header(...)):
    check_auth(x_api_secret)

    try:
        result = get_client().table("player_snapshots").select("*").eq("user_id", user_id).single().execute()
        return result.data or {}
    except Exception:
        return {}


@app.get("/player/{user_id}")
def get_player(user_id: str, x_api_secret: str = Header(...)):
    check_auth(x_api_secret)

    player   = get_client().table("players").select("*").eq("user_id", user_id).single().execute()
    items    = get_client().table("player_items").select("items").eq("user_id", user_id).maybe_single().execute()
    settings = get_client().table("player_settings").select("settings").eq("user_id", user_id).maybe_single().execute()
    comments = get_client().table("comments").select("*").eq("user_id", user_id).maybe_single().execute()

    return {
        "player":   player.data,
        "items":    items.data["items"] if items.data else {},
        "settings": settings.data["settings"] if settings.data else {},
        "comments": comments.data or {},
    }


@app.get("/health")
def health():
    return {"status": "ok"}

@app.api_route("/ping", methods=["GET", "HEAD"])
def ping():
    return {"pong": True}
