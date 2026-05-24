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

TAX_RATE = 0.60
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
            {"name": f"🏦 Taxe Roblox (-{TAX_RATE * 100}%)",   "value": f"`{taxes} Robux`",                     "inline": True},
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
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return "Access denied", 403
    
    html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: sans-serif; background: #f5f5f5; color: #333; }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        .header { background: white; padding: 20px; margin-bottom: 20px; border-radius: 6px; display: flex; justify-content: space-between; }
        .header h1 { font-size: 24px; font-weight: 600; }
        .time { color: #666; }
        .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }
        .stat { background: white; padding: 20px; border-radius: 6px; border-left: 4px solid #ccc; }
        .stat.total { border-left-color: #666; }
        .stat.pending { border-left-color: #f59e0b; }
        .stat.completed { border-left-color: #10b981; }
        .stat.failed { border-left-color: #ef4444; }
        .stat-label { font-size: 12px; color: #666; text-transform: uppercase; margin-bottom: 8px; }
        .stat-value { font-size: 32px; font-weight: 700; }
        .card { background: white; border-radius: 6px; overflow: hidden; }
        .card-header { padding: 20px; border-bottom: 1px solid #eee; display: flex; justify-content: space-between; align-items: center; }
        .card-title { font-size: 16px; font-weight: 600; }
        .controls { display: flex; gap: 8px; }
        button { padding: 8px 16px; border: none; border-radius: 4px; font-size: 13px; font-weight: 500; cursor: pointer; }
        .btn-default { background: #f3f4f6; color: #333; border: 1px solid #e5e7eb; }
        .btn-default:hover { background: #e5e7eb; }
        .btn-success { background: #10b981; color: white; }
        .btn-success:hover { background: #059669; }
        .btn-danger { background: #ef4444; color: white; }
        .btn-danger:hover { background: #dc2626; }
        .table-wrapper { overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        thead { background: #f9fafb; }
        th { padding: 12px 16px; text-align: left; font-weight: 600; color: #666; font-size: 12px; text-transform: uppercase; border-bottom: 1px solid #eee; }
        td { padding: 12px 16px; border-bottom: 1px solid #f3f4f6; }
        tbody tr:hover { background: #fafafa; }
        .badge { display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 11px; font-weight: 600; }
        .badge-pending { background: #fef3c7; color: #92400e; }
        .badge-completed { background: #d1fae5; color: #065f46; }
        .badge-failed { background: #fee2e2; color: #991b1b; }
        .actions { display: flex; gap: 4px; }
        .actions button { padding: 4px 8px; font-size: 11px; }
        .toast { position: fixed; top: 20px; right: 20px; padding: 12px 20px; border-radius: 4px; display: none; font-size: 13px; z-index: 1000; }
        .toast.show { display: block; }
        .toast.success { background: #10b981; color: white; }
        .loading { text-align: center; padding: 40px; color: #999; }
        .empty { text-align: center; padding: 60px 20px; color: #999; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Donations Admin</h1>
            <div class="time" id="time"></div>
        </div>
        
        <div class="stats" id="statsContainer">
            <div class="loading">Loading...</div>
        </div>
        
        <div class="card">
            <div class="card-header">
                <div class="card-title">Donations</div>
                <div class="controls">
                    <button class="btn-default" id="refreshBtn">Refresh</button>
                    <button class="btn-success" id="completedBtn">All completed</button>
                    <button class="btn-danger" id="deleteBtn">Delete pending</button>
                </div>
            </div>
            <div class="table-wrapper">
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Donor</th>
                            <th>Receiver</th>
                            <th>Amount</th>
                            <th>Net</th>
                            <th>Status</th>
                            <th>Date</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody id="tableBody">
                        <tr><td colspan="8" class="loading">Loading...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>
    
    <div class="toast" id="toast"></div>
    
    <script>
    var API = window.location.origin;
    var PASSWORD = new URLSearchParams(window.location.search).get("password");
    
    function showToast(msg) {
        var t = document.getElementById("toast");
        t.textContent = msg;
        t.className = "toast show success";
        setTimeout(function() { t.classList.remove("show"); }, 3000);
    }
    
    function updateTime() {
        var now = new Date();
        document.getElementById("time").textContent = now.toLocaleTimeString("fr-FR");
    }
    setInterval(updateTime, 1000);
    updateTime();
    
    function loadStats() {
        fetch(API + "/admin/stats?password=" + PASSWORD)
            .then(function(r) { return r.json(); })
            .then(function(d) {
                var html = "";
                html += "<div class='stat total'><div class='stat-label'>Total</div><div class='stat-value'>" + d.total + "</div></div>";
                html += "<div class='stat pending'><div class='stat-label'>Pending</div><div class='stat-value'>" + d.pending + "</div></div>";
                html += "<div class='stat completed'><div class='stat-label'>Completed</div><div class='stat-value'>" + d.completed + "</div></div>";
                html += "<div class='stat failed'><div class='stat-label'>Failed</div><div class='stat-value'>" + d.failed + "</div></div>";
                document.getElementById("statsContainer").innerHTML = html;
            });
    }
    
    function loadData() {
        fetch(API + "/admin/donations?password=" + PASSWORD)
            .then(function(r) { return r.json(); })
            .then(function(d) {
                var tbody = document.getElementById("tableBody");
                if (!d.donations || d.donations.length === 0) {
                    tbody.innerHTML = "<tr><td colspan='8' class='empty'>No donations</td></tr>";
                    return;
                }
                var html = "";
                for (var i = 0; i < d.donations.length; i++) {
                    var x = d.donations[i];
                    var date = new Date(x.created_at).toLocaleDateString("fr-FR");
                    var donorName = x.donor_name || x.player_id;
                    var receiverName = x.target_name || x.target_player_id;
                    html += "<tr>";
                    html += "<td>#" + x.id + "</td>";
                    html += "<td>" + donorName + "</td>";
                    html += "<td>" + receiverName + "</td>";
                    html += "<td>" + x.amount_robux + "R$</td>";
                    html += "<td>" + x.final_amount + "R$</td>";
                    html += "<td><span class='badge badge-" + x.status + "'>" + x.status + "</span></td>";
                    html += "<td>" + date + "</td>";
                    html += "<td><div class='actions'>";
                    html += "<button class='btn-success' data-id='" + x.id + "' onclick='setStatus(this)'>OK</button>";
                    html += "<button class='btn-danger' data-id='" + x.id + "' onclick='delDonation(this)'>DEL</button>";
                    html += "</div></td>";
                    html += "</tr>";
                }
                tbody.innerHTML = html;
            });
    }
    
    function setStatus(btn) {
        var id = btn.getAttribute("data-id");
        var url = API + "/admin/donations/" + id + "/status?password=" + PASSWORD;
        fetch(url, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: "{\"status\":\"completed\"}"
        }).then(function() {
            showToast("Updated");
            loadData();
            loadStats();
        });
    }
    
    function delDonation(btn) {
        var id = btn.getAttribute("data-id");
        if (!confirm("Delete " + id + "?")) return;
        var url = API + "/admin/donations/" + id + "?password=" + PASSWORD;
        fetch(url, {method: "DELETE"})
            .then(function() {
                showToast("Deleted");
                loadData();
                loadStats();
            });
    }
    
    function deleteAllPending() {
        if (!confirm("Delete ALL pending?")) return;
        var url = API + "/admin/cleanup?password=" + PASSWORD;
        fetch(url, {method: "POST"})
            .then(function(r) { return r.json(); })
            .then(function(d) {
                showToast(d.deleted + " deleted");
                loadData();
                loadStats();
            });
    }
    
    function markAllCompleted() {
        if (!confirm("Mark ALL as completed?")) return;
        var url = API + "/admin/mark-completed?password=" + PASSWORD;
        fetch(url, {method: "POST"})
            .then(function(r) { return r.json(); })
            .then(function(d) {
                showToast(d.updated + " marked");
                loadData();
                loadStats();
            });
    }
    
    document.getElementById("refreshBtn").onclick = loadData;
    document.getElementById("completedBtn").onclick = markAllCompleted;
    document.getElementById("deleteBtn").onclick = deleteAllPending;
    
    loadStats();
    loadData();
    setInterval(function() { loadStats(); loadData(); }, 20000);
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
