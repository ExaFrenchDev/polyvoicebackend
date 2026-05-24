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
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

try:
    import pyotp
    PYOTP_AVAILABLE = True
except ImportError:
    PYOTP_AVAILABLE = False

load_dotenv()

app = Flask(__name__)

# Configuration
WEBHOOK_SECRET       = os.getenv("WEBHOOK_SECRET", "")
GROUP_ID             = int(os.getenv("GROUP_ID", "0"))
ACCOUNT_COOKIE       = os.getenv("ROBLOX_ACCOUNT_COOKIE", "")
ROBLOX_2FA_SECRET    = os.getenv("ROBLOX_2FA_SECRET", "")
ROBLOX_API_KEY       = os.getenv("ROBLOX_API_KEY", "")
DISCORD_WEBHOOK_URL  = os.getenv("DISCORD_WEBHOOK_URL", "")
ADMIN_PASSWORD       = os.getenv("ADMIN_PASSWORD", "exa14170.")
DATABASE_URL         = os.getenv("DATABASE_URL", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEVPRODUCT_AMOUNTS = {
    "3593525234": 1,
    "3593525497": 50,
    "3593525652": 100,
}

TAX_RATE              = 0.60
WAIT_DAYS             = 7
MIN_GROUP_TENURE_DAYS = 7


# ============================================================================
# DATABASE  (PostgreSQL / Supabase)
# ============================================================================

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS donations (
            id                SERIAL PRIMARY KEY,
            player_id         BIGINT NOT NULL,
            target_player_id  BIGINT NOT NULL,
            devproduct_id     TEXT NOT NULL,
            amount_robux      INTEGER NOT NULL,
            final_amount      INTEGER,
            status            TEXT DEFAULT 'pending',
            created_at        TIMESTAMP DEFAULT NOW(),
            processed_at      TIMESTAMP,
            retry_count       INTEGER DEFAULT 0,
            last_retry        TIMESTAMP,
            discord_message_id TEXT
        )
    ''')
    conn.commit()
    c.close()
    conn.close()
    logger.info("✅ Table donations prête (Supabase/PostgreSQL)")


# ============================================================================
# ROBLOX PAYOUT — RobloxPayout Class (CORRIGÉ)
# ============================================================================

class RobloxPayout:
    """
    Gère les transferts Robux avec support automatique :
    - Chef challenge
    - 2FA TOTP (génération auto depuis secret)
    - Fallback retry avec différents headers
    """
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
        try:
            # Méthode 1: POST logout pour récupérer le token
            r = requests.post("https://auth.roblox.com/v2/logout", headers=self.headers, timeout=5)
            token = r.headers.get('X-CSRF-TOKEN') or r.headers.get('x-csrf-token')
            
            # Méthode 2: GET sur groups si logout ne donne pas de token
            if not token:
                r2 = requests.get(f"https://groups.roblox.com/v1/groups/{self.group_id}", 
                                 headers=self.headers, timeout=5)
                token = r2.headers.get('X-CSRF-TOKEN') or r2.headers.get('x-csrf-token')
            
            if token:
                self.headers['X-CSRF-TOKEN'] = token
                logger.info(f"[CSRF] Token récupéré: {token[:20]}...")
                return True
            else:
                logger.warning("[CSRF] Pas de token trouvé, on continue quand même...")
                return True
        except Exception as e:
            logger.error(f"[Cookie] Erreur CSRF: {e}")
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
            logger.error("[2FA] Pas de TOTP disponible")
            return None
        try:
            logger.info(f"[2FA] Envoi verify: sender_id={sender_id}, challenge_id={challenge_id}, code={code}")
            r = requests.post(
                f"https://twostepverification.roblox.com/v1/users/{sender_id}/challenges/authenticator/verify",
                headers=self.headers,
                json={"actionType": "Generic", "challengeId": challenge_id, "code": code},
                timeout=10
            )
            logger.info(f"[2FA] Response status: {r.status_code}")
            
            if r.status_code != 200:
                logger.error(f"[2FA] HTTP {r.status_code}: {r.text[:200]}")
                return None
            
            data = r.json()
            if "errors" in data:
                logger.error(f"[2FA] Erreur verify: {data['errors'][0].get('message', 'unknown')}")
                return None
            
            vtoken = data.get("verificationToken")
            logger.info(f"[2FA] ✅ Verification token reçu: {vtoken[:20] if vtoken else 'None'}...")
            return vtoken
        except Exception as e:
            logger.error(f"[2FA] Erreur requête verify: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def _continue_challenge(self, challenge_id, challenge_type, metadata):
        try:
            resp = requests.post(
                "https://apis.roblox.com/challenge/v1/continue",
                headers=self.headers,
                json={"challengeId": challenge_id, "challengeType": challenge_type, "challengeMetadata": json.dumps(metadata)},
                timeout=10
            )
            logger.info(f"[Challenge] Continue {challenge_type}: HTTP {resp.status_code}")
            if resp.status_code >= 400:
                logger.warning(f"[Challenge] Response: {resp.text[:200]}")
            return resp
        except Exception as e:
            logger.error(f"[Challenge] Erreur continue: {e}")
            return None

    def payout(self, user_id, amount):
        """Effectue un payout avec gestion automatique des challenges."""
        if not self._set_csrf():
            return False, "Cookie invalide ou expiré"

        logger.info(f"[Cookie] Tentative payout: {amount} R$ → {user_id}")
        r = self._payout_request(user_id, amount)

        if r.status_code == 200:
            logger.info(f"✅ [Cookie] Payout réussi sans challenge")
            return True, "ok"

        challenge_type = r.headers.get("rblx-challenge-type", "").lower()
        challenge_id = r.headers.get("rblx-challenge-id", "")
        challenge_meta_b64 = r.headers.get("rblx-challenge-metadata", "")

        if not challenge_type or not challenge_id:
            logger.error(f"[Cookie] Pas de challenge headers — HTTP {r.status_code}: {r.text}")
            return False, f"No challenge headers (HTTP {r.status_code})"

        # ── CHEF CHALLENGE ──────────────────────────────────────────────────
        if challenge_type == "chef":
            logger.info("[Chef] Chef challenge détecté")
            try:
                chef_meta = json.loads(base64.b64decode(challenge_meta_b64))
            except Exception as e:
                logger.error(f"[Chef] Erreur decode metadata: {e}")
                return False, "Chef metadata decode failed"

            cont = self._continue_challenge(challenge_id, "chef", chef_meta)
            if not cont:
                return False, "Chef continue failed"
            
            cont_data = cont.json()
            next_type = cont_data.get("challengeType", "")
            next_meta_raw = cont_data.get("challengeMetadata", "")

            if next_type == "":
                # Chef passé, retry sans 2FA
                logger.info("[Chef] Passé sans 2FA, retry payout...")
                final = self._payout_request(user_id, amount, {
                    "rblx-challenge-id": challenge_id,
                    "rblx-challenge-type": "twostepverification",
                    "rblx-challenge-metadata": challenge_meta_b64
                })

            elif next_type == "twostepverification":
                # ✅ CORRECTION: Chef → 2FA
                logger.info("[Chef] Unlock 2FA required")
                try:
                    tfa_meta = json.loads(next_meta_raw)
                    tfa_user = tfa_meta["userId"]
                    tfa_cid = tfa_meta["challengeId"]
                except Exception as e:
                    logger.error(f"[2FA] Erreur parse metadata: {e}")
                    return False, "2FA metadata parse failed"

                vtoken = self._verify_totp(tfa_user, tfa_cid)
                if not vtoken:
                    return False, "2FA TOTP verification failed"

                # ✅ Construire la preuve 2FA correctement
                tfa_proof_data = {
                    "rememberDevice": False,
                    "actionType": "Generic",
                    "verificationToken": vtoken,
                    "challengeId": tfa_cid
                }
                
                # ✅ Continuer le challenge avec la preuve
                cont_resp = self._continue_challenge(challenge_id, "twostepverification", tfa_proof_data)
                if not cont_resp or cont_resp.status_code != 200:
                    logger.warning("[Chef+2FA] Continue challenge failed, proceeding anyway...")
                
                # ✅ Encoder la preuve correctement (une seule fois)
                tfa_proof_b64 = base64.b64encode(
                    json.dumps(tfa_proof_data).encode()
                ).decode()

                logger.info("[Chef+2FA] Retry payout avec preuve vérifiée...")
                final = self._payout_request(user_id, amount, {
                    "rblx-challenge-id": challenge_id,
                    "rblx-challenge-type": "twostepverification",
                    "rblx-challenge-metadata": tfa_proof_b64
                })

                # ✅ Fallback intelligent: si twostep échoue, essayer sans headers
                if final.status_code != 200:
                    logger.warning("[Chef+2FA] Twostep échoué (HTTP %d), fallback sans headers...", final.status_code)
                    final = self._payout_request(user_id, amount)

            elif next_type == "blocksession":
                logger.error("[Chef] Session bloquée (AutomatedTampering)")
                return False, "Session bloquée — réessayez dans 1 minute"
            else:
                logger.error(f"[Chef] Challenge inconnu: {next_type}")
                return False, f"Unknown challenge: {next_type}"

            if final.status_code == 200:
                logger.info("✅ [Chef] Payout réussi après chef!")
                return True, "ok"
            else:
                logger.error(f"❌ [Chef] Payout échoué après chef: {final.text}")
                return False, final.text

        # ── TWOSTEPVERIFICATION DIRECT ──────────────────────────────────────
        elif challenge_type == "twostepverification":
            logger.info("[2FA] 2FA directe (pas chef)")
            try:
                meta = json.loads(base64.b64decode(challenge_meta_b64))
                sender = meta["userId"]
                cid = meta["challengeId"]
            except Exception as e:
                logger.error(f"[2FA] Erreur parse metadata: {e}")
                return False, "2FA metadata parse failed"

            vtoken = self._verify_totp(sender, cid)
            if not vtoken:
                return False, "2FA TOTP verification failed"

            tfa_proof = {
                "rememberDevice": False,
                "actionType": "Generic",
                "verificationToken": vtoken,
                "challengeId": cid
            }
            self._continue_challenge(challenge_id, "twostepverification", tfa_proof)

            final = self._payout_request(user_id, amount, {
                'rblx-challenge-id': challenge_id,
                'rblx-challenge-metadata': base64.b64encode(json.dumps(tfa_proof).encode()).decode(),
                'rblx-challenge-type': "twostepverification"
            })
            if final.status_code == 200:
                logger.info("✅ [2FA] Payout réussi après 2FA!")
                return True, "ok"
            else:
                logger.error(f"❌ [2FA] Payout échoué après 2FA: {final.text}")
                return False, final.text

        elif challenge_type == "blocksession":
            logger.error("[Cookie] Session bloquée sur première requête")
            return False, "Session bloquée — réessayez dans 1 minute"
        else:
            logger.error(f"[Cookie] Challenge inconnu: {challenge_type}")
            return False, f"Unknown challenge: {challenge_type}"


# ============================================================================
# ROBLOX API
# ============================================================================

def verify_webhook_signature(data, signature):
    if not WEBHOOK_SECRET:
        return True
    expected_sig = hmac.new(WEBHOOK_SECRET.encode(), data, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected_sig)


def get_user_info(user_id):
    try:
        resp = requests.get(f"https://users.roblox.com/v1/users/{user_id}", timeout=3)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error(f"Erreur user {user_id}: {e}")
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
        logger.error(f"Erreur groupe {user_id}: {e}")
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
    """
    Transfert via Roblox Open Cloud API (clé API) — pas de challenge.
    """
    headers = {
        "x-api-key": ROBLOX_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "payouts": [{"userId": str(target_user_id), "amount": amount_robux}]
    }
    logger.info(f"[OpenCloud] Tentative: {amount_robux} R$ → user {target_user_id}")
    try:
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
            logger.error(f"❌ [OpenCloud] HTTP {response.status_code}: {response.text}")
            return False, roblox_proof
    except Exception as e:
        logger.error(f"❌ [OpenCloud] Exception: {e}")
        return False, {"error": str(e), "timestamp": datetime.utcnow().isoformat() + "Z"}


def transfer_robux_cookie_2fa(target_user_id, amount_robux):
    """
    Transfert via cookie + 2FA TOTP automatique.
    """
    payout = RobloxPayout(ACCOUNT_COOKIE, GROUP_ID, ROBLOX_2FA_SECRET)
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
    """
    1. Essaie Open Cloud API
    2. Fallback Cookie + 2FA automatique
    """
    if ROBLOX_API_KEY:
        success, proof = transfer_robux_opencloud(target_user_id, amount_robux)
        if success:
            return True, proof
        logger.warning("[Payout] Open Cloud échoué, fallback Cookie+2FA...")

    if ACCOUNT_COOKIE:
        return transfer_robux_cookie_2fa(target_user_id, amount_robux)

    logger.error("[Payout] Aucune méthode configurée (ni API key ni cookie)")
    return False, {"error": "No auth method configured", "timestamp": datetime.utcnow().isoformat() + "Z"}


# ============================================================================
# DISCORD
# ============================================================================

def send_discord_notification(donor_id, donor_name, target_id, target_name,
                               amount, final_amount, taxes, donation_id, estimated_date):
    if not DISCORD_WEBHOOK_URL:
        return None
    embed = {
        "title": "💰 Nouvelle Donation en attente",
        "color": 0xFFD700,
        "fields": [
            {"name": "👤 Donateur",          "value": f"`{donor_name}` (ID: `{donor_id}`)",  "inline": False},
            {"name": "🎯 Receveur",           "value": f"`{target_name}` (ID: `{target_id}`)", "inline": False},
            {"name": "💵 Montant brut",       "value": f"`{amount} Robux`",                   "inline": True},
            {"name": f"🏦 Taxe (-{int(TAX_RATE*100)}%)", "value": f"`{taxes} Robux`",         "inline": True},
            {"name": "✨ Montant net",         "value": f"`{final_amount} Robux`",             "inline": True},
            {"name": "📅 Transfert prévu le", "value": f"`{estimated_date}`",                 "inline": False},
            {"name": "🔖 ID Donation",        "value": f"`#{donation_id}`",                   "inline": True},
            {"name": "⏳ Statut",              "value": "`En attente (7 jours)`",              "inline": True},
        ],
        "footer": {"text": "PolyVoice Donation System"},
        "timestamp": datetime.utcnow().isoformat()
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL + "?wait=true", json={"embeds": [embed]}, timeout=5)
        if resp.status_code in (200, 204):
            return resp.json().get("id")
    except Exception as e:
        logger.error(f"❌ Discord: {e}")
    return None


def edit_discord_success(message_id, donor_name, donor_id, target_name, target_id,
                          amount, final_amount, taxes, donation_id, roblox_proof):
    if not DISCORD_WEBHOOK_URL or not message_id:
        return
    proof_str = json.dumps(roblox_proof, indent=2, ensure_ascii=False)
    if len(proof_str) > 950:
        proof_str = proof_str[:950] + "\n... (tronqué)"
    embed = {
        "title": "✅ Donation transférée avec succès!",
        "color": 0x00FF7F,
        "fields": [
            {"name": "👤 Donateur",   "value": f"`{donor_name}` (ID: `{donor_id}`)",   "inline": False},
            {"name": "🎯 Receveur",   "value": f"`{target_name}` (ID: `{target_id}`)", "inline": False},
            {"name": "💵 Montant brut","value": f"`{amount} Robux`",                    "inline": True},
            {"name": f"🏦 Taxe (-{int(TAX_RATE*100)}%)", "value": f"`{taxes} Robux`",  "inline": True},
            {"name": "✨ Montant reçu","value": f"`{final_amount} Robux`",              "inline": True},
            {"name": "🔖 ID Donation","value": f"`#{donation_id}`",                    "inline": True},
            {"name": "✅ Statut",     "value": "`Transféré`",                          "inline": True},
            {"name": "📄 Preuve Roblox", "value": f"```json\n{proof_str}\n```",        "inline": False},
        ],
        "footer": {"text": "PolyVoice Donation System"},
        "timestamp": datetime.utcnow().isoformat()
    }
    try:
        requests.patch(f"{DISCORD_WEBHOOK_URL}/messages/{message_id}", json={"embeds": [embed]}, timeout=5)
    except Exception as e:
        logger.error(f"❌ Discord edit: {e}")


def edit_discord_failed(message_id, donor_name, donor_id, target_name, target_id,
                         amount, final_amount, donation_id, reason):
    if not DISCORD_WEBHOOK_URL or not message_id:
        return
    embed = {
        "title": "❌ Échec du transfert",
        "color": 0xFF4444,
        "fields": [
            {"name": "👤 Donateur",   "value": f"`{donor_name}` (ID: `{donor_id}`)",   "inline": False},
            {"name": "🎯 Receveur",   "value": f"`{target_name}` (ID: `{target_id}`)", "inline": False},
            {"name": "💵 Brut",       "value": f"`{amount} Robux`",                    "inline": True},
            {"name": "✨ Net",        "value": f"`{final_amount} Robux`",              "inline": True},
            {"name": "🔖 ID",        "value": f"`#{donation_id}`",                    "inline": True},
            {"name": "❌ Statut",    "value": "`Échec — sera retenté`",               "inline": True},
            {"name": "⚠️ Raison",   "value": f"`{reason}`",                           "inline": False},
        ],
        "footer": {"text": "PolyVoice Donation System"},
        "timestamp": datetime.utcnow().isoformat()
    }
    try:
        requests.patch(f"{DISCORD_WEBHOOK_URL}/messages/{message_id}", json={"embeds": [embed]}, timeout=5)
    except Exception as e:
        logger.error(f"❌ Discord edit: {e}")


# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/webhook/devproduct', methods=['POST'])
def handle_devproduct_purchase():
    try:
        signature = request.headers.get('X-Roblox-Signature')
        if signature and not verify_webhook_signature(request.data, signature):
            return jsonify({"error": "Invalid signature"}), 401

        data = request.get_json()
        user_id       = data.get("userId")
        target_user_id = data.get("targetUserId")
        devproduct_id  = str(data.get("devProductId"))

        if not all([user_id, target_user_id, devproduct_id]):
            return jsonify({"error": "Missing fields"}), 400
        if devproduct_id not in DEVPRODUCT_AMOUNTS:
            return jsonify({"error": "Unknown DevProduct"}), 400

        amount       = DEVPRODUCT_AMOUNTS[devproduct_id]
        final_amount = math.ceil(amount * (1 - TAX_RATE))
        taxes        = amount - final_amount
        estimated_date = (datetime.now() + timedelta(days=WAIT_DAYS)).strftime("%d/%m/%Y")

        donor_info  = get_user_info(user_id)
        target_info = get_user_info(target_user_id)
        donor_name  = donor_info.get("name", str(user_id))  if donor_info  else str(user_id)
        target_name = target_info.get("name", str(target_user_id)) if target_info else str(target_user_id)

        conn = get_db()
        c = conn.cursor()
        c.execute('''
            INSERT INTO donations
            (player_id, target_player_id, devproduct_id, amount_robux, final_amount, status)
            VALUES (%s, %s, %s, %s, %s, 'pending') RETURNING id
        ''', (user_id, target_user_id, devproduct_id, amount, final_amount))
        donation_id = c.fetchone()["id"]

        message_id = send_discord_notification(
            user_id, donor_name, target_user_id, target_name,
            amount, final_amount, taxes, donation_id, estimated_date
        )
        if message_id:
            c.execute('UPDATE donations SET discord_message_id = %s WHERE id = %s', (message_id, donation_id))

        conn.commit()
        c.close()
        conn.close()
        logger.info(f"✅ Donation {donation_id}: {user_id} → {target_user_id} ({amount} R$)")

        return jsonify({
            "success": True,
            "donationId": donation_id,
            "message": f"Donation enregistrée: {final_amount} Robux dans {WAIT_DAYS} jours",
            "estimatedDate": (datetime.now() + timedelta(days=WAIT_DAYS)).isoformat()
        }), 200

    except Exception as e:
        logger.error(f"Erreur webhook: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/pending', methods=['GET'])
def get_pending_donations():
    try:
        conn = get_db()
        c = conn.cursor()
        cutoff = datetime.now() - timedelta(days=WAIT_DAYS)
        c.execute('''
            SELECT * FROM donations
            WHERE status = 'pending' AND created_at <= %s AND retry_count < 5
            ORDER BY created_at ASC LIMIT 5
        ''', (cutoff,))
        donations = [dict(row) for row in c.fetchall()]
        c.close(); conn.close()
        return jsonify({"success": True, "donations": donations}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/donations/<int:donation_id>/status', methods=['POST'])
def update_donation_status(donation_id):
    try:
        data       = request.get_json()
        new_status = data.get("status")
        roblox_proof = data.get("roblox_proof", None)
        fail_reason  = data.get("reason", "Erreur inconnue")

        if new_status not in ("completed", "pending", "requeue", "failed"):
            return jsonify({"error": "Invalid status"}), 400

        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM donations WHERE id = %s', (donation_id,))
        row = c.fetchone()
        if not row:
            c.close(); conn.close()
            return jsonify({"error": "Donation not found"}), 404
        donation = dict(row)

        if new_status == "completed":
            c.execute('''
                UPDATE donations SET status = 'completed', processed_at = NOW() WHERE id = %s
            ''', (donation_id,))
        else:
            c.execute('''
                UPDATE donations SET status = %s, retry_count = retry_count + 1, last_retry = NOW()
                WHERE id = %s
            ''', (new_status, donation_id))
        conn.commit()
        c.close(); conn.close()

        donor_info  = get_user_info(donation['player_id'])
        target_info = get_user_info(donation['target_player_id'])
        donor_name  = donor_info.get("name",  str(donation['player_id']))        if donor_info  else str(donation['player_id'])
        target_name = target_info.get("name", str(donation['target_player_id'])) if target_info else str(donation['target_player_id'])
        taxes      = donation['amount_robux'] - donation['final_amount']
        message_id = donation.get('discord_message_id')

        if new_status == "completed" and roblox_proof:
            edit_discord_success(message_id, donor_name, donation['player_id'],
                                  target_name, donation['target_player_id'],
                                  donation['amount_robux'], donation['final_amount'],
                                  taxes, donation_id, roblox_proof)
        elif new_status in ("failed", "requeue"):
            edit_discord_failed(message_id, donor_name, donation['player_id'],
                                 target_name, donation['target_player_id'],
                                 donation['amount_robux'], donation['final_amount'],
                                 donation_id, fail_reason)
        return jsonify({"success": True}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/status/<int:donation_id>', methods=['GET'])
def get_donation_status(donation_id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM donations WHERE id = %s', (donation_id,))
        d = c.fetchone()
        c.close(); conn.close()
        if not d:
            return jsonify({"error": "Donation not found"}), 404
        return jsonify({
            "id": d['id'], "status": d['status'],
            "amount": d['amount_robux'], "finalAmount": d['final_amount'],
            "createdAt": str(d['created_at']), "processedAt": str(d['processed_at']),
            "targetUserId": d['target_player_id']
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/list', methods=['GET'])
def list_donations():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM donations ORDER BY created_at DESC LIMIT 50')
        rows = c.fetchall()
        c.close(); conn.close()
        return jsonify({
            "success": True, "count": len(rows),
            "donations": [dict(r) for r in rows]
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()}), 200


@app.route('/', methods=['GET'])
def index():
    return jsonify({"service": "Roblox Donation Backend", "status": "running"}), 200


# ============================================================================
# ADMIN DASHBOARD
# ============================================================================

@app.route('/admin', methods=['GET'])
def dashboard():
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return "Access denied", 403

    html = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PolyVoice — Admin</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0a0f;
    --surface: #111118;
    --border: #1e1e2e;
    --border-hi: #2a2a3e;
    --text: #e2e2f0;
    --muted: #6b6b8a;
    --accent: #7c6af7;
    --accent-glow: rgba(124,106,247,0.15);
    --green: #22d3a0;
    --green-bg: rgba(34,211,160,0.08);
    --yellow: #f5c842;
    --yellow-bg: rgba(245,200,66,0.08);
    --red: #f05a5a;
    --red-bg: rgba(240,90,90,0.08);
    --blue: #60a5fa;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--text); font-family:'IBM Plex Sans',sans-serif; font-size:14px; min-height:100vh; }
  body::before {
    content:''; position:fixed; inset:0;
    background-image: linear-gradient(var(--border) 1px,transparent 1px), linear-gradient(90deg,var(--border) 1px,transparent 1px);
    background-size:40px 40px; opacity:0.4; pointer-events:none; z-index:0;
  }
  .wrap { position:relative; z-index:1; max-width:1300px; margin:0 auto; padding:28px 24px; }

  .topbar { display:flex; align-items:center; justify-content:space-between; margin-bottom:32px; padding-bottom:20px; border-bottom:1px solid var(--border); }
  .logo { display:flex; align-items:center; gap:12px; }
  .logo-dot { width:10px; height:10px; background:var(--accent); border-radius:50%; box-shadow:0 0 10px var(--accent); animation:pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
  .logo-title { font-family:'IBM Plex Mono',monospace; font-size:16px; font-weight:600; letter-spacing:0.05em; }
  .logo-sub { font-family:'IBM Plex Mono',monospace; font-size:10px; color:var(--muted); letter-spacing:0.1em; text-transform:uppercase; }
  .topbar-right { display:flex; align-items:center; gap:20px; }
  .live-badge { display:flex; align-items:center; gap:6px; background:var(--green-bg); border:1px solid rgba(34,211,160,0.2); padding:5px 12px; border-radius:20px; font-size:11px; color:var(--green); font-family:'IBM Plex Mono',monospace; }
  .live-dot { width:6px; height:6px; background:var(--green); border-radius:50%; animation:pulse 1.5s infinite; }
  .clock { font-family:'IBM Plex Mono',monospace; font-size:13px; color:var(--muted); }

  .db-badge { display:flex; align-items:center; gap:10px; background:rgba(124,106,247,0.06); border:1px solid rgba(124,106,247,0.2); border-radius:8px; padding:10px 16px; margin-bottom:24px; font-size:12px; font-family:'IBM Plex Mono',monospace; color:var(--muted); }
  .db-badge strong { color:var(--accent); }

  .stats-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:28px; }
  .stat-card { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:20px; position:relative; overflow:hidden; }
  .stat-card::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; }
  .s-total::before    { background:linear-gradient(90deg,var(--accent),transparent); }
  .s-pending::before  { background:linear-gradient(90deg,var(--yellow),transparent); }
  .s-completed::before{ background:linear-gradient(90deg,var(--green),transparent); }
  .s-failed::before   { background:linear-gradient(90deg,var(--red),transparent); }
  .stat-label { font-size:10px; text-transform:uppercase; letter-spacing:0.12em; color:var(--muted); margin-bottom:12px; font-family:'IBM Plex Mono',monospace; }
  .stat-value { font-size:38px; font-weight:600; font-family:'IBM Plex Mono',monospace; line-height:1; }
  .s-total .stat-value    { color:var(--accent); }
  .s-pending .stat-value  { color:var(--yellow); }
  .s-completed .stat-value{ color:var(--green); }
  .s-failed .stat-value   { color:var(--red); }

  .toolbar { display:flex; align-items:center; justify-content:space-between; margin-bottom:16px; }
  .toolbar-left { display:flex; align-items:center; gap:12px; }
  .section-title { font-family:'IBM Plex Mono',monospace; font-size:13px; font-weight:500; letter-spacing:0.05em; }
  .count-badge { background:var(--accent-glow); border:1px solid rgba(124,106,247,0.3); color:var(--accent); font-size:11px; font-family:'IBM Plex Mono',monospace; padding:2px 8px; border-radius:4px; }
  .btn-group { display:flex; gap:8px; }
  .btn { display:inline-flex; align-items:center; gap:6px; padding:7px 14px; border-radius:6px; font-size:12px; font-weight:500; font-family:'IBM Plex Sans',sans-serif; cursor:pointer; border:1px solid transparent; transition:all 0.15s; }
  .btn-ghost { background:transparent; border-color:var(--border-hi); color:var(--muted); }
  .btn-ghost:hover { border-color:var(--text); color:var(--text); }
  .btn-green { background:var(--green-bg); border-color:rgba(34,211,160,0.3); color:var(--green); }
  .btn-green:hover { background:rgba(34,211,160,0.15); }
  .btn-red { background:var(--red-bg); border-color:rgba(240,90,90,0.3); color:var(--red); }
  .btn-red:hover { background:rgba(240,90,90,0.15); }

  .table-wrap { background:var(--surface); border:1px solid var(--border); border-radius:10px; overflow:hidden; }
  table { width:100%; border-collapse:collapse; }
  thead tr { border-bottom:1px solid var(--border); background:rgba(255,255,255,0.02); }
  th { padding:11px 16px; text-align:left; font-size:10px; text-transform:uppercase; letter-spacing:0.1em; color:var(--muted); font-family:'IBM Plex Mono',monospace; }
  tbody tr { border-bottom:1px solid var(--border); transition:background 0.1s; }
  tbody tr:last-child { border-bottom:none; }
  tbody tr:hover { background:rgba(255,255,255,0.025); }
  td { padding:13px 16px; }
  .id-cell { font-family:'IBM Plex Mono',monospace; font-size:12px; color:var(--muted); }
  .name-cell { font-weight:500; }
  .amount-cell { font-family:'IBM Plex Mono',monospace; font-size:12px; }
  .amount-gross { color:var(--muted); }
  .amount-net { color:var(--green); font-weight:500; }
  .date-cell { font-family:'IBM Plex Mono',monospace; font-size:11px; color:var(--muted); }

  .badge { display:inline-flex; align-items:center; gap:5px; padding:3px 10px; border-radius:4px; font-size:11px; font-weight:500; font-family:'IBM Plex Mono',monospace; }
  .badge::before { content:''; width:5px; height:5px; border-radius:50%; }
  .badge-pending   { background:var(--yellow-bg); color:var(--yellow); border:1px solid rgba(245,200,66,0.2); }
  .badge-pending::before { background:var(--yellow); }
  .badge-completed { background:var(--green-bg); color:var(--green); border:1px solid rgba(34,211,160,0.2); }
  .badge-completed::before { background:var(--green); }
  .badge-failed    { background:var(--red-bg); color:var(--red); border:1px solid rgba(240,90,90,0.2); }
  .badge-failed::before { background:var(--red); }
  .badge-requeue   { background:rgba(96,165,250,0.08); color:var(--blue); border:1px solid rgba(96,165,250,0.2); }
  .badge-requeue::before { background:var(--blue); }

  .row-actions { display:flex; gap:6px; }
  .row-btn { padding:4px 10px; border-radius:4px; font-size:11px; font-family:'IBM Plex Mono',monospace; cursor:pointer; border:1px solid transparent; transition:all 0.12s; font-weight:500; }
  .row-btn-ok  { background:var(--green-bg); border-color:rgba(34,211,160,0.25); color:var(--green); }
  .row-btn-ok:hover  { background:rgba(34,211,160,0.18); }
  .row-btn-del { background:var(--red-bg); border-color:rgba(240,90,90,0.25); color:var(--red); }
  .row-btn-del:hover { background:rgba(240,90,90,0.18); }

  .empty-state { text-align:center; padding:60px 20px; color:var(--muted); font-family:'IBM Plex Mono',monospace; font-size:13px; }
  .spinner { display:inline-block; width:18px; height:18px; border:2px solid var(--border-hi); border-top-color:var(--accent); border-radius:50%; animation:spin 0.7s linear infinite; vertical-align:middle; margin-right:8px; }
  @keyframes spin { to { transform:rotate(360deg); } }

  .toast { position:fixed; bottom:24px; right:24px; padding:12px 20px; border-radius:8px; font-size:13px; font-family:'IBM Plex Mono',monospace; font-weight:500; pointer-events:none; z-index:9999; opacity:0; transform:translateY(8px); transition:all 0.2s; }
  .toast.show { opacity:1; transform:translateY(0); }
  .toast-success { background:var(--green-bg); border:1px solid rgba(34,211,160,0.3); color:var(--green); }
  .toast-error   { background:var(--red-bg); border:1px solid rgba(240,90,90,0.3); color:var(--red); }

  @media(max-width:900px){ .stats-grid{grid-template-columns:repeat(2,1fr);} }
  @media(max-width:600px){ .stats-grid{grid-template-columns:1fr 1fr;} .topbar{flex-direction:column;gap:12px;} }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="logo">
      <div class="logo-dot"></div>
      <div>
        <div class="logo-title">POLYVOICE</div>
        <div class="logo-sub">Donation Admin</div>
      </div>
    </div>
    <div class="topbar-right">
      <div class="live-badge"><span class="live-dot"></span>LIVE</div>
      <div class="clock" id="clock">--:--:--</div>
    </div>
  </div>

  <div class="db-badge">
    <span>🗄</span>
    <span>Base de données : <strong>Supabase / PostgreSQL</strong> &nbsp;|&nbsp; <span id="dbUrl">chargement...</span></span>
  </div>

  <div class="stats-grid">
    <div class="stat-card s-total">    <div class="stat-label">Total</div>    <div class="stat-value" id="s-total">—</div></div>
    <div class="stat-card s-pending">  <div class="stat-label">En attente</div><div class="stat-value" id="s-pending">—</div></div>
    <div class="stat-card s-completed"><div class="stat-label">Complétées</div><div class="stat-value" id="s-completed">—</div></div>
    <div class="stat-card s-failed">   <div class="stat-label">Échouées</div>  <div class="stat-value" id="s-failed">—</div></div>
  </div>

  <div class="toolbar">
    <div class="toolbar-left">
      <span class="section-title">DONATIONS</span>
      <span class="count-badge" id="countBadge">0</span>
    </div>
    <div class="btn-group">
      <button class="btn btn-ghost"  id="refreshBtn">      ↻ Refresh</button>
      <button class="btn btn-green"  id="completeAllBtn">  ✓ Tout compléter</button>
      <button class="btn btn-red"    id="deletePendingBtn">✕ Suppr. pending</button>
    </div>
  </div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>#ID</th><th>Donateur</th><th>Receveur</th>
          <th>Brut</th><th>Net</th><th>Statut</th><th>Date</th><th>Actions</th>
        </tr>
      </thead>
      <tbody id="tableBody">
        <tr><td colspan="8" class="empty-state"><span class="spinner"></span>Chargement...</td></tr>
      </tbody>
    </table>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
(function() {
  var BASE = window.location.origin;
  var PWD = (function() {
    var pairs = window.location.search.substring(1).split('&');
    for (var i = 0; i < pairs.length; i++) {
      var kv = pairs[i].split('=');
      if (kv[0] === 'password') return decodeURIComponent(kv[1] || '');
    }
    return '';
  })();

  function tickClock() {
    var n = new Date();
    var h = n.getHours().toString().padStart(2,'0');
    var m = n.getMinutes().toString().padStart(2,'0');
    var s = n.getSeconds().toString().padStart(2,'0');
    document.getElementById('clock').textContent = h+':'+m+':'+s;
  }
  setInterval(tickClock, 1000); tickClock();

  var toastTimer = null;
  function showToast(msg, isErr) {
    var el = document.getElementById('toast');
    el.textContent = msg;
    el.className = 'toast show ' + (isErr ? 'toast-error' : 'toast-success');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function() { el.className = 'toast'; }, 3000);
  }

  function badgeClass(st) {
    if (st === 'pending')   return 'badge-pending';
    if (st === 'completed') return 'badge-completed';
    if (st === 'failed')    return 'badge-failed';
    if (st === 'requeue')   return 'badge-requeue';
    return 'badge-pending';
  }

  function fmtDate(str) {
    if (!str) return '—';
    var d = new Date(str);
    if (isNaN(d.getTime())) return str;
    return d.getDate().toString().padStart(2,'0') + '/'
      + (d.getMonth()+1).toString().padStart(2,'0') + '/'
      + d.getFullYear() + ' '
      + d.getHours().toString().padStart(2,'0') + ':'
      + d.getMinutes().toString().padStart(2,'0');
  }

  function loadStats() {
    fetch(BASE + '/admin/stats?password=' + PWD)
      .then(function(r) { return r.json(); })
      .then(function(d) {
        document.getElementById('s-total').textContent     = d.total     !== undefined ? d.total     : '?';
        document.getElementById('s-pending').textContent   = d.pending   !== undefined ? d.pending   : '?';
        document.getElementById('s-completed').textContent = d.completed !== undefined ? d.completed : '?';
        document.getElementById('s-failed').textContent    = d.failed    !== undefined ? d.failed    : '?';
        if (d.db_info) document.getElementById('dbUrl').textContent = d.db_info;
      })
      .catch(function(e) { console.error('Stats:', e); });
  }

  function loadTable() {
    fetch(BASE + '/admin/donations?password=' + PWD)
      .then(function(r) { return r.json(); })
      .then(function(d) {
        var tbody = document.getElementById('tableBody');
        var list  = d.donations || [];
        document.getElementById('countBadge').textContent = list.length;
        if (list.length === 0) {
          tbody.innerHTML = '<tr><td colspan="8" class="empty-state">/ / Aucune donation trouvée / /</td></tr>';
          return;
        }
        var rows = '';
        for (var i = 0; i < list.length; i++) {
          var item    = list[i];
          var donor   = item.donor_name  || String(item.player_id);
          var receiver= item.target_name || String(item.target_player_id);
          var st      = item.status || 'pending';
          var id      = item.id;
          rows += '<tr>';
          rows += '<td class="id-cell">#' + id + '</td>';
          rows += '<td class="name-cell">' + donor + '</td>';
          rows += '<td class="name-cell">' + receiver + '</td>';
          rows += '<td class="amount-cell amount-gross">' + item.amount_robux + ' R$</td>';
          rows += '<td class="amount-cell amount-net">'   + item.final_amount + ' R$</td>';
          rows += '<td><span class="badge ' + badgeClass(st) + '">' + st + '</span></td>';
          rows += '<td class="date-cell">' + fmtDate(item.created_at) + '</td>';
          rows += '<td><div class="row-actions">';
          rows += '<button class="row-btn row-btn-ok"  data-id="' + id + '" onclick="markOK(this)">✓ OK</button>';
          rows += '<button class="row-btn row-btn-del" data-id="' + id + '" onclick="delRow(this)">✕ DEL</button>';
          rows += '</div></td></tr>';
        }
        tbody.innerHTML = rows;
      })
      .catch(function(e) {
        console.error('Table:', e);
        document.getElementById('tableBody').innerHTML =
          '<tr><td colspan="8" class="empty-state">Erreur de chargement</td></tr>';
      });
  }

  window.markOK = function(btn) {
    var id = btn.getAttribute('data-id');
    if (!confirm('Effectuer le vrai transfert Roblox pour la donation #' + id + ' ?')) return;
    btn.disabled = true;
    btn.textContent = '...';
    fetch(BASE + '/admin/donations/' + id + '/status?password=' + PWD, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{"status":"completed","force_transfer":true}'
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.transferred) {
        showToast('Transfert #' + id + ' effectue !');
      } else if (d.success) {
        showToast('Statut mis a jour (pas de transfert)');
      } else {
        showToast('Echec transfert #' + id + ' — voir logs', true);
      }
      loadTable(); loadStats();
    })
    .catch(function() { showToast('Erreur reseau', true); btn.disabled = false; btn.textContent = 'OK'; });
  };

  window.delRow = function(btn) {
    var id = btn.getAttribute('data-id');
    if (!confirm('Supprimer la donation #' + id + ' ?')) return;
    fetch(BASE + '/admin/donations/' + id + '?password=' + PWD, {method:'DELETE'})
      .then(function() { showToast('Supprimée #' + id); loadTable(); loadStats(); })
      .catch(function() { showToast('Erreur', true); });
  };

  document.getElementById('refreshBtn').onclick = function() {
    loadStats(); loadTable(); showToast('Données rechargées');
  };
  document.getElementById('completeAllBtn').onclick = function() {
    if (!confirm('Marquer TOUTES les pending comme completed ?')) return;
    fetch(BASE + '/admin/mark-completed?password=' + PWD, {method:'POST'})
      .then(function(r) { return r.json(); })
      .then(function(d) { showToast(d.updated + ' donation(s) complétée(s)'); loadTable(); loadStats(); })
      .catch(function() { showToast('Erreur', true); });
  };
  document.getElementById('deletePendingBtn').onclick = function() {
    if (!confirm('Supprimer TOUTES les donations pending ?')) return;
    fetch(BASE + '/admin/cleanup?password=' + PWD, {method:'POST'})
      .then(function(r) { return r.json(); })
      .then(function(d) { showToast(d.deleted + ' supprimée(s)'); loadTable(); loadStats(); })
      .catch(function() { showToast('Erreur', true); });
  };

  loadStats(); loadTable();
  setInterval(function() { loadStats(); loadTable(); }, 20000);
})();
</script>
</body>
</html>"""
    return html


@app.route('/admin/stats', methods=['GET'])
def admin_stats():
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) AS n FROM donations")
        total = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) AS n FROM donations WHERE status = 'pending'")
        pending = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) AS n FROM donations WHERE status = 'completed'")
        completed = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) AS n FROM donations WHERE status = 'failed'")
        failed = c.fetchone()["n"]
        c.close(); conn.close()
        db_info = "non configurée"
        if DATABASE_URL:
            try:
                from urllib.parse import urlparse
                u = urlparse(DATABASE_URL)
                db_info = u.hostname or "supabase"
            except Exception:
                db_info = "supabase"
        return jsonify({
            "total": total, "pending": pending,
            "completed": completed, "failed": failed,
            "db_info": db_info
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/donations', methods=['GET'])
def admin_list_donations():
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            SELECT id, player_id, target_player_id, amount_robux, final_amount, status, created_at
            FROM donations ORDER BY created_at DESC
        ''')
        rows = c.fetchall()
        c.close(); conn.close()
        donations = []
        for row in rows:
            row = dict(row)
            donor_info  = get_user_info(row["player_id"])
            target_info = get_user_info(row["target_player_id"])
            donations.append({
                "id": row["id"],
                "player_id": row["player_id"],
                "donor_name":  donor_info.get("name")  if donor_info  else None,
                "target_player_id": row["target_player_id"],
                "target_name": target_info.get("name") if target_info else None,
                "amount_robux": row["amount_robux"],
                "final_amount": row["final_amount"],
                "status": row["status"],
                "created_at": str(row["created_at"])
            })
        return jsonify({"donations": donations}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/donations/<int:donation_id>/status', methods=['POST'])
def admin_update_status(donation_id):
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        data = request.get_json()
        new_status = data.get("status")
        force_transfer = data.get("force_transfer", False)

        if new_status not in ("pending", "completed", "failed"):
            return jsonify({"error": "Invalid status"}), 400

        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM donations WHERE id = %s', (donation_id,))
        row = c.fetchone()
        if not row:
            c.close(); conn.close()
            return jsonify({"error": "Donation not found"}), 404
        donation = dict(row)

        if new_status == "completed" and force_transfer:
            target_user_id = donation['target_player_id']
            amount = donation['final_amount']

            success, roblox_proof = transfer_robux(target_user_id, amount)

            if success:
                c.execute('''
                    UPDATE donations SET status = 'completed', processed_at = NOW() WHERE id = %s
                ''', (donation_id,))
                conn.commit()
                c.close(); conn.close()

                donor_info  = get_user_info(donation['player_id'])
                target_info = get_user_info(donation['target_player_id'])
                donor_name  = donor_info.get("name",  str(donation['player_id']))        if donor_info  else str(donation['player_id'])
                target_name = target_info.get("name", str(donation['target_player_id'])) if target_info else str(donation['target_player_id'])
                taxes = donation['amount_robux'] - donation['final_amount']
                edit_discord_success(
                    donation.get('discord_message_id'),
                    donor_name, donation['player_id'],
                    target_name, donation['target_player_id'],
                    donation['amount_robux'], donation['final_amount'],
                    taxes, donation_id, roblox_proof
                )
                logger.info(f"✅ Transfert admin forcé réussi pour donation #{donation_id}: {amount} R$ → {target_user_id}")
                return jsonify({"success": True, "transferred": True, "proof": roblox_proof}), 200
            else:
                c.execute('''
                    UPDATE donations SET status = 'failed', retry_count = retry_count + 1, last_retry = NOW() WHERE id = %s
                ''', (donation_id,))
                conn.commit()
                c.close(); conn.close()

                donor_info  = get_user_info(donation['player_id'])
                target_info = get_user_info(donation['target_player_id'])
                donor_name  = donor_info.get("name",  str(donation['player_id']))        if donor_info  else str(donation['player_id'])
                target_name = target_info.get("name", str(donation['target_player_id'])) if target_info else str(donation['target_player_id'])
                edit_discord_failed(
                    donation.get('discord_message_id'),
                    donor_name, donation['player_id'],
                    target_name, donation['target_player_id'],
                    donation['amount_robux'], donation['final_amount'],
                    donation_id, f"Échec transfert admin (HTTP {roblox_proof.get('httpStatus', '?')})"
                )
                logger.error(f"❌ Transfert admin échoué pour donation #{donation_id}: {roblox_proof}")
                return jsonify({"success": False, "transferred": False, "proof": roblox_proof}), 200

        c.execute('UPDATE donations SET status = %s WHERE id = %s', (new_status, donation_id))
        conn.commit()
        c.close(); conn.close()
        return jsonify({"success": True, "transferred": False}), 200

    except Exception as e:
        logger.error(f"Erreur admin_update_status: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/admin/donations/<int:donation_id>', methods=['DELETE'])
def admin_delete_donation(donation_id):
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('DELETE FROM donations WHERE id = %s', (donation_id,))
        conn.commit()
        c.close(); conn.close()
        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/cleanup', methods=['POST'])
def admin_cleanup_pending():
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) AS n FROM donations WHERE status = 'pending'")
        cnt = c.fetchone()["n"]
        c.execute("DELETE FROM donations WHERE status = 'pending'")
        conn.commit()
        c.close(); conn.close()
        return jsonify({"success": True, "deleted": cnt}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/mark-completed', methods=['POST'])
def admin_mark_completed():
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE donations SET status = 'completed' WHERE status = 'pending'")
        conn.commit()
        updated = c.rowcount
        c.close(); conn.close()
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
        logger.error(f"❌ Erreur connexion DB: {e}")
else:
    logger.warning("⚠️ DATABASE_URL non configurée")

logger.info(f"🔧 DevProducts: {list(DEVPRODUCT_AMOUNTS.keys())}")
logger.info(f"🔑 Roblox API Key: {'✅ OK (Open Cloud)' if ROBLOX_API_KEY else '❌ non configurée'}")
logger.info(f"🍪 Cookie Roblox:  {'✅ OK' if ACCOUNT_COOKIE else '❌ non configuré'}")
logger.info(f"🔐 2FA TOTP:       {'✅ OK (pyotp)' if ROBLOX_2FA_SECRET and PYOTP_AVAILABLE else '❌ non configuré'}")
logger.info(f"🔔 Discord:        {'✅ OK' if DISCORD_WEBHOOK_URL else '❌ non configuré'}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False, use_reloader=False)
