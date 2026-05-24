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
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "exa14170.")
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

        # Récupérer le CSRF token
        csrf_response = session.post('https://auth.roblox.com/v2/logout', timeout=5)
        csrf_token = csrf_response.headers.get('x-csrf-token', '')

        headers = {
            'X-CSRF-TOKEN': csrf_token,
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        }

        payload = {
            "PayoutType": "FixedAmount",
            "Recipients": [
                {
                    "recipientId": target_user_id,
                    "recipientType": "User",
                    "amount": amount_robux
                }
            ]
        }

        response = session.post(
            f'https://groups.roblox.com/v1/groups/{GROUP_ID}/payouts',
            json=payload,
            headers=headers,
            timeout=10
        )

        roblox_proof = {
            "endpoint": f"groups/{GROUP_ID}/payouts",
            "recipientId": target_user_id,
            "amountSent": amount_robux,
            "httpStatus": response.status_code,
            "response": response.json() if response.content else {},
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }

        if response.status_code == 200:
            logger.info(f"✅ Payout réussi: {amount_robux} Robux à {target_user_id}")
            return True, roblox_proof
        else:
            logger.error(f"❌ Erreur payout: {response.status_code} — {response.text}")
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

@app.route('/admin', methods=['GET'])
def dashboard():
    """Dashboard HTML pro et modern"""
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return "❌ Accès refusé", 403
    
    html = """<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Donations</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Oxygen', 'Ubuntu', 'Cantarell', sans-serif;
            background: linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%);
            min-height: 100vh;
            color: #333;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 30px 20px;
        }
        
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 40px;
            color: white;
        }
        
        .header h1 {
            font-size: 32px;
            font-weight: 700;
            letter-spacing: -0.5px;
        }
        
        .header-right {
            display: flex;
            gap: 15px;
            align-items: center;
        }
        
        .time {
            font-size: 13px;
            opacity: 0.8;
            background: rgba(255,255,255,0.1);
            padding: 8px 16px;
            border-radius: 20px;
            backdrop-filter: blur(10px);
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 20px;
            margin-bottom: 40px;
        }
        
        .stat-card {
            background: white;
            border-radius: 16px;
            padding: 24px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            border: 1px solid rgba(0,0,0,0.05);
        }
        
        .stat-card:hover {
            transform: translateY(-4px);
            box-shadow: 0 12px 40px rgba(0,0,0,0.15);
        }
        
        .stat-label {
            font-size: 12px;
            font-weight: 600;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 12px;
        }
        
        .stat-number {
            font-size: 36px;
            font-weight: 700;
            color: #111;
            margin-bottom: 8px;
        }
        
        .stat-change {
            font-size: 12px;
            color: #999;
        }
        
        .stat-card.primary .stat-number { color: #6366f1; }
        .stat-card.success .stat-number { color: #10b981; }
        .stat-card.warning .stat-number { color: #f59e0b; }
        .stat-card.danger .stat-number { color: #ef4444; }
        
        .content {
            display: grid;
            grid-template-columns: 1fr;
            gap: 20px;
        }
        
        .card {
            background: white;
            border-radius: 16px;
            padding: 28px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
            border: 1px solid rgba(0,0,0,0.05);
        }
        
        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            padding-bottom: 16px;
            border-bottom: 2px solid #f3f4f6;
        }
        
        .card-title {
            font-size: 18px;
            font-weight: 700;
            color: #111;
        }
        
        .controls {
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
        }
        
        button {
            padding: 10px 20px;
            border: none;
            border-radius: 10px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 600;
            transition: all 0.2s ease;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        
        .btn {
            background: #f3f4f6;
            color: #333;
            border: 1px solid #e5e7eb;
        }
        
        .btn:hover {
            background: #e5e7eb;
        }
        
        .btn-primary {
            background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
            color: white;
            border: none;
        }
        
        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 20px rgba(99, 102, 241, 0.35);
        }
        
        .btn-success {
            background: #10b981;
            color: white;
            border: none;
        }
        
        .btn-success:hover {
            background: #059669;
            transform: translateY(-2px);
        }
        
        .btn-danger {
            background: #ef4444;
            color: white;
            border: none;
        }
        
        .btn-danger:hover {
            background: #dc2626;
            transform: translateY(-2px);
        }
        
        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        
        .table-wrapper {
            overflow-x: auto;
            border-radius: 12px;
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }
        
        thead {
            background: #f9fafb;
            border-bottom: 2px solid #e5e7eb;
        }
        
        th {
            padding: 16px;
            text-align: left;
            font-weight: 600;
            color: #374151;
            text-transform: uppercase;
            font-size: 12px;
            letter-spacing: 0.5px;
        }
        
        td {
            padding: 16px;
            border-bottom: 1px solid #f3f4f6;
        }
        
        tbody tr {
            transition: background 0.15s ease;
        }
        
        tbody tr:hover {
            background: #f9fafb;
        }
        
        .status {
            display: inline-block;
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
        }
        
        .status.pending {
            background: #fef3c7;
            color: #92400e;
        }
        
        .status.completed {
            background: #dcfce7;
            color: #166534;
        }
        
        .status.failed {
            background: #fee2e2;
            color: #991b1b;
        }
        
        .actions {
            display: flex;
            gap: 6px;
        }
        
        .actions button {
            padding: 6px 12px;
            font-size: 12px;
            border-radius: 8px;
        }
        
        .message {
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 16px 20px;
            border-radius: 12px;
            display: none;
            align-items: center;
            gap: 12px;
            font-size: 14px;
            font-weight: 500;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            z-index: 1000;
            animation: slideIn 0.3s ease;
        }
        
        @keyframes slideIn {
            from {
                transform: translateX(400px);
                opacity: 0;
            }
            to {
                transform: translateX(0);
                opacity: 1;
            }
        }
        
        .message.show {
            display: flex;
        }
        
        .message.success {
            background: #10b981;
            color: white;
        }
        
        .message.error {
            background: #ef4444;
            color: white;
        }
        
        .message.info {
            background: #6366f1;
            color: white;
        }
        
        .loading {
            text-align: center;
            padding: 60px 20px;
            color: #999;
        }
        
        .loading i {
            font-size: 40px;
            margin-bottom: 16px;
            animation: spin 2s linear infinite;
        }
        
        @keyframes spin {
            from { transform: rotate(0deg); }
            to { transform: rotate(360deg); }
        }
        
        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: #999;
        }
        
        .empty-state i {
            font-size: 48px;
            margin-bottom: 16px;
            opacity: 0.3;
        }
        
        @media (max-width: 768px) {
            .header {
                flex-direction: column;
                gap: 16px;
                text-align: center;
            }
            
            .stats-grid {
                grid-template-columns: 1fr;
            }
            
            .card-header {
                flex-direction: column;
                align-items: flex-start;
                gap: 16px;
            }
            
            .controls {
                width: 100%;
            }
            
            button {
                flex: 1;
                justify-content: center;
            }
            
            table {
                font-size: 12px;
            }
            
            th, td {
                padding: 12px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>💰 Donations Admin</h1>
            </div>
            <div class="header-right">
                <div class="time" id="time"></div>
            </div>
        </div>
        
        <div class="stats-grid" id="statsGrid">
            <div class="loading"><i class="fas fa-spinner"></i></div>
        </div>
        
        <div class="content">
            <div class="card">
                <div class="card-header">
                    <h2 class="card-title">Donations</h2>
                    <div class="controls">
                        <button class="btn btn-primary" onclick="loadData()"><i class="fas fa-sync"></i> Rafraîchir</button>
                        <button class="btn btn-success" onclick="markAllCompleted()"><i class="fas fa-check-circle"></i> Tous completed</button>
                        <button class="btn btn-danger" onclick="deleteAllPending()"><i class="fas fa-trash"></i> Supprimer pending</button>
                    </div>
                </div>
                
                <div class="table-wrapper">
                    <table id="donationsTable">
                        <thead>
                            <tr>
                                <th>ID</th>
                                <th>Donateur</th>
                                <th>Receveur</th>
                                <th>Montant</th>
                                <th>Net</th>
                                <th>Statut</th>
                                <th>Date</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr><td colspan="8" class="loading"><i class="fas fa-spinner"></i> Chargement...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
    
    <div class="message" id="message"></div>
    
    <script>
        const API_BASE = window.location.origin;
        const PASSWORD = new URLSearchParams(window.location.search).get('password');
        
        function showMessage(text, type = 'info') {
            const msg = document.getElementById('message');
            msg.textContent = text;
            msg.className = `message show ${type}`;
            setTimeout(() => msg.classList.remove('show'), 3500);
        }
        
        function updateTime() {
            const now = new Date();
            document.getElementById('time').textContent = now.toLocaleTimeString('fr-FR');
        }
        setInterval(updateTime, 1000);
        updateTime();
        
        function loadStats() {
            fetch(`${API_BASE}/admin/stats?password=${PASSWORD}`)
                .then(r => r.json())
                .then(data => {
                    const grid = document.getElementById('statsGrid');
                    grid.innerHTML = `
                        <div class="stat-card primary">
                            <div class="stat-label">Total</div>
                            <div class="stat-number">${data.total}</div>
                        </div>
                        <div class="stat-card warning">
                            <div class="stat-label">En attente</div>
                            <div class="stat-number">${data.pending}</div>
                        </div>
                        <div class="stat-card success">
                            <div class="stat-label">Complétées</div>
                            <div class="stat-number">${data.completed}</div>
                        </div>
                        <div class="stat-card danger">
                            <div class="stat-label">Échouées</div>
                            <div class="stat-number">${data.failed}</div>
                        </div>
                    `;
                });
        }
        
        function loadData() {
            fetch(`${API_BASE}/admin/donations?password=${PASSWORD}`)
                .then(r => r.json())
                .then(data => {
                    const tbody = document.querySelector('tbody');
                    if (!data.donations || data.donations.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="8" class="empty-state"><i class="fas fa-inbox"></i><br>Aucune donation</td></tr>';
                        return;
                    }
                    
                    tbody.innerHTML = data.donations.map(d => `
                        <tr>
                            <td><strong>#${d.id}</strong></td>
                            <td>${d.donor_name || d.player_id}</td>
                            <td>${d.target_name || d.target_player_id}</td>
                            <td>${d.amount_robux}R$</td>
                            <td>${d.final_amount}R$</td>
                            <td><span class="status ${d.status}">${d.status.toUpperCase()}</span></td>
                            <td>${new Date(d.created_at).toLocaleDateString('fr-FR')}</td>
                            <td>
                                <div class="actions">
                                    <button class="btn btn-success" onclick="changeStatus(${d.id}, 'completed')" title="Marquer complété"><i class="fas fa-check"></i></button>
                                    <button class="btn btn-danger" onclick="deleteSingle(${d.id})" title="Supprimer"><i class="fas fa-trash"></i></button>
                                </div>
                            </td>
                        </tr>
                    `).join('');
                })
                .catch(e => showMessage('Erreur de chargement', 'error'));
        }
        
        function changeStatus(id, status) {
            fetch(`${API_BASE}/admin/donations/${id}/status?password=${PASSWORD}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ status })
            })
            .then(r => r.json())
            .then(() => {
                showMessage(`✓ Donation #${id} → ${status}`, 'success');
                loadData();
                loadStats();
            });
        }
        
        function deleteSingle(id) {
            if (confirm(`Supprimer donation #${id}?`)) {
                fetch(`${API_BASE}/admin/donations/${id}?password=${PASSWORD}`, { method: 'DELETE' })
                    .then(r => r.json())
                    .then(() => {
                        showMessage(`✓ Donation #${id} supprimée`, 'success');
                        loadData();
                        loadStats();
                    });
            }
        }
        
        function deleteAllPending() {
            if (confirm('⚠️  Supprimer TOUTES les donations pending?\nCette action est irréversible!')) {
                fetch(`${API_BASE}/admin/cleanup?password=${PASSWORD}`, { method: 'POST' })
                    .then(r => r.json())
                    .then(data => {
                        showMessage(`✓ ${data.deleted} donations supprimées`, 'success');
                        loadData();
                        loadStats();
                    });
            }
        }
        
        function markAllCompleted() {
            if (confirm('Marquer TOUS les pending en completed?')) {
                fetch(`${API_BASE}/admin/mark-completed?password=${PASSWORD}`, { method: 'POST' })
                    .then(r => r.json())
                    .then(data => {
                        showMessage(`✓ ${data.updated} donations marquées completed`, 'success');
                        loadData();
                        loadStats();
                    });
            }
        }
        
        // Init
        loadStats();
        loadData();
        
        // Refresh toutes les 15s
        setInterval(() => {
            loadStats();
            loadData();
        }, 15000);
    </script>
</body>
</html>"""
    return html
 
 
