from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import os
import hashlib
import hmac
import logging
import requests
import math
import json
import base64
import time
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
import threading
from concurrent.futures import ThreadPoolExecutor

try:
    import pyotp
    PYOTP_AVAILABLE = True
except ImportError:
    PYOTP_AVAILABLE = False

try:
    from imap_2fa import fetch_roblox_email_code
    IMAP_AVAILABLE = True
except ImportError:
    IMAP_AVAILABLE = False

load_dotenv()

app = Flask(__name__)

WEBHOOK_SECRET       = os.getenv("WEBHOOK_SECRET", "")
GROUP_ID             = int(os.getenv("GROUP_ID", "0"))
ACCOUNT_COOKIE       = os.getenv("ROBLOX_ACCOUNT_COOKIE", "")
ROBLOX_2FA_SECRET    = os.getenv("ROBLOX_2FA_SECRET", "")
ROBLOX_API_KEY       = os.getenv("ROBLOX_API_KEY", "")
DISCORD_WEBHOOK_URL  = os.getenv("DISCORD_WEBHOOK_URL", "")
ADMIN_PASSWORD       = os.getenv("ADMIN_PASSWORD", "exa14170.")
DATABASE_URL         = os.getenv("DATABASE_URL", "")
ROBLOX_USERNAME      = os.getenv("ROBLOX_USERNAME", "")
ROBLOX_PASSWORD      = os.getenv("ROBLOX_PASSWORD", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEVPRODUCT_AMOUNTS = {
    "3593525234": 1,
    "3598085103": 5,
    "3598085104": 10,
    "3593525497": 50,
    "3593525652": 100,
}

TAX_RATE              = 0.60
WAIT_DAYS             = 7
MIN_GROUP_TENURE_DAYS = 7

DELAY_AFTER_CSRF    = 2.0
DELAY_AFTER_2FA     = 10.0
DELAY_BEFORE_FINAL  = 10.0
DELAY_BLOCKSESSION  = 15.0

cookie_lock    = threading.Lock()
payout_executor = ThreadPoolExecutor(max_workers=3)


# ============================================================================
# COOKIE MANAGEMENT
# ============================================================================

def reauthenticate_roblox():
    if not ROBLOX_USERNAME or not ROBLOX_PASSWORD:
        return None
    try:
        logger.info(f"[Reauth] 🔄 Login {ROBLOX_USERNAME}...")
        session = requests.Session()
        pre = session.post(
            "https://auth.roblox.com/v2/logout",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5, json={}
        )
        csrf = pre.headers.get('X-CSRF-TOKEN') or pre.headers.get('x-csrf-token', '')

        headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
        if csrf:
            headers["X-CSRF-TOKEN"] = csrf

        resp = session.post(
            "https://auth.roblox.com/v2/login",
            json={"ctype": "Username", "cvalue": ROBLOX_USERNAME, "passwd": ROBLOX_PASSWORD},
            headers=headers, timeout=10
        )
        logger.info(f"[Reauth] HTTP {resp.status_code}")

        if resp.status_code == 200:
            cookie = session.cookies.get('.ROBLOSECURITY')
            if cookie:
                os.environ['ROBLOX_ACCOUNT_COOKIE'] = cookie
                globals()['ACCOUNT_COOKIE'] = cookie
                logger.info("[Reauth] ✅ Cookie obtenu")
                return cookie

        elif resp.status_code == 403:
            new_csrf = resp.headers.get('X-CSRF-TOKEN') or resp.headers.get('x-csrf-token')
            if new_csrf:
                headers["X-CSRF-TOKEN"] = new_csrf
                resp2 = session.post(
                    "https://auth.roblox.com/v2/login",
                    json={"ctype": "Username", "cvalue": ROBLOX_USERNAME, "passwd": ROBLOX_PASSWORD},
                    headers=headers, timeout=10
                )
                if resp2.status_code == 200:
                    cookie = session.cookies.get('.ROBLOSECURITY')
                    if cookie:
                        os.environ['ROBLOX_ACCOUNT_COOKIE'] = cookie
                        globals()['ACCOUNT_COOKIE'] = cookie
                        logger.info("[Reauth] ✅ Cookie obtenu (retry)")
                        return cookie
        return None
    except Exception as e:
        logger.error(f"[Reauth] ❌ {e}")
        return None


def verify_cookie_validity(cookie):
    try:
        resp = requests.get(
            "https://users.roblox.com/v1/users/authenticated",
            headers={'Cookie': f'.ROBLOSECURITY={cookie}', 'User-Agent': 'Mozilla/5.0'},
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            logger.info(f"[Cookie] ✅ {data.get('name')} ({data.get('id')})")
            return True
        logger.warning(f"[Cookie] ⚠️ HTTP {resp.status_code}")
        return False
    except Exception as e:
        logger.error(f"[Cookie] ❌ {e}")
        return False


def get_valid_cookie():
    current = globals().get('ACCOUNT_COOKIE', '')
    if current and verify_cookie_validity(current):
        return current
    logger.warning("[Cookie] Réauth automatique...")
    return reauthenticate_roblox()


# ============================================================================
# ROBLOX PAYOUT
# ============================================================================

class RobloxPayout:
    def __init__(self, roblosecurity, group_id, twofactor_secret=""):
        self.roblosecurity   = roblosecurity
        self.group_id        = group_id
        self.twofactor_secret = twofactor_secret
        self.headers = {
            'Cookie':     f'.ROBLOSECURITY={roblosecurity}',
            'User-Agent': 'Mozilla/5.0'
        }

    # ── TOTP ─────────────────────────────────────────────────────────────────

    def _get_totp(self):
        if not PYOTP_AVAILABLE or not self.twofactor_secret:
            return None
        try:
            return pyotp.TOTP(self.twofactor_secret).now()
        except Exception as e:
            logger.error(f"[TOTP] {e}")
            return None

    # ── CSRF ─────────────────────────────────────────────────────────────────

    def _fetch_csrf(self):
        try:
            r = requests.post(
                "https://auth.roblox.com/v2/logout",
                headers=self.headers, timeout=5, json={}
            )
            token = r.headers.get('X-CSRF-TOKEN') or r.headers.get('x-csrf-token')
            logger.info(f"[CSRF] logout HTTP {r.status_code} → {'✅ ' + token[:15] if token else '❌'}")
            if token:
                self.headers['X-CSRF-TOKEN'] = token
                time.sleep(DELAY_AFTER_CSRF)
                return True
            r2 = requests.post(
                "https://auth.roblox.com/v1/authentication-ticket",
                headers=self.headers, timeout=5, json={}
            )
            token = r2.headers.get('X-CSRF-TOKEN') or r2.headers.get('x-csrf-token')
            logger.info(f"[CSRF] auth-ticket HTTP {r2.status_code} → {'✅ ' + token[:15] if token else '❌'}")
            if token:
                self.headers['X-CSRF-TOKEN'] = token
                time.sleep(DELAY_AFTER_CSRF)
                return True
            logger.error("[CSRF] ❌ Aucun token récupéré")
            return False
        except Exception as e:
            logger.error(f"[CSRF] ❌ {e}")
            return False

    def _set_csrf(self):
        if not verify_cookie_validity(self.roblosecurity):
            new_cookie = reauthenticate_roblox()
            if not new_cookie:
                return False
            self.roblosecurity = new_cookie
            self.headers['Cookie'] = f'.ROBLOSECURITY={new_cookie}'
        return self._fetch_csrf()

    def _refresh_cookie_and_csrf(self):
        logger.info("[Refresh] Force réauth + CSRF...")
        new_cookie = reauthenticate_roblox()
        if not new_cookie:
            return False
        self.roblosecurity = new_cookie
        self.headers['Cookie'] = f'.ROBLOSECURITY={new_cookie}'
        self.headers.pop('X-CSRF-TOKEN', None)
        return self._fetch_csrf()

    # ── PAYOUT REQUEST ───────────────────────────────────────────────────────

    def _payout_request(self, user_id, amount, extra_headers=None):
        h = self.headers.copy()
        if extra_headers:
            h.update(extra_headers)
        return requests.post(
            f"https://groups.roblox.com/v1/groups/{self.group_id}/payouts",
            headers=h,
            json={"PayoutType": 1, "Recipients": [{"amount": amount, "recipientId": user_id, "recipientType": 0}]},
            timeout=10
        )

    # ── 2FA ──────────────────────────────────────────────────────────────────

    def _resolve_2fa_code(self, method_hint="authenticator"):
        """
        Résout le code 2FA en utilisant TOTP ou IMAP selon la disponibilité.
        method_hint : 'authenticator' | 'email'
        Retourne le code (str) ou None.
        """
        # 1. TOTP — prioritaire si le secret est configuré et que c'est une auth app
        if method_hint == "authenticator" or not method_hint:
            code = self._get_totp()
            if code:
                logger.info(f"[2FA] 🔑 Code TOTP: {code}")
                return code, "authenticator"

        # 2. IMAP — fallback email ou si pas de TOTP configuré
        if IMAP_AVAILABLE:
            logger.info(f"[2FA] 📧 Pas de TOTP disponible — tentative IMAP (method={method_hint})...")
            code = fetch_roblox_email_code(timeout=60, poll_interval=5)
            if code:
                logger.info(f"[2FA] ✅ Code IMAP: {code}")
                return code, "email"
        else:
            logger.warning("[2FA] Module IMAP non disponible")

        logger.error("[2FA] ❌ Aucun code disponible (ni TOTP ni IMAP)")
        return None, None

    def _verify_2fa(self, sender_id, challenge_id, method_hint="authenticator"):
        """
        Vérifie le challenge 2FA en choisissant automatiquement
        l'endpoint Roblox correspondant au type de code obtenu.
        """
        code, resolved_method = self._resolve_2fa_code(method_hint)
        if not code:
            return None

        # Choix de l'endpoint selon la méthode résolue
        if resolved_method == "email":
            endpoint = f"https://twostepverification.roblox.com/v1/users/{sender_id}/challenges/email/verify"
        else:
            endpoint = f"https://twostepverification.roblox.com/v1/users/{sender_id}/challenges/authenticator/verify"

        try:
            r = requests.post(
                endpoint,
                headers=self.headers,
                json={"actionType": "Generic", "challengeId": challenge_id, "code": code},
                timeout=10
            )
            logger.info(f"[2FA] {resolved_method} verify → HTTP {r.status_code}")
            if r.status_code != 200:
                logger.error(f"[2FA] ❌ {r.text[:200]}")

                # Si authenticator a échoué, tenter IMAP en dernier recours
                if resolved_method == "authenticator" and IMAP_AVAILABLE:
                    logger.info("[2FA] 🔄 Authenticator échoué → tentative IMAP fallback...")
                    imap_code = fetch_roblox_email_code(timeout=60, poll_interval=5)
                    if imap_code:
                        email_endpoint = f"https://twostepverification.roblox.com/v1/users/{sender_id}/challenges/email/verify"
                        r2 = requests.post(
                            email_endpoint,
                            headers=self.headers,
                            json={"actionType": "Generic", "challengeId": challenge_id, "code": imap_code},
                            timeout=10
                        )
                        logger.info(f"[2FA] IMAP fallback → HTTP {r2.status_code}")
                        if r2.status_code == 200:
                            data2 = r2.json()
                            if "errors" not in data2:
                                vtoken = data2.get("verificationToken")
                                logger.info(f"[2FA] ✅ Token IMAP fallback: {vtoken[:20] if vtoken else 'None'}...")
                                return vtoken
                return None

            data = r.json()
            if "errors" in data:
                logger.error(f"[2FA] ❌ {data['errors'][0].get('message')}")
                return None
            vtoken = data.get("verificationToken")
            logger.info(f"[2FA] ✅ Token: {vtoken[:20] if vtoken else 'None'}...")
            return vtoken
        except Exception as e:
            logger.error(f"[2FA] ❌ Exception: {e}")
            return None

    # Alias pour compatibilité avec l'ancien code
    def _verify_totp(self, sender_id, challenge_id):
        return self._verify_2fa(sender_id, challenge_id, method_hint="authenticator")

    # ── CHALLENGE ────────────────────────────────────────────────────────────

    def _continue_challenge(self, challenge_id, challenge_type, metadata):
        try:
            resp = requests.post(
                "https://apis.roblox.com/challenge/v1/continue",
                headers=self.headers,
                json={"challengeId": challenge_id, "challengeType": challenge_type, "challengeMetadata": json.dumps(metadata)},
                timeout=10
            )
            logger.info(f"[Challenge] continue {challenge_type} → HTTP {resp.status_code}")
            return resp
        except Exception as e:
            logger.error(f"[Challenge] ❌ {e}")
            return None

    def _handle_blocksession(self, user_id, amount, retry_count, max_retries):
        if retry_count >= max_retries:
            return False, "Session bloquée — max retries atteint"
        logger.warning(f"[BlockSession] Attente {DELAY_BLOCKSESSION}s puis réauth complète...")
        time.sleep(DELAY_BLOCKSESSION)
        if not self._refresh_cookie_and_csrf():
            return False, "Réauth échouée après blocksession"
        return self.payout(user_id, amount, retry_count + 1, max_retries)

    # ── PAYOUT PRINCIPAL ─────────────────────────────────────────────────────

    def payout(self, user_id, amount, retry_count=0, max_retries=3):
        if retry_count == 0:
            if not self._set_csrf():
                return False, "Cookie invalide ou expiré"

        logger.info(f"[Payout] tentative {retry_count+1}/{max_retries+1}: {amount} R$ → {user_id}")
        r = self._payout_request(user_id, amount)

        if r.status_code == 200:
            logger.info("✅ [Payout] Réussi!")
            return True, "ok"

        challenge_type     = r.headers.get("rblx-challenge-type", "").lower()
        challenge_id       = r.headers.get("rblx-challenge-id", "")
        challenge_meta_b64 = r.headers.get("rblx-challenge-metadata", "")

        if not challenge_type or not challenge_id:
            logger.error(f"[Payout] ❌ Pas de challenge headers — HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code == 403 and retry_count < max_retries:
                logger.warning("[Payout] 403 sans challenge → réauth + retry")
                new_cookie = reauthenticate_roblox()
                if new_cookie:
                    self.roblosecurity = new_cookie
                    self.headers['Cookie'] = f'.ROBLOSECURITY={new_cookie}'
                    self.headers.pop('X-CSRF-TOKEN', None)
                    if self._fetch_csrf():
                        return self.payout(user_id, amount, retry_count + 1, max_retries)
            return False, f"No challenge headers (HTTP {r.status_code})"

        # ── CHEF ─────────────────────────────────────────────────────────────
        if challenge_type == "chef":
            logger.info("[Chef] 👨‍🍳 Challenge")
            try:
                chef_meta = json.loads(base64.b64decode(challenge_meta_b64))
            except Exception as e:
                return False, f"Chef metadata decode failed: {e}"

            cont = self._continue_challenge(challenge_id, "chef", chef_meta)
            if not cont:
                return False, "Chef continue failed"

            cont_data     = cont.json()
            next_type     = cont_data.get("challengeType", "")
            next_meta_raw = cont_data.get("challengeMetadata", "")

            if next_type == "":
                logger.info("[Chef] ✅ Pas de 2FA")
                time.sleep(DELAY_BEFORE_FINAL)
                final = self._payout_request(user_id, amount, {
                    "rblx-challenge-id":       challenge_id,
                    "rblx-challenge-type":     "chef",
                    "rblx-challenge-metadata": challenge_meta_b64
                })

            elif next_type == "twostepverification":
                logger.info("[Chef] 🔐 2FA requis")
                try:
                    tfa_meta   = json.loads(next_meta_raw)
                    tfa_user   = tfa_meta["userId"]
                    tfa_cid    = tfa_meta["challengeId"]
                    tfa_method = tfa_meta.get("mediaType", "authenticator").lower()
                except Exception:
                    return False, "2FA metadata parse failed"

                vtoken = self._verify_2fa(tfa_user, tfa_cid, method_hint=tfa_method)
                if not vtoken:
                    return False, "2FA failed (TOTP + IMAP)"

                time.sleep(DELAY_AFTER_2FA)
                cont2 = self._continue_challenge(challenge_id, "twostepverification", {
                    "verificationToken": vtoken,
                    "rememberDevice":    False,
                    "userId":            tfa_user,
                    "challengeId":       tfa_cid,
                })
                if not cont2 or cont2.status_code >= 400:
                    return False, "2FA continue failed"
                if cont2.json().get("challengeType") == "blocksession":
                    return self._handle_blocksession(user_id, amount, retry_count, max_retries)

                time.sleep(DELAY_BEFORE_FINAL)
                final = self._payout_request(user_id, amount)

            elif next_type == "blocksession":
                return self._handle_blocksession(user_id, amount, retry_count, max_retries)
            else:
                return False, f"Unknown next challenge: {next_type}"

            if final.status_code == 200:
                logger.info("✅ [Chef] Payout réussi!")
                return True, "ok"
            logger.error(f"❌ [Chef] {final.text[:200]}")
            return False, final.text

        # ── 2FA DIRECT ───────────────────────────────────────────────────────
        elif challenge_type == "twostepverification":
            logger.info("[2FA] 🔐 Challenge direct")
            try:
                meta       = json.loads(base64.b64decode(challenge_meta_b64))
                sender     = meta["userId"]
                cid        = meta["challengeId"]
                tfa_method = meta.get("mediaType", "authenticator").lower()
            except Exception:
                return False, "2FA metadata parse failed"

            vtoken = self._verify_2fa(sender, cid, method_hint=tfa_method)
            if not vtoken:
                return False, "2FA failed (TOTP + IMAP)"

            time.sleep(DELAY_AFTER_2FA)
            cont2 = self._continue_challenge(challenge_id, "twostepverification", {
                "verificationToken": vtoken,
                "rememberDevice":    False,
                "userId":            sender,
                "challengeId":       cid,
            })
            if not cont2 or cont2.status_code >= 400:
                return False, "2FA continue failed"
            if cont2.json().get("challengeType") == "blocksession":
                return self._handle_blocksession(user_id, amount, retry_count, max_retries)

            time.sleep(DELAY_BEFORE_FINAL)
            final = self._payout_request(user_id, amount)
            if final.status_code == 200:
                return True, "ok"
            return False, final.text

        elif challenge_type == "blocksession":
            return self._handle_blocksession(user_id, amount, retry_count, max_retries)
        else:
            return False, f"Unknown challenge: {challenge_type}"


# ============================================================================
# TRANSFER
# ============================================================================

def transfer_robux_opencloud(target_user_id, amount_robux):
    try:
        response = requests.post(
            f"https://apis.roblox.com/cloud/v2/groups/{GROUP_ID}/payouts",
            json={"payouts": [{"userId": str(target_user_id), "amount": amount_robux}]},
            headers={"x-api-key": ROBLOX_API_KEY, "Content-Type": "application/json"},
            timeout=10
        )
        proof = {
            "method":      "open_cloud",
            "recipientId": target_user_id,
            "amountSent":  amount_robux,
            "httpStatus":  response.status_code,
            "response":    response.json() if response.content else {},
            "timestamp":   datetime.utcnow().isoformat() + "Z"
        }
        if response.status_code == 200:
            logger.info(f"✅ [OpenCloud] {amount_robux} R$ → {target_user_id}")
            return True, proof
        logger.error(f"❌ [OpenCloud] HTTP {response.status_code}")
        return False, proof
    except Exception as e:
        return False, {"error": str(e), "timestamp": datetime.utcnow().isoformat() + "Z"}


def transfer_robux_cookie_2fa(target_user_id, amount_robux):
    cookie = get_valid_cookie()
    if not cookie:
        return False, {"error": "Cookie invalide", "timestamp": datetime.utcnow().isoformat() + "Z"}
    payout  = RobloxPayout(cookie, GROUP_ID, ROBLOX_2FA_SECRET)
    success, detail = payout.payout(target_user_id, amount_robux)
    method  = "cookie_totp" if ROBLOX_2FA_SECRET else ("cookie_imap" if IMAP_AVAILABLE else "cookie_only")
    return success, {
        "method":      method,
        "recipientId": target_user_id,
        "amountSent":  amount_robux,
        "httpStatus":  200 if success else 403,
        "response":    {"detail": detail},
        "timestamp":   datetime.utcnow().isoformat() + "Z"
    }


def transfer_robux(target_user_id, amount_robux):
    if ROBLOX_API_KEY:
        success, proof = transfer_robux_opencloud(target_user_id, amount_robux)
        if success:
            return True, proof
        logger.warning("[Payout] OpenCloud échoué → fallback Cookie+2FA")
    if ACCOUNT_COOKIE or (ROBLOX_USERNAME and ROBLOX_PASSWORD):
        return transfer_robux_cookie_2fa(target_user_id, amount_robux)
    return False, {"error": "Aucune méthode configurée", "timestamp": datetime.utcnow().isoformat() + "Z"}


# ============================================================================
# PAYOUT ASYNC
# ============================================================================

def _process_payout_async(donation_id, target_user_id, amount,
                           discord_message_id, player_id, amount_robux, final_amount):
    logger.info(f"[Async] 🚀 Payout #{donation_id}: {amount} R$ → {target_user_id}")
    try:
        success, proof = transfer_robux(target_user_id, amount)
        conn = get_db()
        c    = conn.cursor()
        if success:
            c.execute("UPDATE donations SET status='completed', processed_at=NOW() WHERE id=%s", (donation_id,))
            conn.commit()
            logger.info(f"✅ [Async] #{donation_id} OK")
            donor_info  = get_user_info(player_id)
            target_info = get_user_info(target_user_id)
            edit_discord_success(
                discord_message_id,
                donor_info.get("name",  str(player_id))        if donor_info  else str(player_id),       player_id,
                target_info.get("name", str(target_user_id))   if target_info else str(target_user_id),  target_user_id,
                amount_robux, final_amount, amount_robux - final_amount, donation_id, proof
            )
        else:
            c.execute("UPDATE donations SET status='failed', retry_count=retry_count+1, last_retry=NOW() WHERE id=%s", (donation_id,))
            conn.commit()
            logger.error(f"❌ [Async] #{donation_id} échoué")
            donor_info  = get_user_info(player_id)
            target_info = get_user_info(target_user_id)
            edit_discord_failed(
                discord_message_id,
                donor_info.get("name",  str(player_id))        if donor_info  else str(player_id),       player_id,
                target_info.get("name", str(target_user_id))   if target_info else str(target_user_id),  target_user_id,
                amount_robux, final_amount, donation_id,
                str(proof.get("response", {}).get("detail", "Erreur inconnue"))
            )
        c.close()
        conn.close()
    except Exception as e:
        logger.error(f"[Async] ❌ Exception #{donation_id}: {e}")
        import traceback
        logger.error(traceback.format_exc())


# ============================================================================
# DATABASE
# ============================================================================

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_db()
    c    = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS donations (
        id SERIAL PRIMARY KEY,
        player_id BIGINT NOT NULL,
        target_player_id BIGINT NOT NULL,
        devproduct_id TEXT NOT NULL,
        amount_robux INTEGER NOT NULL,
        final_amount INTEGER,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT NOW(),
        processed_at TIMESTAMP,
        retry_count INTEGER DEFAULT 0,
        last_retry TIMESTAMP,
        discord_message_id TEXT
    )''')
    conn.commit()
    c.close()
    conn.close()
    logger.info("✅ Table donations prête")


# ============================================================================
# ROBLOX API HELPERS
# ============================================================================

def verify_webhook_signature(data, signature):
    if not WEBHOOK_SECRET:
        return True
    expected = hmac.new(WEBHOOK_SECRET.encode(), data, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def get_user_info(user_id):
    try:
        resp = requests.get(f"https://users.roblox.com/v1/users/{user_id}", timeout=3)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error(f"[User] {e}")
    return None


def get_group_membership(user_id, group_id):
    try:
        resp = requests.get(f"https://groups.roblox.com/v1/users/{user_id}/groups", timeout=3)
        if resp.status_code == 200:
            for group in resp.json().get("data", []):
                if group["group"]["id"] == group_id:
                    return {"in_group": True, "join_date": group.get("joinedDate"), "role": group.get("role", {}).get("name")}
            return {"in_group": False}
    except Exception as e:
        logger.error(f"[Group] {e}")
    return {"in_group": False}


def check_eligibility(user_id, group_id, days_required=MIN_GROUP_TENURE_DAYS):
    membership = get_group_membership(user_id, group_id)
    if not membership.get("in_group"):
        return {"eligible": False, "reason": "Not in group"}
    if membership.get("join_date"):
        try:
            join_date = datetime.fromisoformat(membership["join_date"].replace("Z", "+00:00"))
            days      = (datetime.now(join_date.tzinfo) - join_date).days
            if days < days_required:
                return {"eligible": False, "reason": f"Not long enough ({days}/{days_required} days)"}
        except Exception:
            pass
    return {"eligible": True, "reason": "Meets all requirements"}


# ============================================================================
# DISCORD
# ============================================================================

def send_discord_notification(donor_id, donor_name, target_id, target_name,
                               amount, final_amount, taxes, donation_id, estimated_date):
    if not DISCORD_WEBHOOK_URL:
        return None
    embed = {
        "title":  "💰 Nouvelle Donation en attente",
        "color":  0xFFD700,
        "fields": [
            {"name": "👤 Donateur",                    "value": f"`{donor_name}` (ID: `{donor_id}`)",  "inline": False},
            {"name": "🎯 Receveur",                    "value": f"`{target_name}` (ID: `{target_id}`)", "inline": False},
            {"name": "💵 Montant brut",                "value": f"`{amount} Robux`",                   "inline": True},
            {"name": f"🏦 Taxe (-{int(TAX_RATE*100)}%)", "value": f"`{taxes} Robux`",                  "inline": True},
            {"name": "✨ Montant net",                 "value": f"`{final_amount} Robux`",             "inline": True},
            {"name": "📅 Transfert prévu",             "value": f"`{estimated_date}`",                 "inline": False},
            {"name": "🔖 ID",                          "value": f"`#{donation_id}`",                   "inline": True},
        ],
        "footer":    {"text": "PolyVoice Donation System"},
        "timestamp": datetime.utcnow().isoformat()
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL + "?wait=true", json={"embeds": [embed]}, timeout=5)
        if resp.status_code in (200, 204):
            return resp.json().get("id")
    except Exception as e:
        logger.error(f"[Discord] {e}")
    return None


def edit_discord_success(message_id, donor_name, donor_id, target_name, target_id,
                          amount, final_amount, taxes, donation_id, proof):
    if not DISCORD_WEBHOOK_URL or not message_id:
        return
    proof_str = json.dumps(proof, indent=2, ensure_ascii=False)
    if len(proof_str) > 950:
        proof_str = proof_str[:950] + "\n...(tronqué)"
    embed = {
        "title":  "✅ Donation transférée!",
        "color":  0x00FF7F,
        "fields": [
            {"name": "👤 Donateur",                    "value": f"`{donor_name}` (ID: `{donor_id}`)",  "inline": False},
            {"name": "🎯 Receveur",                    "value": f"`{target_name}` (ID: `{target_id}`)", "inline": False},
            {"name": "💵 Brut",                        "value": f"`{amount} Robux`",                   "inline": True},
            {"name": f"🏦 Taxe (-{int(TAX_RATE*100)}%)", "value": f"`{taxes} Robux`",                  "inline": True},
            {"name": "✨ Net",                         "value": f"`{final_amount} Robux`",             "inline": True},
            {"name": "🔖 ID",                         "value": f"`#{donation_id}`",                   "inline": True},
            {"name": "📄 Preuve",                      "value": f"```json\n{proof_str}\n```",          "inline": False},
        ],
        "footer":    {"text": "PolyVoice Donation System"},
        "timestamp": datetime.utcnow().isoformat()
    }
    try:
        requests.patch(f"{DISCORD_WEBHOOK_URL}/messages/{message_id}", json={"embeds": [embed]}, timeout=5)
    except Exception as e:
        logger.error(f"[Discord edit] {e}")


def edit_discord_failed(message_id, donor_name, donor_id, target_name, target_id,
                         amount, final_amount, donation_id, reason):
    if not DISCORD_WEBHOOK_URL or not message_id:
        return
    embed = {
        "title":  "❌ Échec du transfert",
        "color":  0xFF4444,
        "fields": [
            {"name": "👤 Donateur",  "value": f"`{donor_name}` (ID: `{donor_id}`)",   "inline": False},
            {"name": "🎯 Receveur",  "value": f"`{target_name}` (ID: `{target_id}`)", "inline": False},
            {"name": "💵 Brut",      "value": f"`{amount} Robux`",                    "inline": True},
            {"name": "✨ Net",       "value": f"`{final_amount} Robux`",              "inline": True},
            {"name": "🔖 ID",       "value": f"`#{donation_id}`",                    "inline": True},
            {"name": "❌ Statut",   "value": "`Échec — sera retenté`",               "inline": True},
            {"name": "⚠️ Raison",  "value": f"`{reason}`",                           "inline": False},
        ],
        "footer":    {"text": "PolyVoice Donation System"},
        "timestamp": datetime.utcnow().isoformat()
    }
    try:
        requests.patch(f"{DISCORD_WEBHOOK_URL}/messages/{message_id}", json={"embeds": [embed]}, timeout=5)
    except Exception as e:
        logger.error(f"[Discord edit] {e}")


# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/webhook/devproduct', methods=['POST'])
def handle_devproduct_purchase():
    try:
        sig = request.headers.get('X-Roblox-Signature')
        if sig and not verify_webhook_signature(request.data, sig):
            return jsonify({"error": "Invalid signature"}), 401

        data           = request.get_json()
        user_id        = data.get("userId")
        target_user_id = data.get("targetUserId")
        devproduct_id  = str(data.get("devProductId"))

        if not all([user_id, target_user_id, devproduct_id]):
            return jsonify({"error": "Missing fields"}), 400
        if devproduct_id not in DEVPRODUCT_AMOUNTS:
            return jsonify({"error": "Unknown DevProduct"}), 400

        amount         = DEVPRODUCT_AMOUNTS[devproduct_id]
        final_amount   = math.ceil(amount * (1 - TAX_RATE))
        taxes          = amount - final_amount
        estimated_date = (datetime.now() + timedelta(days=WAIT_DAYS)).strftime("%d/%m/%Y")

        donor_info  = get_user_info(user_id)
        target_info = get_user_info(target_user_id)
        donor_name  = donor_info.get("name",  str(user_id))         if donor_info  else str(user_id)
        target_name = target_info.get("name", str(target_user_id))  if target_info else str(target_user_id)

        conn = get_db()
        c    = conn.cursor()
        c.execute(
            '''INSERT INTO donations (player_id, target_player_id, devproduct_id, amount_robux, final_amount, status)
               VALUES (%s, %s, %s, %s, %s, 'pending') RETURNING id''',
            (user_id, target_user_id, devproduct_id, amount, final_amount)
        )
        donation_id = c.fetchone()["id"]

        message_id = send_discord_notification(user_id, donor_name, target_user_id, target_name,
                                                amount, final_amount, taxes, donation_id, estimated_date)
        if message_id:
            c.execute('UPDATE donations SET discord_message_id=%s WHERE id=%s', (message_id, donation_id))
        conn.commit()
        c.close()
        conn.close()

        return jsonify({
            "success":       True,
            "donationId":    donation_id,
            "message":       f"{final_amount} Robux dans {WAIT_DAYS} jours",
            "estimatedDate": (datetime.now() + timedelta(days=WAIT_DAYS)).isoformat()
        }), 200
    except Exception as e:
        logger.error(f"[Webhook] {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/pending', methods=['GET'])
def get_pending_donations():
    try:
        conn   = get_db()
        c      = conn.cursor()
        cutoff = datetime.now() - timedelta(days=WAIT_DAYS)
        c.execute(
            '''SELECT * FROM donations WHERE status='pending' AND created_at<=%s AND retry_count<5
               ORDER BY created_at ASC LIMIT 5''',
            (cutoff,)
        )
        donations = [dict(r) for r in c.fetchall()]
        c.close()
        conn.close()
        return jsonify({"success": True, "donations": donations}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/donations/<int:donation_id>/status', methods=['POST'])
def update_donation_status(donation_id):
    try:
        data           = request.get_json()
        new_status     = data.get("status")
        roblox_proof   = data.get("roblox_proof")
        fail_reason    = data.get("reason", "Erreur inconnue")
        force_transfer = data.get("force_transfer", False)

        if new_status not in ("completed", "pending", "requeue", "failed"):
            return jsonify({"error": "Invalid status"}), 400

        conn = get_db()
        c    = conn.cursor()
        c.execute('SELECT * FROM donations WHERE id=%s', (donation_id,))
        row = c.fetchone()
        if not row:
            c.close()
            conn.close()
            return jsonify({"error": "Donation not found"}), 404
        donation = dict(row)

        if new_status == "completed" and force_transfer:
            c.execute("UPDATE donations SET status='processing' WHERE id=%s", (donation_id,))
            conn.commit()
            c.close()
            conn.close()
            payout_executor.submit(
                _process_payout_async, donation_id,
                donation['target_player_id'], donation['final_amount'],
                donation.get('discord_message_id'), donation['player_id'],
                donation['amount_robux'], donation['final_amount']
            )
            return jsonify({"success": True, "async": True}), 202

        if new_status == "completed":
            c.execute("UPDATE donations SET status='completed', processed_at=NOW() WHERE id=%s", (donation_id,))
        else:
            c.execute(
                "UPDATE donations SET status=%s, retry_count=retry_count+1, last_retry=NOW() WHERE id=%s",
                (new_status, donation_id)
            )
        conn.commit()

        donor_info  = get_user_info(donation['player_id'])
        target_info = get_user_info(donation['target_player_id'])
        donor_name  = donor_info.get("name",  str(donation['player_id']))        if donor_info  else str(donation['player_id'])
        target_name = target_info.get("name", str(donation['target_player_id'])) if target_info else str(donation['target_player_id'])
        taxes       = donation['amount_robux'] - donation['final_amount']
        msg_id      = donation.get('discord_message_id')

        if new_status == "completed" and roblox_proof:
            edit_discord_success(msg_id, donor_name, donation['player_id'],
                                  target_name, donation['target_player_id'],
                                  donation['amount_robux'], donation['final_amount'], taxes, donation_id, roblox_proof)
        elif new_status in ("failed", "requeue"):
            edit_discord_failed(msg_id, donor_name, donation['player_id'],
                                 target_name, donation['target_player_id'],
                                 donation['amount_robux'], donation['final_amount'], donation_id, fail_reason)

        c.close()
        conn.close()
        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/status/<int:donation_id>', methods=['GET'])
def get_donation_status(donation_id):
    try:
        conn = get_db()
        c    = conn.cursor()
        c.execute('SELECT * FROM donations WHERE id=%s', (donation_id,))
        d = c.fetchone()
        c.close()
        conn.close()
        if not d:
            return jsonify({"error": "Not found"}), 404
        return jsonify({
            "id":           d['id'],
            "status":       d['status'],
            "amount":       d['amount_robux'],
            "finalAmount":  d['final_amount'],
            "createdAt":    str(d['created_at']),
            "processedAt":  str(d['processed_at']),
            "targetUserId": d['target_player_id']
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/list', methods=['GET'])
def list_donations():
    try:
        conn = get_db()
        c    = conn.cursor()
        c.execute('SELECT * FROM donations ORDER BY created_at DESC LIMIT 50')
        rows = c.fetchall()
        c.close()
        conn.close()
        return jsonify({"success": True, "count": len(rows), "donations": [dict(r) for r in rows]}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status":     "ok",
        "timestamp":  datetime.now().isoformat(),
        "2fa_totp":   PYOTP_AVAILABLE and bool(ROBLOX_2FA_SECRET),
        "2fa_imap":   IMAP_AVAILABLE,
        "opencloud":  bool(ROBLOX_API_KEY),
        "cookie":     bool(ACCOUNT_COOKIE),
        "reauth":     bool(ROBLOX_USERNAME and ROBLOX_PASSWORD),
    }), 200


@app.route('/', methods=['GET'])
def index():
    return jsonify({"service": "PolyVoice Donation Backend", "status": "running"}), 200


# ============================================================================
# ADMIN ROUTES
# ============================================================================

def _admin_auth(req):
    return req.args.get('password', '') == ADMIN_PASSWORD


@app.route('/admin/stats', methods=['GET'])
def admin_stats():
    if not _admin_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db()
        c    = conn.cursor()
        c.execute("SELECT COUNT(*) AS n FROM donations");                                       total     = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) AS n FROM donations WHERE status='pending'");                pending   = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) AS n FROM donations WHERE status='completed'");              completed = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) AS n FROM donations WHERE status IN ('failed','processing')"); failed  = c.fetchone()["n"]
        c.close()
        conn.close()
        return jsonify({"total": total, "pending": pending, "completed": completed, "failed": failed}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/donations', methods=['GET'])
def admin_list_donations():
    if not _admin_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db()
        c    = conn.cursor()
        c.execute('SELECT id,player_id,target_player_id,amount_robux,final_amount,status,created_at FROM donations ORDER BY created_at DESC')
        rows   = c.fetchall()
        c.close()
        conn.close()
        result = []
        for row in rows:
            row = dict(row)
            di  = get_user_info(row["player_id"])
            ti  = get_user_info(row["target_player_id"])
            result.append({**row,
                "donor_name":  di.get("name") if di else None,
                "target_name": ti.get("name") if ti else None,
                "created_at":  str(row["created_at"])
            })
        return jsonify({"donations": result}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/donations/<int:donation_id>/status', methods=['POST'])
def admin_update_status(donation_id):
    if not _admin_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        data           = request.get_json()
        new_status     = data.get("status")
        force_transfer = data.get("force_transfer", False)

        if new_status not in ("pending", "completed", "failed"):
            return jsonify({"error": "Invalid status"}), 400

        conn = get_db()
        c    = conn.cursor()
        c.execute('SELECT * FROM donations WHERE id=%s', (donation_id,))
        row = c.fetchone()
        if not row:
            c.close()
            conn.close()
            return jsonify({"error": "Not found"}), 404
        donation = dict(row)

        if new_status == "completed" and force_transfer:
            c.execute("UPDATE donations SET status='processing' WHERE id=%s", (donation_id,))
            conn.commit()
            c.close()
            conn.close()
            payout_executor.submit(
                _process_payout_async, donation_id,
                donation['target_player_id'], donation['final_amount'],
                donation.get('discord_message_id'), donation['player_id'],
                donation['amount_robux'], donation['final_amount']
            )
            return jsonify({"success": True, "async": True, "message": "Payout lancé en arrière-plan"}), 202

        c.execute('UPDATE donations SET status=%s WHERE id=%s', (new_status, donation_id))
        conn.commit()
        c.close()
        conn.close()
        return jsonify({"success": True, "transferred": False}), 200
    except Exception as e:
        logger.error(f"[AdminStatus] {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/admin/donations/<int:donation_id>', methods=['DELETE'])
def admin_delete_donation(donation_id):
    if not _admin_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db()
        c    = conn.cursor()
        c.execute('DELETE FROM donations WHERE id=%s', (donation_id,))
        conn.commit()
        c.close()
        conn.close()
        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/cleanup', methods=['POST'])
def admin_cleanup_pending():
    if not _admin_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db()
        c    = conn.cursor()
        c.execute("SELECT COUNT(*) AS n FROM donations WHERE status='pending'")
        cnt = c.fetchone()["n"]
        c.execute("DELETE FROM donations WHERE status='pending'")
        conn.commit()
        c.close()
        conn.close()
        return jsonify({"success": True, "deleted": cnt}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/mark-completed', methods=['POST'])
def admin_mark_completed():
    if not _admin_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db()
        c    = conn.cursor()
        c.execute("UPDATE donations SET status='completed' WHERE status='pending'")
        conn.commit()
        updated = c.rowcount
        c.close()
        conn.close()
        return jsonify({"success": True, "updated": updated}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================================
# STARTUP
# ============================================================================

if DATABASE_URL:
    try:
        init_db()
        logger.info("✅ Connecté à Supabase/PostgreSQL")
    except Exception as e:
        logger.error(f"❌ DB: {e}")
else:
    logger.warning("⚠️ DATABASE_URL non configurée")

logger.info(f"🔑 API Key:   {'✅' if ROBLOX_API_KEY else '❌'}")
logger.info(f"🍪 Cookie:    {'✅' if ACCOUNT_COOKIE else '❌'}")
logger.info(f"🔐 TOTP:      {'✅' if ROBLOX_2FA_SECRET and PYOTP_AVAILABLE else '❌'}")
logger.info(f"📧 IMAP 2FA:  {'✅' if IMAP_AVAILABLE else '❌'}")
logger.info(f"🔄 Réauth:    {'✅' if ROBLOX_USERNAME and ROBLOX_PASSWORD else '❌'}")
logger.info(f"⚡ Threads:   {payout_executor._max_workers}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False, use_reloader=False)
