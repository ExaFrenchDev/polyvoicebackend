from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import os
import sqlite3
import hashlib
import hmac
import logging
import requests
import math
import json
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Configuration
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
GROUP_ID = int(os.getenv("GROUP_ID", "0"))
ACCOUNT_COOKIE = os.getenv("ROBLOX_ACCOUNT_COOKIE", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DATABASE_PATH = "donations.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEVPRODUCT_AMOUNTS = {
    "3593525234": 1,
    "3593525497": 50,
    "3593525652": 100,
}

TAX_RATE = 0.40
WAIT_DAYS = 7
MIN_GROUP_TENURE_DAYS = 7


# ============================================================================
# DATABASE
# ============================================================================

def init_db():
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS donations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            target_player_id INTEGER NOT NULL,
            devproduct_id TEXT NOT NULL,
            amount_robux INTEGER NOT NULL,
            final_amount INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_at TIMESTAMP,
            retry_count INTEGER DEFAULT 0,
            last_retry TIMESTAMP,
            discord_message_id TEXT
        )
    ''')
    # Migration: ajouter discord_message_id si la table existait déjà sans cette colonne
    try:
        c.execute('ALTER TABLE donations ADD COLUMN discord_message_id TEXT')
        logger.info("✅ Colonne discord_message_id ajoutée")
    except Exception:
        pass
    conn.commit()
    conn.close()


def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
                return {"eligible": False, "reason": f"Not long enough in group ({days_in_group}/{days_required} days)"}
        except Exception as e:
            logger.error(f"Erreur parsing date: {e}")
    return {"eligible": True, "reason": "Meets all requirements"}


def transfer_robux(target_user_id, amount_robux):
    try:
        session = requests.Session()
        session.cookies.set('.ROBLOSECURITY', ACCOUNT_COOKIE)
        headers = {
            'X-CSRF-TOKEN': '',
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        }
        csrf_response = session.post('https://www.roblox.com/home', headers=headers, timeout=5)
        if 'X-CSRF-TOKEN' in csrf_response.headers:
            headers['X-CSRF-TOKEN'] = csrf_response.headers['X-CSRF-TOKEN']

        transfer_payload = {
            'recipientId': target_user_id,
            'robux': amount_robux,
            'expectedCost': amount_robux
        }
        transfer_response = session.post(
            'https://www.roblox.com/api/v1/user/economic-transactions/send-robux',
            json=transfer_payload,
            headers=headers,
            timeout=10
        )

        roblox_proof = {
            "endpoint": "send-robux",
            "recipientId": target_user_id,
            "amountSent": amount_robux,
            "httpStatus": transfer_response.status_code,
            "response": transfer_response.json() if transfer_response.content else {},
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }

        if transfer_response.status_code == 200:
            result = transfer_response.json()
            if result.get('success'):
                logger.info(f"✅ Transfert réussi: {amount_robux} Robux à {target_user_id}")
                return True, roblox_proof
            else:
                logger.error(f"❌ Erreur transfert: {result.get('message')}")
                return False, roblox_proof
        else:
            logger.error(f"❌ Erreur HTTP {transfer_response.status_code}")
            return False, roblox_proof

    except Exception as e:
        logger.error(f"❌ Erreur transfert Robux: {e}")
        return False, {"error": str(e), "timestamp": datetime.utcnow().isoformat() + "Z"}


# ============================================================================
# DISCORD NOTIFICATIONS
# ============================================================================

def send_discord_notification(donor_id, donor_name, target_id, target_name,
                               amount, final_amount, taxes, donation_id, estimated_date):
    """Envoie l'embed initial et retourne le message_id Discord"""
    if not DISCORD_WEBHOOK_URL:
        logger.warning("⚠️ DISCORD_WEBHOOK_URL non configurée")
        return None

    embed = {
        "title": "💰 Nouvelle Donation en attente",
        "color": 0xFFD700,
        "fields": [
            {"name": "👤 Donateur",          "value": f"`{donor_name}` (ID: `{donor_id}`)",  "inline": False},
            {"name": "🎯 Receveur",           "value": f"`{target_name}` (ID: `{target_id}`)", "inline": False},
            {"name": "💵 Montant brut",       "value": f"`{amount} Robux`",                   "inline": True},
            {"name": "🏦 Taxe Roblox (-40%)", "value": f"`{taxes} Robux`",                    "inline": True},
            {"name": "✨ Montant net",         "value": f"`{final_amount} Robux`",             "inline": True},
            {"name": "📅 Transfert prévu le", "value": f"`{estimated_date}`",                 "inline": False},
            {"name": "🔖 ID Donation",        "value": f"`#{donation_id}`",                   "inline": True},
            {"name": "⏳ Statut",              "value": "`En attente (7 jours)`",              "inline": True},
        ],
        "footer": {"text": "PolyVoice Donation System"},
        "timestamp": datetime.utcnow().isoformat()
    }

    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL + "?wait=true",
            json={"embeds": [embed]},
            timeout=5
        )
        if resp.status_code in (200, 204):
            message_id = resp.json().get("id")
            logger.info(f"✅ Notification Discord envoyée (message_id: {message_id})")
            return message_id
        else:
            logger.error(f"❌ Erreur Discord webhook: HTTP {resp.status_code} — {resp.text}")
            return None
    except Exception as e:
        logger.error(f"❌ Erreur envoi Discord: {e}")
        return None


def edit_discord_success(message_id, donor_name, donor_id, target_name, target_id,
                          amount, final_amount, taxes, donation_id, roblox_proof):
    """Édite l'embed pour confirmer le transfert avec preuve Roblox"""
    if not DISCORD_WEBHOOK_URL or not message_id:
        return

    proof_str = json.dumps(roblox_proof, indent=2, ensure_ascii=False)
    # Discord limite les fields à 1024 chars
    if len(proof_str) > 950:
        proof_str = proof_str[:950] + "\n... (tronqué)"

    embed = {
        "title": "✅ Donation transférée avec succès!",
        "color": 0x00FF7F,
        "fields": [
            {"name": "👤 Donateur",            "value": f"`{donor_name}` (ID: `{donor_id}`)",   "inline": False},
            {"name": "🎯 Receveur",             "value": f"`{target_name}` (ID: `{target_id}`)", "inline": False},
            {"name": "💵 Montant brut",         "value": f"`{amount} Robux`",                    "inline": True},
            {"name": "🏦 Taxe Roblox (-40%)",   "value": f"`{taxes} Robux`",                     "inline": True},
            {"name": "✨ Montant reçu",          "value": f"`{final_amount} Robux`",              "inline": True},
            {"name": "🔖 ID Donation",          "value": f"`#{donation_id}`",                    "inline": True},
            {"name": "✅ Statut",                "value": "`Transféré`",                          "inline": True},
            {"name": "📄 Preuve de transfert Roblox", "value": f"```json\n{proof_str}\n```",     "inline": False},
        ],
        "footer": {"text": "PolyVoice Donation System"},
        "timestamp": datetime.utcnow().isoformat()
    }

    try:
        resp = requests.patch(
            f"{DISCORD_WEBHOOK_URL}/messages/{message_id}",
            json={"embeds": [embed]},
            timeout=5
        )
        if resp.status_code in (200, 204):
            logger.info(f"✅ Embed Discord mis à jour (succès) pour donation #{donation_id}")
        else:
            logger.error(f"❌ Erreur édition embed: HTTP {resp.status_code} — {resp.text}")
    except Exception as e:
        logger.error(f"❌ Erreur édition embed Discord: {e}")


def edit_discord_failed(message_id, donor_name, donor_id, target_name, target_id,
                         amount, final_amount, donation_id, reason):
    """Édite l'embed pour signaler un échec"""
    if not DISCORD_WEBHOOK_URL or not message_id:
        return

    embed = {
        "title": "❌ Échec du transfert",
        "color": 0xFF4444,
        "fields": [
            {"name": "👤 Donateur",   "value": f"`{donor_name}` (ID: `{donor_id}`)",   "inline": False},
            {"name": "🎯 Receveur",   "value": f"`{target_name}` (ID: `{target_id}`)", "inline": False},
            {"name": "💵 Montant brut","value": f"`{amount} Robux`",                    "inline": True},
            {"name": "✨ Montant net", "value": f"`{final_amount} Robux`",              "inline": True},
            {"name": "🔖 ID Donation","value": f"`#{donation_id}`",                    "inline": True},
            {"name": "❌ Statut",     "value": "`Échec — sera retenté`",               "inline": True},
            {"name": "⚠️ Raison",    "value": f"`{reason}`",                           "inline": False},
        ],
        "footer": {"text": "PolyVoice Donation System"},
        "timestamp": datetime.utcnow().isoformat()
    }

    try:
        resp = requests.patch(
            f"{DISCORD_WEBHOOK_URL}/messages/{message_id}",
            json={"embeds": [embed]},
            timeout=5
        )
        if resp.status_code in (200, 204):
            logger.info(f"✅ Embed Discord mis à jour (échec) pour donation #{donation_id}")
        else:
            logger.error(f"❌ Erreur édition embed: HTTP {resp.status_code} — {resp.text}")
    except Exception as e:
        logger.error(f"❌ Erreur édition embed Discord: {e}")


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
        user_id = data.get("userId")
        target_user_id = data.get("targetUserId")
        devproduct_id = str(data.get("devProductId"))

        if not all([user_id, target_user_id, devproduct_id]):
            return jsonify({"error": "Missing fields"}), 400

        if devproduct_id not in DEVPRODUCT_AMOUNTS:
            return jsonify({"error": "Unknown DevProduct"}), 400

        amount = DEVPRODUCT_AMOUNTS[devproduct_id]
        final_amount = math.ceil(amount * (1 - TAX_RATE))
        taxes = amount - final_amount
        estimated_date = (datetime.now() + timedelta(days=WAIT_DAYS)).strftime("%d/%m/%Y")

        donor_info = get_user_info(user_id)
        target_info = get_user_info(target_user_id)
        donor_name = donor_info.get("name", str(user_id)) if donor_info else str(user_id)
        target_name = target_info.get("name", str(target_user_id)) if target_info else str(target_user_id)

        conn = get_db()
        c = conn.cursor()
        c.execute('''
            INSERT INTO donations 
            (player_id, target_player_id, devproduct_id, amount_robux, final_amount, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
        ''', (user_id, target_user_id, devproduct_id, amount, final_amount))
        conn.commit()
        donation_id = c.lastrowid

        message_id = send_discord_notification(
            user_id, donor_name,
            target_user_id, target_name,
            amount, final_amount, taxes,
            donation_id, estimated_date
        )

        if message_id:
            c.execute('UPDATE donations SET discord_message_id = ? WHERE id = ?', (message_id, donation_id))
            conn.commit()

        conn.close()
        logger.info(f"✅ Donation {donation_id} enregistrée: {user_id} -> {target_user_id} ({amount} Robux)")

        return jsonify({
            "success": True,
            "donationId": donation_id,
            "message": f"Donation enregistrée: {final_amount} Robux seront transférés dans {WAIT_DAYS} jours",
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
        cutoff_date = datetime.now() - timedelta(days=WAIT_DAYS)
        c.execute('''
            SELECT * FROM donations 
            WHERE status = 'pending' AND created_at <= ? AND retry_count < 5
            ORDER BY created_at ASC LIMIT 5
        ''', (cutoff_date,))
        donations = [dict(row) for row in c.fetchall()]
        conn.close()
        logger.info(f"📋 /pending: {len(donations)} donation(s) retournée(s)")
        return jsonify({"success": True, "donations": donations}), 200
    except Exception as e:
        logger.error(f"Erreur get_pending_donations: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/donations/<int:donation_id>/status', methods=['POST'])
def update_donation_status(donation_id):
    try:
        data = request.get_json()
        new_status = data.get("status")
        roblox_proof = data.get("roblox_proof", None)
        fail_reason = data.get("reason", "Erreur inconnue")

        if new_status not in ("completed", "pending", "requeue", "failed"):
            return jsonify({"error": "Invalid status"}), 400

        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM donations WHERE id = ?', (donation_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Donation not found"}), 404

        donation = dict(row)

        if new_status == "completed":
            c.execute('''
                UPDATE donations SET status = 'completed', processed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (donation_id,))
        else:
            c.execute('''
                UPDATE donations SET status = ?, retry_count = retry_count + 1, last_retry = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (new_status, donation_id))

        conn.commit()
        conn.close()

        donor_info = get_user_info(donation['player_id'])
        target_info = get_user_info(donation['target_player_id'])
        donor_name = donor_info.get("name", str(donation['player_id'])) if donor_info else str(donation['player_id'])
        target_name = target_info.get("name", str(donation['target_player_id'])) if target_info else str(donation['target_player_id'])
        taxes = donation['amount_robux'] - donation['final_amount']
        message_id = donation.get('discord_message_id')

        if new_status == "completed" and roblox_proof:
            edit_discord_success(
                message_id,
                donor_name, donation['player_id'],
                target_name, donation['target_player_id'],
                donation['amount_robux'], donation['final_amount'], taxes,
                donation_id, roblox_proof
            )
        elif new_status in ("failed", "requeue"):
            edit_discord_failed(
                message_id,
                donor_name, donation['player_id'],
                target_name, donation['target_player_id'],
                donation['amount_robux'], donation['final_amount'],
                donation_id, fail_reason
            )

        logger.info(f"✅ Donation {donation_id} mise à jour: {new_status}")
        return jsonify({"success": True}), 200

    except Exception as e:
        logger.error(f"Erreur update_donation_status: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/status/<int:donation_id>', methods=['GET'])
def get_donation_status(donation_id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM donations WHERE id = ?', (donation_id,))
        donation = c.fetchone()
        conn.close()
        if not donation:
            return jsonify({"error": "Donation not found"}), 404
        return jsonify({
            "id": donation['id'],
            "status": donation['status'],
            "amount": donation['amount_robux'],
            "finalAmount": donation['final_amount'],
            "createdAt": donation['created_at'],
            "processedAt": donation['processed_at'],
            "targetUserId": donation['target_player_id']
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/list', methods=['GET'])
def list_donations():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM donations ORDER BY created_at DESC LIMIT 50')
        donations = c.fetchall()
        conn.close()
        return jsonify({
            "success": True,
            "count": len(donations),
            "donations": [
                {
                    "id": d['id'], "playerId": d['player_id'], "targetId": d['target_player_id'],
                    "amount": d['amount_robux'], "finalAmount": d['final_amount'],
                    "status": d['status'], "createdAt": d['created_at'], "processedAt": d['processed_at']
                }
                for d in donations
            ]
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()}), 200


@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "service": "Roblox Donation Backend", "status": "running",
        "endpoints": {
            "webhook": "/webhook/devproduct (POST)", "pending": "/pending (GET)",
            "update_status": "/donations/<id>/status (POST)", "status": "/status/<donation_id> (GET)",
            "list": "/list (GET)", "health": "/health (GET)"
        }
    }), 200


# ============================================================================
# STARTUP
# ============================================================================

init_db()
logger.info("✅ API Donation en cours de démarrage...")
logger.info(f"🔧 DevProducts configurés: {list(DEVPRODUCT_AMOUNTS.keys())}")
logger.info(f"🔔 Discord webhook: {'configuré' if DISCORD_WEBHOOK_URL else 'NON configuré — ajoute DISCORD_WEBHOOK_URL dans les env vars Render'}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False, use_reloader=False)
