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
    items: dict[str, bool] = {}
    settings: dict[str, bool] = {}
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

    def safe_upsert(table: str, data: dict):
        try:
            get_client().table(table).upsert(data).execute()
        except Exception as e:
            warn_msg = f"[sync] upsert {table} failed: {e}"
            print(warn_msg)

    safe_upsert("players", {
        "user_id":    uid,
        "argent":     payload.argent,
        "reputation": payload.reputation,
        "raised":     payload.raised,
        "donated":    payload.donated,
        "time_stats": payload.timeStats,
    })

    if payload.items:
        safe_upsert("player_items", {
            "user_id": uid,
            "items":   payload.items,
        })

    if payload.settings:
        safe_upsert("player_settings", {
            "user_id":  uid,
            "settings": payload.settings,
        })

    if payload.comments_sent or payload.comments_received:
        safe_upsert("comments", {
            "user_id":  uid,
            "sent":     [c.model_dump() for c in payload.comments_sent],
            "received": [c.model_dump() for c in payload.comments_received],
        })

    # ── Snapshot anti-perte ──────────────────────────────────────────────────
    # raised/donated  → high-water mark
    # items           → merge
    # argent/rep      → valeur actuelle

    def merge_comments(existing_list: list, new_list: list) -> list:
        seen   = {(c["timestamp"], c["authorId"]) for c in existing_list}
        merged = list(existing_list)
        for c in new_list:
            key = (c["timestamp"], c["authorId"])
            if key not in seen:
                merged.append(c)
                seen.add(key)
        return merged

    try:
        existing      = get_client().table("player_snapshots") \
                            .select("raised, donated, items") \
                            .eq("user_id", uid).maybe_single().execute()
        existing_data = existing.data or {}
    except Exception:
        existing_data = {}

    merged_items = {**(existing_data.get("items") or {}), **payload.items}

    snapshot_base = {
        "user_id":    uid,
        "argent":     payload.argent,
        "reputation": payload.reputation,
        "raised":     max(payload.raised  or 0, existing_data.get("raised")  or 0),
        "donated":    max(payload.donated or 0, existing_data.get("donated") or 0),
        "items":      merged_items,
    }

    # Tente d'inclure les comments si les colonnes existent
    try:
        existing_comments = get_client().table("player_snapshots") \
                                .select("comments_sent, comments_received") \
                                .eq("user_id", uid).maybe_single().execute()
        cd            = existing_comments.data or {}
        new_sent      = [c.model_dump() for c in payload.comments_sent]
        new_received  = [c.model_dump() for c in payload.comments_received]
        safe_upsert("player_snapshots", {
            **snapshot_base,
            "comments_sent":     merge_comments(cd.get("comments_sent")     or [], new_sent),
            "comments_received": merge_comments(cd.get("comments_received") or [], new_received),
        })
    except Exception:
        safe_upsert("player_snapshots", snapshot_base)

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


@app.post("/sync-debug")
async def sync_debug(request: Request):
    body = await request.json()
    return {"received": body}


@app.get("/health")
def health():
    return {"status": "ok"}

@app.api_route("/ping", methods=["GET", "HEAD"])
def ping():
    return {"pong": True}
