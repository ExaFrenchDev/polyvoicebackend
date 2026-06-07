from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, ORJSONResponse
from pydantic import BaseModel, field_validator
from typing import Any, Optional
from supabase import create_client, Client, ClientOptions
import asyncio
import os

app = FastAPI()

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    print(f"[422] body={body.decode()[:500]}")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
API_SECRET   = os.environ["ROBLOX_API_SECRET"]

_client = None
def get_client() -> Client:
    global _client
    if not _client:
        _client = create_client(
            SUPABASE_URL,
            SUPABASE_KEY,
            options=ClientOptions(auto_refresh_token=False, persist_session=False)
        )
    return _client


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
    fish_inventory: Any = {}
    raised: Optional[int] = None

    @field_validator("items", "settings", "fish_inventory", mode="before")
    @classmethod
    def coerce_to_dict(cls, v):
        return v if isinstance(v, dict) else {}

    donated: Optional[int] = None
    timeStats: Optional[int] = None
    comments_sent: list[CommentEntry] = []
    comments_received: list[CommentEntry] = []


def check_auth(x_api_secret: str):
    if x_api_secret != API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


def is_empty_response_error(e: Exception) -> bool:
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


def merge_fish(existing: dict, incoming: dict) -> dict:
    merged = dict(existing)
    for fish, count in incoming.items():
        merged[fish] = int(count or 0)
    return merged


async def async_upsert(table: str, data: dict, uid: str):
    try:
        get_client().table(table).upsert(data).execute()
    except Exception as e:
        print(f"[sync] upsert {table} for {uid} failed: {e}")


@app.post("/sync")
async def sync_player(payload: PlayerSyncPayload, x_api_secret: str = Header(...)):
    check_auth(x_api_secret)
    uid = str(payload.userId)

    clean_items    = {k: bool(v) for k, v in payload.items.items()} if payload.items else {}
    clean_settings = {k: bool(v) for k, v in payload.settings.items()} if payload.settings else {}
    clean_fish     = {k: int(v) for k, v in payload.fish_inventory.items() if int(v or 0) > 0} if payload.fish_inventory else {}

    base_data = {
        "user_id":    uid,
        "argent":     payload.argent,
        "reputation": payload.reputation,
        "raised":     payload.raised,
        "donated":    payload.donated,
        "time_stats": payload.timeStats,
    }

    try:
        existing = get_client().table("player_snapshots") \
                        .select("raised,donated,items,comments_sent,comments_received,time_stats,fish_inventory") \
                        .eq("user_id", uid).maybe_single().execute()
        existing_data = existing.data or {}
    except Exception as e:
        if not is_empty_response_error(e):
            print(f"[sync] Error fetching existing snapshot for {uid}: {e}")
        existing_data = {}

    merged_items = {**(existing_data.get("items") or {}), **clean_items}
    merged_fish  = merge_fish(existing_data.get("fish_inventory") or {}, clean_fish)
    new_sent     = [c.model_dump() for c in payload.comments_sent]
    new_received = [c.model_dump() for c in payload.comments_received]

    snapshot_data = {
        **base_data,
        "items":             merged_items,
        "fish_inventory":    merged_fish,
        "comments_sent":     merge_comments(existing_data.get("comments_sent") or [], new_sent),
        "comments_received": merge_comments(existing_data.get("comments_received") or [], new_received),
        "raised":            max(payload.raised or 0, existing_data.get("raised") or 0),
        "donated":           max(payload.donated or 0, existing_data.get("donated") or 0),
    }

    upsert_tasks = [
        asyncio.create_task(async_upsert("players", base_data, uid)),
        asyncio.create_task(async_upsert("player_snapshots", snapshot_data, uid)),
    ]

    if clean_items:
        upsert_tasks.append(asyncio.create_task(async_upsert("player_items", {"user_id": uid, "items": clean_items}, uid)))

    if clean_settings:
        upsert_tasks.append(asyncio.create_task(async_upsert("player_settings", {"user_id": uid, "settings": clean_settings}, uid)))

    if new_sent or new_received:
        upsert_tasks.append(asyncio.create_task(async_upsert("comments", {
            "user_id":  uid,
            "sent":     new_sent,
            "received": new_received,
        }, uid)))

    await asyncio.gather(*upsert_tasks, return_exceptions=True)
    print(f"[sync] {uid} synced")
    return ORJSONResponse({"ok": True})


@app.get("/snapshot/{user_id}")
async def get_snapshot(user_id: str, x_api_secret: str = Header(...)):
    check_auth(x_api_secret)

    try:
        result = get_client().table("player_snapshots").select("*").eq("user_id", user_id).single().execute()
        data = result.data or {}
        fish_count = len(data.get("fish_inventory") or {})
        print(f"[snapshot] {user_id}: argent={data.get('argent')}, fish={fish_count}")
        return ORJSONResponse(data)
    except Exception as e:
        print(f"[snapshot] Error for {user_id}: {e}")
        return ORJSONResponse({})


@app.get("/player/{user_id}")
async def get_player(user_id: str, x_api_secret: str = Header(...)):
    check_auth(x_api_secret)

    async def fetch_player():
        try:
            return get_client().table("players").select("*").eq("user_id", user_id).single().execute().data or {}
        except Exception as e:
            print(f"[player] Error fetching player {user_id}: {e}")
            return {}

    async def fetch_items():
        try:
            result = get_client().table("player_items").select("items").eq("user_id", user_id).maybe_single().execute()
            return result.data.get("items", {}) if result and result.data else {}
        except Exception as e:
            if not is_empty_response_error(e):
                print(f"[player] Error fetching items {user_id}: {e}")
            return {}

    async def fetch_settings():
        try:
            result = get_client().table("player_settings").select("settings").eq("user_id", user_id).maybe_single().execute()
            return result.data.get("settings", {}) if result and result.data else {}
        except Exception as e:
            if not is_empty_response_error(e):
                print(f"[player] Error fetching settings {user_id}: {e}")
            return {}

    async def fetch_comments():
        try:
            result = get_client().table("comments").select("*").eq("user_id", user_id).maybe_single().execute()
            return result.data if result and result.data else {}
        except Exception as e:
            if not is_empty_response_error(e):
                print(f"[player] Error fetching comments {user_id}: {e}")
            return {}

    player, items, settings, comments = await asyncio.gather(
        fetch_player(),
        fetch_items(),
        fetch_settings(),
        fetch_comments(),
        return_exceptions=True
    )

    return ORJSONResponse({
        "player":   player if isinstance(player, dict) else {},
        "items":    items if isinstance(items, dict) else {},
        "settings": settings if isinstance(settings, dict) else {},
        "comments": comments if isinstance(comments, dict) else {},
    })


@app.post("/sync-debug")
async def sync_debug(request: Request):
    body = await request.json()
    print(f"[debug] {body}")
    return ORJSONResponse({"received": body})


@app.get("/health")
async def health():
    return ORJSONResponse({"status": "ok"})

@app.api_route("/ping", methods=["GET", "HEAD"])
async def ping():
    return ORJSONResponse({"pong": True})
