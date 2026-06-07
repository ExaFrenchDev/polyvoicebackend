from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from typing import Any, Optional
from supabase import create_client, Client, ClientOptions

import os

app = FastAPI()

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    print(f"[422] body={body.decode()[:500]}")
    print(f"[422] errors={exc.errors()}")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
API_SECRET   = os.environ["ROBLOX_API_SECRET"]

def get_client() -> Client:
    return create_client(
        SUPABASE_URL,
        SUPABASE_KEY,
        options=ClientOptions(auto_refresh_token=False, persist_session=False)
    )


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
    items: Any = {}
    settings: Any = {}
    raised: Optional[int] = None

    @field_validator("items", "settings", mode="before")
    @classmethod
    def coerce_to_dict(cls, v):
        if isinstance(v, list):
            return {}
        if isinstance(v, dict):
            return v
        return {}
    donated: Optional[int] = None
    timeStats: Optional[int] = None
    comments_sent: list[CommentEntry] = []
    comments_received: list[CommentEntry] = []


def check_auth(x_api_secret: str):
    if x_api_secret != API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


def is_empty_response_error(e: Exception) -> bool:
    """Returns True if the exception is just a 204 No Content (no rows found), not a real error."""
    s = str(e)
    return "204" in s or "Missing response" in s


def merge_comments(existing_list: list, new_list: list) -> list:
    seen   = {(c["timestamp"], c["authorId"]) for c in existing_list}
    merged = list(existing_list)
    for c in new_list:
        key = (c["timestamp"], c["authorId"])
        if key not in seen:
            merged.append(c)
            seen.add(key)
    return merged


@app.post("/sync")
def sync_player(payload: PlayerSyncPayload, x_api_secret: str = Header(...)):
    check_auth(x_api_secret)
    uid = str(payload.userId)

    clean_items    = {k: bool(v) for k, v in payload.items.items()}
    clean_settings = {k: bool(v) for k, v in payload.settings.items()}

    def safe_upsert(table: str, data: dict):
        try:
            get_client().table(table).upsert(data).execute()
        except Exception as e:
            print(f"[sync] upsert {table} for {uid} failed: {e}")

    safe_upsert("players", {
        "user_id":    uid,
        "argent":     payload.argent,
        "reputation": payload.reputation,
        "raised":     payload.raised,
        "donated":    payload.donated,
        "time_stats": payload.timeStats,
    })

    if clean_items:
        safe_upsert("player_items", {
            "user_id": uid,
            "items":   clean_items,
        })

    if clean_settings:
        safe_upsert("player_settings", {
            "user_id":  uid,
            "settings": clean_settings,
        })

    if payload.comments_sent or payload.comments_received:
        safe_upsert("comments", {
            "user_id":  uid,
            "sent":     [c.model_dump() for c in payload.comments_sent],
            "received": [c.model_dump() for c in payload.comments_received],
        })

    # Single SELECT fetching all snapshot fields at once (avoids double query + silences 204 false errors)
    try:
        existing = get_client().table("player_snapshots") \
                        .select("raised, donated, items, comments_sent, comments_received, time_stats") \
                        .eq("user_id", uid).maybe_single().execute()
        existing_data = existing.data or {}
    except Exception as e:
        if not is_empty_response_error(e):
            print(f"[sync] Error fetching existing snapshot for {uid}: {e}")
        existing_data = {}

    merged_items = {**(existing_data.get("items") or {}), **clean_items}

    new_sent     = [c.model_dump() for c in payload.comments_sent]
    new_received = [c.model_dump() for c in payload.comments_received]

    safe_upsert("player_snapshots", {
        "user_id":           uid,
        "argent":            payload.argent,
        "reputation":        payload.reputation,
        "raised":            max(payload.raised  or 0, existing_data.get("raised")  or 0),
        "donated":           max(payload.donated or 0, existing_data.get("donated") or 0),
        "items":             merged_items,
        "time_stats":        payload.timeStats,
        "comments_sent":     merge_comments(existing_data.get("comments_sent")     or [], new_sent),
        "comments_received": merge_comments(existing_data.get("comments_received") or [], new_received),
    })

    print(f"[sync] {uid} synced successfully")
    return {"ok": True}


@app.get("/snapshot/{user_id}")
def get_snapshot(user_id: str, x_api_secret: str = Header(...)):
    check_auth(x_api_secret)

    try:
        result = get_client().table("player_snapshots").select("*").eq("user_id", user_id).single().execute()
        data = result.data or {}
        print(f"[snapshot] {user_id}: argent={data.get('argent')}, rep={data.get('reputation')}, raised={data.get('raised')}, donated={data.get('donated')}")
        return data
    except Exception as e:
        print(f"[snapshot] Error fetching snapshot for {user_id}: {e}")
        return {}


@app.get("/player/{user_id}")
def get_player(user_id: str, x_api_secret: str = Header(...)):
    check_auth(x_api_secret)

    try:
        player = get_client().table("players").select("*").eq("user_id", user_id).single().execute()
        player_data = player.data or {}
    except Exception as e:
        print(f"[player] Error fetching player {user_id}: {e}")
        player_data = {}

    try:
        items = get_client().table("player_items").select("items").eq("user_id", user_id).maybe_single().execute()
        items_data = items.data["items"] if items.data else {}
    except Exception as e:
        if not is_empty_response_error(e):
            print(f"[player] Error fetching items {user_id}: {e}")
        items_data = {}

    try:
        settings = get_client().table("player_settings").select("settings").eq("user_id", user_id).maybe_single().execute()
        settings_data = settings.data["settings"] if settings.data else {}
    except Exception as e:
        if not is_empty_response_error(e):
            print(f"[player] Error fetching settings {user_id}: {e}")
        settings_data = {}

    try:
        comments = get_client().table("comments").select("*").eq("user_id", user_id).maybe_single().execute()
        comments_data = comments.data or {}
    except Exception as e:
        if not is_empty_response_error(e):
            print(f"[player] Error fetching comments {user_id}: {e}")
        comments_data = {}

    return {
        "player":   player_data,
        "items":    items_data,
        "settings": settings_data,
        "comments": comments_data,
    }


@app.post("/sync-debug")
async def sync_debug(request: Request):
    body = await request.json()
    print(f"[debug] {body}")
    return {"received": body}


@app.get("/health")
def health():
    return {"status": "ok"}

@app.api_route("/ping", methods=["GET", "HEAD"])
def ping():
    return {"pong": True}