@app.route('/admin/stats', methods=['GET'])
def admin_stats():
    """Récupère les stats de donations"""
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        conn = get_db()
        c = conn.cursor()
        
        c.execute("SELECT COUNT(*) FROM donations")
        total = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM donations WHERE status = 'pending'")
        pending = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM donations WHERE status = 'completed'")
        completed = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM donations WHERE status = 'failed'")
        failed = c.fetchone()[0]
        
        conn.close()
        
        return jsonify({
            "total": total,
            "pending": pending,
            "completed": completed,
            "failed": failed
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
@app.route('/admin/donations', methods=['GET'])
def admin_list_donations():
    """Liste toutes les donations avec infos du donateur"""
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        conn = get_db()
        c = conn.cursor()
        
        c.execute('''
            SELECT id, player_id, target_player_id, amount_robux, final_amount, status, created_at 
            FROM donations 
            ORDER BY created_at DESC
        ''')
        
        donations = []
        for row in c.fetchall():
            donor_info = get_user_info(row[1])
            target_info = get_user_info(row[2])
            
            donations.append({
                "id": row[0],
                "player_id": row[1],
                "donor_name": donor_info.get("name") if donor_info else None,
                "target_player_id": row[2],
                "target_name": target_info.get("name") if target_info else None,
                "amount_robux": row[3],
                "final_amount": row[4],
                "status": row[5],
                "created_at": row[6]
            })
        
        conn.close()
        
        return jsonify({"donations": donations}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
@app.route('/admin/donations/<int:donation_id>/status', methods=['POST'])
def admin_update_status(donation_id):
    """Met à jour le statut d'une donation"""
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        data = request.get_json()
        new_status = data.get("status")
        
        if new_status not in ("pending", "completed", "failed"):
            return jsonify({"error": "Invalid status"}), 400
        
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE donations SET status = ? WHERE id = ?', (new_status, donation_id))
        conn.commit()
        conn.close()
        
        logger.info(f"✅ Donation {donation_id} → {new_status}")
        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
@app.route('/admin/donations/<int:donation_id>', methods=['DELETE'])
def admin_delete_donation(donation_id):
    """Supprime une donation"""
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('DELETE FROM donations WHERE id = ?', (donation_id,))
        conn.commit()
        conn.close()
        
        logger.info(f"🗑️ Donation {donation_id} supprimée")
        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
@app.route('/admin/cleanup', methods=['POST'])
def admin_cleanup_pending():
    """Supprime toutes les donations pending"""
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        conn = get_db()
        c = conn.cursor()
        
        c.execute("SELECT COUNT(*) FROM donations WHERE status = 'pending'")
        deleted_count = c.fetchone()[0]
        
        c.execute("DELETE FROM donations WHERE status = 'pending'")
        conn.commit()
        conn.close()
        
        logger.info(f"🧹 {deleted_count} donations pending supprimées")
        return jsonify({"success": True, "deleted": deleted_count}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
@app.route('/admin/mark-completed', methods=['POST'])
def admin_mark_completed():
    """Marque tous les pending en completed"""
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        conn = get_db()
        c = conn.cursor()
        
        c.execute("UPDATE donations SET status = 'completed' WHERE status = 'pending'")
        conn.commit()
        
        updated = c.rowcount
        conn.close()
        
        logger.info(f"✅ {updated} donations marquées completed")
        return jsonify({"success": True, "updated": updated}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 


# ============================================================================
# STARTUP
# ============================================================================

init_db()
logger.info("✅ API Donation en cours de démarrage...")
logger.info(f"🔧 DevProducts configurés: {list(DEVPRODUCT_AMOUNTS.keys())}")
logger.info(f"🔔 Discord webhook: {'configuré' if DISCORD_WEBHOOK_URL else 'NON configuré — ajoute DISCORD_WEBHOOK_URL dans les env vars Render'}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False, use_reloader=False)
