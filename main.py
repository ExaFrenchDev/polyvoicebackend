import discord
from discord.ext import commands, tasks
import os
import logging
from datetime import datetime, timedelta
import requests
import json
import base64
import importlib
import subprocess
import sys
import time
import asyncio
import threading
from dotenv import load_dotenv

modules = ["pyotp"]

def install_missing_modules():
    for module in modules:
        try:
            importlib.import_module(module.split('==')[0].replace('.py', '').replace('-', '_'))
        except ImportError:
            print(f"Installation du module manquant : {module}")
            subprocess.check_call([sys.executable, "-m", "pip", "install", module, "--break-system-packages"])

install_missing_modules()

try:
    import pyotp
    PYOTP_AVAILABLE = True
except ImportError:
    PYOTP_AVAILABLE = False

load_dotenv()

# Configuration
DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))
BACKEND_URL        = os.getenv("BACKEND_URL", "https://polyvoicebackend.onrender.com")
GROUP_ID           = int(os.getenv("GROUP_ID", "0"))
ACCOUNT_COOKIE     = os.getenv("ROBLOX_ACCOUNT_COOKIE", "")
ROBLOX_2FA_SECRET  = os.getenv("ROBLOX_2FA_SECRET", "")
ROBLOX_API_KEY     = os.getenv("ROBLOX_API_KEY", "")
ADMIN_DISCORD_IDS  = list(map(int, os.getenv("ADMIN_DISCORD_IDS", "").split(","))) if os.getenv("ADMIN_DISCORD_IDS") else []
ROBLOX_USERNAME    = os.getenv("ROBLOX_USERNAME", "")
ROBLOX_PASSWORD    = os.getenv("ROBLOX_PASSWORD", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MIN_GROUP_TENURE_DAYS = 7

DELAY_AFTER_CSRF      = 2.0
DELAY_AFTER_2FA       = 10.0
DELAY_BEFORE_FINAL    = 10.0
DELAY_BLOCKSESSION    = 15.0

COOKIE_REFRESH_INTERVAL = 3600
cookie_last_refresh = datetime.now()
cookie_lock = threading.Lock()


# ============================================================================
# COOKIE MANAGEMENT & REAUTH
# ============================================================================

def reauthenticate_roblox():
    """Réauthentifie via username/password et retourne le nouveau cookie"""
    if not ROBLOX_USERNAME or not ROBLOX_PASSWORD:
        logger.warning("⚠️ ROBLOX_USERNAME ou ROBLOX_PASSWORD non configurés")
        return None

    try:
        logger.info(f"🔄 Réauthentification en cours pour {ROBLOX_USERNAME}...")
        session = requests.Session()

        # Étape 1 : récupérer un CSRF token via logout (sans cookie)
        pre = session.post(
            "https://auth.roblox.com/v2/logout",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:148.0) Gecko/20100101 Firefox/148.0"},
            timeout=5,
            json={}
        )
        csrf = pre.headers.get('X-CSRF-TOKEN') or pre.headers.get('x-csrf-token', '')
        logger.info(f"[Reauth] CSRF pré-login: {'✅ ' + csrf[:15] if csrf else '❌ absent'}")

        # Étape 2 : login
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:148.0) Gecko/20100101 Firefox/148.0",
            "Content-Type": "application/json",
        }
        if csrf:
            headers["X-CSRF-TOKEN"] = csrf

        resp = session.post(
            "https://auth.roblox.com/v2/login",
            json={
                "ctype": "Username",
                "cvalue": ROBLOX_USERNAME,
                "passwd": ROBLOX_PASSWORD
            },
            headers=headers,
            timeout=10
        )

        logger.info(f"[Reauth] Login HTTP {resp.status_code}")

        if resp.status_code == 200:
            new_cookie = session.cookies.get('.ROBLOSECURITY')
            if new_cookie:
                logger.info(f"✅ Nouveau cookie obtenu: {new_cookie[:30]}...")
                os.environ['ROBLOX_ACCOUNT_COOKIE'] = new_cookie
                globals()['ACCOUNT_COOKIE'] = new_cookie
                return new_cookie
            else:
                logger.error("❌ Pas de .ROBLOSECURITY dans les cookies de réponse")
                return None

        elif resp.status_code == 403:
            # Roblox renvoie parfois un nouveau CSRF dans le 403
            new_csrf = resp.headers.get('X-CSRF-TOKEN') or resp.headers.get('x-csrf-token')
            if new_csrf:
                logger.info(f"[Reauth] Nouveau CSRF après 403: {new_csrf[:15]}, retry...")
                headers["X-CSRF-TOKEN"] = new_csrf
                resp2 = session.post(
                    "https://auth.roblox.com/v2/login",
                    json={"ctype": "Username", "cvalue": ROBLOX_USERNAME, "passwd": ROBLOX_PASSWORD},
                    headers=headers,
                    timeout=10
                )
                logger.info(f"[Reauth] Login retry HTTP {resp2.status_code}")
                if resp2.status_code == 200:
                    new_cookie = session.cookies.get('.ROBLOSECURITY')
                    if new_cookie:
                        logger.info(f"✅ Nouveau cookie obtenu (retry): {new_cookie[:30]}...")
                        os.environ['ROBLOX_ACCOUNT_COOKIE'] = new_cookie
                        globals()['ACCOUNT_COOKIE'] = new_cookie
                        return new_cookie
            try:
                data = resp.json()
                if data.get("errors"):
                    logger.error(f"❌ Login échoué: {data['errors'][0].get('message', 'Unknown')}")
            except:
                pass
            return None
        else:
            logger.error(f"❌ Login échoué HTTP {resp.status_code}: {resp.text[:200]}")
            return None

    except Exception as e:
        logger.error(f"❌ Erreur réauthentification: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


def verify_cookie_validity(cookie):
    """Vérifie le cookie sur un endpoint qui REQUIERT une vraie auth"""
    try:
        resp = requests.get(
            "https://users.roblox.com/v1/users/authenticated",
            headers={
                'Cookie': f'.ROBLOSECURITY={cookie}',
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:148.0) Gecko/20100101 Firefox/148.0'
            },
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            logger.info(f"✅ Cookie valide — connecté: {data.get('name')} (ID: {data.get('id')})")
            return True
        else:
            logger.warning(f"⚠️ Cookie invalide (HTTP {resp.status_code})")
            return False
    except Exception as e:
        logger.error(f"❌ Erreur vérification cookie: {e}")
        return False


def get_valid_cookie():
    """Retourne un cookie valide, réauthentifie si nécessaire"""
    global cookie_last_refresh

    current_cookie = globals().get('ACCOUNT_COOKIE', '')

    if current_cookie and verify_cookie_validity(current_cookie):
        return current_cookie

    logger.warning("🔄 Cookie invalide ou absent — réauthentification automatique...")
    new_cookie = reauthenticate_roblox()
    if new_cookie:
        with cookie_lock:
            cookie_last_refresh = datetime.now()
        return new_cookie

    logger.error("❌ Impossible d'obtenir un cookie valide")
    return None


def refresh_cookie_if_needed():
    """Refresh périodique du cookie"""
    global cookie_last_refresh

    with cookie_lock:
        now = datetime.now()
        if (now - cookie_last_refresh).total_seconds() > COOKIE_REFRESH_INTERVAL:
            logger.info("🔄 Refresh périodique du cookie...")
            new_cookie = reauthenticate_roblox()
            if new_cookie:
                cookie_last_refresh = now
                return new_cookie
    return None


# ============================================================================
# ROBLOX PAYOUT
# ============================================================================

class RobloxPayout:
    def __init__(self, roblosecurity, group_id, twofactor_secret=""):
        self.roblosecurity = roblosecurity
        self.group_id = group_id
        self.twofactor_secret = twofactor_secret
        self.headers = {
            'Cookie': '.ROBLOSECURITY=' + self.roblosecurity,
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:148.0) Gecko/20100101 Firefox/148.0'
        }

    def _get_totp(self):
        if not PYOTP_AVAILABLE or not self.twofactor_secret:
            return None
        try:
            return pyotp.TOTP(self.twofactor_secret).now()
        except Exception as e:
            logger.error(f"[2FA] Erreur TOTP: {e}")
            return None

    def _set_csrf(self):
        """Vérifie le cookie, réauthentifie si besoin, récupère le CSRF"""

        # 1. Vérifier la validité du cookie
        if not verify_cookie_validity(self.roblosecurity):
            logger.warning("[CSRF] Cookie invalide — réauthentification automatique...")
            new_cookie = reauthenticate_roblox()
            if new_cookie:
                self.roblosecurity = new_cookie
                self.headers['Cookie'] = f'.ROBLOSECURITY={new_cookie}'
                logger.info("[CSRF] ✅ Nouveau cookie appliqué")
            else:
                logger.error("[CSRF] ❌ Réauthentification échouée")
                return False

        # 2. Récupérer le CSRF token via POST logout (méthode standard Roblox)
        try:
            r = requests.post(
                "https://auth.roblox.com/v2/logout",
                headers=self.headers,
                timeout=5,
                json={}
            )
            token = r.headers.get('X-CSRF-TOKEN') or r.headers.get('x-csrf-token')
            logger.info(f"[CSRF] logout → HTTP {r.status_code}, token={'✅ ' + token[:20] if token else '❌ absent'}")

            if token:
                self.headers['X-CSRF-TOKEN'] = token
                time.sleep(DELAY_AFTER_CSRF)
                return True

            # Fallback : authentication-ticket
            r2 = requests.post(
                "https://auth.roblox.com/v1/authentication-ticket",
                headers=self.headers,
                timeout=5,
                json={}
            )
            token = r2.headers.get('X-CSRF-TOKEN') or r2.headers.get('x-csrf-token')
            logger.info(f"[CSRF] auth-ticket → HTTP {r2.status_code}, token={'✅ ' + token[:20] if token else '❌ absent'}")

            if token:
                self.headers['X-CSRF-TOKEN'] = token
                time.sleep(DELAY_AFTER_CSRF)
                return True

            logger.error("[CSRF] ❌ Impossible de récupérer le CSRF token")
            return False

        except Exception as e:
            logger.error(f"[CSRF] ❌ Erreur: {e}")
            return False

    def _payout_request(self, user_id, amount, extra_headers=None):
        h = self.headers.copy()
        if extra_headers:
            h.update(extra_headers)
        return requests.post(
            f"https://groups.roblox.com/v1/groups/{self.group_id}/payouts",
            headers=h,
            json={
                "PayoutType": 1,
                "Recipients": [{"amount": amount, "recipientId": user_id, "recipientType": 0}]
            },
            timeout=10
        )

    def _verify_totp(self, sender_id, challenge_id):
        code = self._get_totp()
        if not code:
            logger.error("[2FA] ❌ Pas de TOTP disponible")
            return None
        try:
            logger.info(f"[2FA] 📱 Envoi verify: sender_id={sender_id}, code={code}")
            r = requests.post(
                f"https://twostepverification.roblox.com/v1/users/{sender_id}/challenges/authenticator/verify",
                headers=self.headers,
                json={"actionType": "Generic", "challengeId": challenge_id, "code": code},
                timeout=10
            )
            logger.info(f"[2FA] HTTP {r.status_code}")
            if r.status_code != 200:
                logger.error(f"[2FA] ❌ HTTP {r.status_code}: {r.text[:200]}")
                return None
            data = r.json()
            if "errors" in data:
                logger.error(f"[2FA] ❌ Erreur: {data['errors'][0].get('message', 'unknown')}")
                return None
            vtoken = data.get("verificationToken")
            logger.info(f"[2FA] ✅ Token reçu: {vtoken[:20] if vtoken else 'None'}...")
            return vtoken
        except Exception as e:
            logger.error(f"[2FA] ❌ Exception: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def _continue_challenge(self, challenge_id, challenge_type, metadata):
        try:
            resp = requests.post(
                "https://apis.roblox.com/challenge/v1/continue",
                headers=self.headers,
                json={
                    "challengeId": challenge_id,
                    "challengeType": challenge_type,
                    "challengeMetadata": json.dumps(metadata)
                },
                timeout=10
            )
            logger.info(f"[Challenge] Continue {challenge_type}: HTTP {resp.status_code}")
            if resp.status_code >= 400:
                logger.warning(f"[Challenge] ⚠️ Response: {resp.text[:200]}")
            return resp
        except Exception as e:
            logger.error(f"[Challenge] ❌ Exception: {e}")
            return None

    def payout(self, user_id, amount, retry_count=0, max_retries=3):
        if not self._set_csrf():
            return False, "Cookie invalide ou expiré"

        logger.info(f"[Payout] 🔄 Tentative {retry_count + 1}/{max_retries + 1}: {amount} R$ → {user_id}")
        r = self._payout_request(user_id, amount)

        if r.status_code == 200:
            logger.info("✅ [Payout] Réussi sans challenge!")
            return True, "ok"

        challenge_type     = r.headers.get("rblx-challenge-type", "").lower()
        challenge_id       = r.headers.get("rblx-challenge-id", "")
        challenge_meta_b64 = r.headers.get("rblx-challenge-metadata", "")

        if not challenge_type or not challenge_id:
            logger.error(f"[Payout] ❌ Pas de challenge headers — HTTP {r.status_code}: {r.text[:200]}")
            # 403 sans challenge = cookie probablement mort → réessayer avec réauth
            if r.status_code == 403 and retry_count < max_retries:
                logger.warning("[Payout] 🔄 403 sans challenge — tentative de réauthentification...")
                new_cookie = reauthenticate_roblox()
                if new_cookie:
                    self.roblosecurity = new_cookie
                    self.headers['Cookie'] = f'.ROBLOSECURITY={new_cookie}'
                    time.sleep(2)
                    return self.payout(user_id, amount, retry_count + 1, max_retries)
            return False, f"No challenge headers (HTTP {r.status_code})"

        # ── CHEF CHALLENGE ──────────────────────────────────────────────────
        if challenge_type == "chef":
            logger.info("[Chef] 👨‍🍳 Challenge détecté")
            try:
                chef_meta = json.loads(base64.b64decode(challenge_meta_b64))
            except Exception as e:
                logger.error(f"[Chef] ❌ Erreur decode: {e}")
                return False, "Chef metadata decode failed"

            cont = self._continue_challenge(challenge_id, "chef", chef_meta)
            if not cont:
                return False, "Chef continue failed"

            cont_data     = cont.json()
            next_type     = cont_data.get("challengeType", "")
            next_meta_raw = cont_data.get("challengeMetadata", "")

            if next_type == "":
                logger.info("[Chef] ✅ Passé sans 2FA")
                time.sleep(DELAY_BEFORE_FINAL)
                final = self._payout_request(user_id, amount, {
                    "rblx-challenge-id":       challenge_id,
                    "rblx-challenge-type":     "chef",
                    "rblx-challenge-metadata": challenge_meta_b64
                })

            elif next_type == "twostepverification":
                logger.info("[Chef] 🔐 2FA requis")
                try:
                    tfa_meta = json.loads(next_meta_raw)
                    tfa_user = tfa_meta["userId"]
                    tfa_cid  = tfa_meta["challengeId"]
                except Exception as e:
                    return False, "2FA metadata parse failed"

                vtoken = self._verify_totp(tfa_user, tfa_cid)
                if not vtoken:
                    return False, "2FA TOTP verification failed"

                time.sleep(DELAY_AFTER_2FA)
                complete_meta = {
                    "verificationToken": vtoken,
                    "rememberDevice": False,
                    "userId": tfa_user,
                    "challengeId": tfa_cid,
                }
                cont2 = self._continue_challenge(challenge_id, "twostepverification", complete_meta)
                if not cont2 or cont2.status_code >= 400:
                    return False, "2FA continue failed"

                if cont2.json().get("challengeType") == "blocksession":
                    if retry_count < max_retries:
                        time.sleep(DELAY_BLOCKSESSION)
                        self._set_csrf()
                        return self.payout(user_id, amount, retry_count + 1, max_retries)
                    return False, "Session bloquée après max retries"

                time.sleep(DELAY_BEFORE_FINAL)
                final = self._payout_request(user_id, amount)

            elif next_type == "blocksession":
                if retry_count < max_retries:
                    time.sleep(DELAY_BLOCKSESSION)
                    self._set_csrf()
                    return self.payout(user_id, amount, retry_count + 1, max_retries)
                return False, "Session bloquée après max retries"
            else:
                return False, f"Unknown challenge: {next_type}"

            if final.status_code == 200:
                logger.info("✅ [Chef] Payout réussi!")
                return True, "ok"
            else:
                logger.error(f"❌ [Chef] Payout échoué: {final.text[:200]}")
                return False, final.text

        # ── TWOSTEPVERIFICATION DIRECT ──────────────────────────────────────
        elif challenge_type == "twostepverification":
            logger.info("[2FA] 🔐 Challenge direct")
            try:
                meta   = json.loads(base64.b64decode(challenge_meta_b64))
                sender = meta["userId"]
                cid    = meta["challengeId"]
            except Exception as e:
                return False, "2FA metadata parse failed"

            vtoken = self._verify_totp(sender, cid)
            if not vtoken:
                return False, "2FA TOTP verification failed"

            time.sleep(DELAY_AFTER_2FA)
            complete_meta = {
                "verificationToken": vtoken,
                "rememberDevice": False,
                "userId": sender,
                "challengeId": cid,
            }
            cont2 = self._continue_challenge(challenge_id, "twostepverification", complete_meta)
            if not cont2 or cont2.status_code >= 400:
                return False, "2FA continue failed"

            if cont2.json().get("challengeType") == "blocksession":
                if retry_count < max_retries:
                    time.sleep(DELAY_BLOCKSESSION)
                    self._set_csrf()
                    return self.payout(user_id, amount, retry_count + 1, max_retries)
                return False, "Session bloquée après max retries"

            time.sleep(DELAY_BEFORE_FINAL)
            final = self._payout_request(user_id, amount)

            if final.status_code == 200:
                return True, "ok"
            return False, final.text

        elif challenge_type == "blocksession":
            if retry_count < max_retries:
                time.sleep(DELAY_BLOCKSESSION)
                self._set_csrf()
                return self.payout(user_id, amount, retry_count + 1, max_retries)
            return False, "Session bloquée après max retries"
        else:
            return False, f"Unknown challenge: {challenge_type}"


# ============================================================================
# PERMISSIONS
# ============================================================================

def is_admin(user_id):
    return user_id in ADMIN_DISCORD_IDS


# ============================================================================
# ROBLOX API
# ============================================================================

def get_user_info(user_id):
    try:
        resp = requests.get(f"https://users.roblox.com/v1/users/{user_id}", timeout=3)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error(f"Erreur récupération user {user_id}: {e}")
    return None


def get_group_membership(user_id, group_id):
    try:
        resp = requests.get(f"https://groups.roblox.com/v1/users/{user_id}/groups", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            for group in data.get("data", []):
                if group["group"]["id"] == group_id:
                    return {
                        "in_group": True,
                        "join_date": group.get("joinedDate"),
                        "role": group.get("role", {}).get("name")
                    }
            return {"in_group": False}
    except Exception as e:
        logger.error(f"Erreur vérification groupe pour {user_id}: {e}")
    return {"in_group": False}


def check_eligibility(user_id, group_id, days_required=MIN_GROUP_TENURE_DAYS):
    membership = get_group_membership(user_id, group_id)
    if not membership.get("in_group"):
        return {"eligible": False, "reason": "Not in group"}
    if membership.get("join_date"):
        try:
            join_date = datetime.fromisoformat(membership["join_date"].replace("Z", "+00:00"))
            days_in_group = (datetime.now(join_date.tzinfo) - join_date).days
            if days_in_group < days_required:
                return {"eligible": False, "reason": f"Not long enough ({days_in_group}/{days_required} days)"}
        except Exception as e:
            logger.error(f"Erreur parsing date: {e}")
    return {"eligible": True, "reason": "Meets all requirements"}


def transfer_robux_opencloud(target_user_id, amount_robux):
    headers = {
        "x-api-key": ROBLOX_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {"payouts": [{"userId": str(target_user_id), "amount": amount_robux}]}
    logger.info(f"[OpenCloud] 📤 Tentative: {amount_robux} R$ → {target_user_id}")
    response = requests.post(
        f"https://apis.roblox.com/cloud/v2/groups/{GROUP_ID}/payouts",
        json=payload, headers=headers, timeout=10
    )
    roblox_proof = {
        "method": "open_cloud",
        "endpoint": f"cloud/v2/groups/{GROUP_ID}/payouts",
        "recipientId": target_user_id,
        "amountSent": amount_robux,
        "httpStatus": response.status_code,
        "response": response.json() if response.content else {},
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    if response.status_code == 200:
        logger.info(f"✅ [OpenCloud] Réussi: {amount_robux} R$ → {target_user_id}")
        return True, roblox_proof
    else:
        logger.error(f"❌ [OpenCloud] HTTP {response.status_code}")
        return False, roblox_proof


def transfer_robux_cookie_2fa(target_user_id, amount_robux):
    # Obtenir un cookie valide (réauthentifie si nécessaire)
    cookie = get_valid_cookie()
    if not cookie:
        return False, {"error": "Impossible d'obtenir un cookie valide", "timestamp": datetime.utcnow().isoformat() + "Z"}

    payout = RobloxPayout(cookie, GROUP_ID, ROBLOX_2FA_SECRET)
    success, detail = payout.payout(target_user_id, amount_robux)
    proof = {
        "method": "cookie_2fa" if ROBLOX_2FA_SECRET else "cookie_only",
        "endpoint": f"groups/{GROUP_ID}/payouts",
        "recipientId": target_user_id,
        "amountSent": amount_robux,
        "httpStatus": 200 if success else 403,
        "response": {"detail": detail},
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    return success, proof


def transfer_robux(target_user_id, amount_robux):
    if ROBLOX_API_KEY:
        success, proof = transfer_robux_opencloud(target_user_id, amount_robux)
        if success:
            return True, proof
        logger.warning("[Payout] Open Cloud échoué, fallback Cookie+2FA...")

    if ACCOUNT_COOKIE or (ROBLOX_USERNAME and ROBLOX_PASSWORD):
        return transfer_robux_cookie_2fa(target_user_id, amount_robux)

    logger.error("[Payout] Aucune méthode configurée")
    return False, {"error": "No auth method", "timestamp": datetime.utcnow().isoformat() + "Z"}


# ============================================================================
# DISCORD BOT
# ============================================================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


class DonationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_donations.start()

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"✅ Bot Discord connecté: {self.bot.user}")
        try:
            synced = await self.bot.tree.sync()
            logger.info(f"✅ {len(synced)} slash commands synchronisées")
        except Exception as e:
            logger.error(f"❌ Erreur sync: {e}")

    @tasks.loop(minutes=30)
    async def check_donations(self):
        logger.info("🔄 Vérification des donations...")
        try:
            resp = requests.get(f"{BACKEND_URL}/pending", timeout=10)
            if resp.status_code != 200:
                logger.error(f"❌ Erreur /pending: HTTP {resp.status_code}")
                return

            donations = resp.json().get("donations", [])
            logger.info(f"📋 {len(donations)} donation(s) en attente")

            for donation in donations:
                user_id     = donation['target_player_id']
                amount      = donation['final_amount']
                donation_id = donation['id']

                eligibility = check_eligibility(user_id, GROUP_ID)

                if eligibility["eligible"]:
                    success, roblox_proof = transfer_robux(user_id, amount)

                    if success:
                        requests.post(
                            f"{BACKEND_URL}/donations/{donation_id}/status",
                            json={"status": "completed", "roblox_proof": roblox_proof},
                            timeout=5
                        )
                        logger.info(f"✅ Transfert réussi donation #{donation_id}")
                        channel = self.bot.get_channel(DISCORD_CHANNEL_ID)
                        if channel:
                            await channel.send(f"✅ Donation `#{donation_id}`: `{amount} R$` transféré")
                    else:
                        requests.post(
                            f"{BACKEND_URL}/donations/{donation_id}/status",
                            json={"status": "failed", "reason": f"Payout échoué (HTTP {roblox_proof.get('httpStatus', '?')})"},
                            timeout=5
                        )
                        logger.warning(f"⚠️ Donation #{donation_id} échouée")
                else:
                    requests.post(
                        f"{BACKEND_URL}/donations/{donation_id}/status",
                        json={"status": "requeue", "reason": eligibility["reason"]},
                        timeout=5
                    )
                    logger.info(f"🔄 Donation #{donation_id} re-queued")

        except Exception as e:
            logger.error(f"❌ Erreur check_donations: {e}")

    @discord.app_commands.command(name="donations_stats", description="Voir les statistiques")
    async def donations_stats(self, interaction: discord.Interaction):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Non autorisé", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        try:
            resp = requests.get(f"{BACKEND_URL}/list", timeout=10)
            if resp.status_code != 200:
                await interaction.followup.send("❌ Erreur backend")
                return

            data      = resp.json()
            donations = data.get("donations", [])
            total     = len(donations)
            pending   = sum(1 for d in donations if d['status'] == 'pending')
            completed = sum(1 for d in donations if d['status'] == 'completed')

            embed = discord.Embed(title="📊 Statistiques", color=discord.Color.blue())
            embed.add_field(name="Total",      value=f"`{total}`",    inline=True)
            embed.add_field(name="En attente", value=f"`{pending}`",   inline=True)
            embed.add_field(name="Complétées", value=f"`{completed}`", inline=True)

            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"Erreur: {e}")
            await interaction.followup.send(f"❌ Erreur: {e}")

    @discord.app_commands.command(name="donation_status", description="Statut d'une donation")
    @discord.app_commands.describe(donation_id="ID de la donation")
    async def donation_status(self, interaction: discord.Interaction, donation_id: int):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Non autorisé", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        try:
            resp = requests.get(f"{BACKEND_URL}/status/{donation_id}", timeout=10)
            if resp.status_code == 404:
                await interaction.followup.send("❌ Donation non trouvée")
                return
            if resp.status_code != 200:
                await interaction.followup.send("❌ Erreur backend")
                return

            donation = resp.json()
            embed = discord.Embed(title=f"Donation #{donation_id}", color=discord.Color.green())
            embed.add_field(name="Montant brut", value=f"`{donation['amount']} R$`",      inline=True)
            embed.add_field(name="Montant net",  value=f"`{donation['finalAmount']} R$`", inline=True)
            embed.add_field(name="Statut",       value=f"`{donation['status']}`",         inline=True)

            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"Erreur: {e}")
            await interaction.followup.send(f"❌ Erreur: {e}")


async def setup_bot():
    await bot.add_cog(DonationCog(bot))


# ============================================================================
# STARTUP
# ============================================================================

logger.info("✅ Bot Discord en cours de démarrage...")
logger.info(f"🔑 Roblox API Key: {'✅' if ROBLOX_API_KEY else '❌'}")
logger.info(f"🍪 Cookie: {'✅' if ACCOUNT_COOKIE else '❌'}")
logger.info(f"🔐 2FA: {'✅' if ROBLOX_2FA_SECRET and PYOTP_AVAILABLE else '❌'}")
logger.info(f"🔄 Réauth: {'✅' if ROBLOX_USERNAME and ROBLOX_PASSWORD else '❌'}")


async def main():
    async with bot:
        await setup_bot()
        await bot.start(DISCORD_TOKEN)


if __name__ == '__main__':
    asyncio.run(main())
