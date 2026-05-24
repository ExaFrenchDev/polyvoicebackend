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

# ✅ FIX: Persistent disk path for Render
# On Render, mount a Persistent Disk at /data
# In your Render dashboard: Settings > Disks > Add Disk > Mount Path: /data
DATABASE_PATH = os.getenv("DATABASE_PATH", "/data/donations.db")

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
    # Ensure the directory exists (for /data on Render)
    db_dir = os.path.dirname(DATABASE_PATH)
    if db_dir and not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except Exception:
            pass  # fallback to local if /data not available

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
    try:
        c.execute('ALTER TABLE donations ADD COLUMN discord_message_id TEXT')
        logger.info("✅ Colonne discord_message_id ajoutée")
    except Exception:
        pass
    conn.commit()
    conn.close()
    logger.info(f"✅ Database initialisée: {DATABASE_PATH}")


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
            {"name": f"🏦 Taxe Roblox (-{TAX_RATE * 100}%)", "value": f"`{taxes} Robux`",    "inline": True},
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
    if not DISCORD_WEBHOOK_URL or not message_id:
        return

    proof_str = json.dumps(roblox_proof, indent=2, ensure_ascii=False)
    if len(proof_str) > 950:
        proof_str = proof_str[:950] + "\n... (tronqué)"

    embed = {
        "title": "✅ Donation transférée avec succès!",
        "color": 0x00FF7F,
        "fields": [
            {"name": "👤 Donateur",            "value": f"`{donor_name}` (ID: `{donor_id}`)",   "inline": False},
            {"name": "🎯 Receveur",             "value": f"`{target_name}` (ID: `{target_id}`)", "inline": False},
            {"name": "💵 Montant brut",         "value": f"`{amount} Robux`",                    "inline": True},
            {"name": f"🏦 Taxe Roblox (-{TAX_RATE * 100}%)", "value": f"`{taxes} Robux`",       "inline": True},
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
# ADMIN DASHBOARD — rebuilt from scratch, no ES6 destructuring
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
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 14px;
    min-height: 100vh;
  }

  /* GRID BACKGROUND */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background-image:
      linear-gradient(var(--border) 1px, transparent 1px),
      linear-gradient(90deg, var(--border) 1px, transparent 1px);
    background-size: 40px 40px;
    opacity: 0.4;
    pointer-events: none;
    z-index: 0;
  }

  .wrap { position: relative; z-index: 1; max-width: 1300px; margin: 0 auto; padding: 28px 24px; }

  /* HEADER */
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 32px;
    padding-bottom: 20px;
    border-bottom: 1px solid var(--border);
  }
  .logo {
    display: flex; align-items: center; gap: 12px;
  }
  .logo-dot {
    width: 10px; height: 10px;
    background: var(--accent);
    border-radius: 50%;
    box-shadow: 0 0 10px var(--accent);
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%,100% { opacity:1; } 50% { opacity:0.4; }
  }
  .logo-title {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 16px;
    font-weight: 600;
    letter-spacing: 0.05em;
    color: var(--text);
  }
  .logo-sub {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    color: var(--muted);
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }
  .topbar-right {
    display: flex; align-items: center; gap: 20px;
  }
  .live-badge {
    display: flex; align-items: center; gap: 6px;
    background: var(--green-bg);
    border: 1px solid rgba(34,211,160,0.2);
    padding: 5px 12px;
    border-radius: 20px;
    font-size: 11px;
    color: var(--green);
    font-family: 'IBM Plex Mono', monospace;
  }
  .live-dot {
    width: 6px; height: 6px;
    background: var(--green);
    border-radius: 50%;
    animation: pulse 1.5s infinite;
  }
  .clock {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
    color: var(--muted);
  }

  /* STATS */
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
    margin-bottom: 28px;
  }
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.2s;
  }
  .stat-card:hover { border-color: var(--border-hi); }
  .stat-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
  }
  .stat-card.s-total::before { background: linear-gradient(90deg, var(--accent), transparent); }
  .stat-card.s-pending::before { background: linear-gradient(90deg, var(--yellow), transparent); }
  .stat-card.s-completed::before { background: linear-gradient(90deg, var(--green), transparent); }
  .stat-card.s-failed::before { background: linear-gradient(90deg, var(--red), transparent); }
  .stat-label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--muted);
    margin-bottom: 12px;
    font-family: 'IBM Plex Mono', monospace;
  }
  .stat-value {
    font-size: 38px;
    font-weight: 600;
    font-family: 'IBM Plex Mono', monospace;
    line-height: 1;
  }
  .s-total .stat-value { color: var(--accent); }
  .s-pending .stat-value { color: var(--yellow); }
  .s-completed .stat-value { color: var(--green); }
  .s-failed .stat-value { color: var(--red); }

  /* TOOLBAR */
  .toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
  }
  .toolbar-left {
    display: flex; align-items: center; gap: 12px;
  }
  .section-title {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
    font-weight: 500;
    color: var(--text);
    letter-spacing: 0.05em;
  }
  .count-badge {
    background: var(--accent-glow);
    border: 1px solid rgba(124,106,247,0.3);
    color: var(--accent);
    font-size: 11px;
    font-family: 'IBM Plex Mono', monospace;
    padding: 2px 8px;
    border-radius: 4px;
  }
  .btn-group { display: flex; gap: 8px; }
  .btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 7px 14px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 500;
    font-family: 'IBM Plex Sans', sans-serif;
    cursor: pointer;
    border: 1px solid transparent;
    transition: all 0.15s;
  }
  .btn-ghost {
    background: transparent;
    border-color: var(--border-hi);
    color: var(--muted);
  }
  .btn-ghost:hover { border-color: var(--text); color: var(--text); }
  .btn-green {
    background: var(--green-bg);
    border-color: rgba(34,211,160,0.3);
    color: var(--green);
  }
  .btn-green:hover { background: rgba(34,211,160,0.15); }
  .btn-red {
    background: var(--red-bg);
    border-color: rgba(240,90,90,0.3);
    color: var(--red);
  }
  .btn-red:hover { background: rgba(240,90,90,0.15); }
  .btn-icon { font-size: 14px; line-height: 1; }

  /* TABLE */
  .table-wrap {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
  }
  table { width: 100%; border-collapse: collapse; }
  thead tr {
    border-bottom: 1px solid var(--border);
    background: rgba(255,255,255,0.02);
  }
  th {
    padding: 11px 16px;
    text-align: left;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--muted);
    font-family: 'IBM Plex Mono', monospace;
    font-weight: 500;
  }
  tbody tr {
    border-bottom: 1px solid var(--border);
    transition: background 0.1s;
  }
  tbody tr:last-child { border-bottom: none; }
  tbody tr:hover { background: rgba(255,255,255,0.025); }
  td { padding: 13px 16px; }

  .id-cell {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    color: var(--muted);
  }
  .name-cell { font-weight: 500; }
  .amount-cell {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
  }
  .amount-gross { color: var(--muted); }
  .amount-net { color: var(--green); font-weight: 500; }
  .date-cell {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    color: var(--muted);
  }

  /* BADGES */
  .badge {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 3px 10px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 500;
    font-family: 'IBM Plex Mono', monospace;
    letter-spacing: 0.04em;
  }
  .badge::before { content: ''; width: 5px; height: 5px; border-radius: 50%; }
  .badge-pending {
    background: var(--yellow-bg);
    color: var(--yellow);
    border: 1px solid rgba(245,200,66,0.2);
  }
  .badge-pending::before { background: var(--yellow); }
  .badge-completed {
    background: var(--green-bg);
    color: var(--green);
    border: 1px solid rgba(34,211,160,0.2);
  }
  .badge-completed::before { background: var(--green); }
  .badge-failed {
    background: var(--red-bg);
    color: var(--red);
    border: 1px solid rgba(240,90,90,0.2);
  }
  .badge-failed::before { background: var(--red); }
  .badge-requeue {
    background: rgba(96,165,250,0.08);
    color: var(--blue);
    border: 1px solid rgba(96,165,250,0.2);
  }
  .badge-requeue::before { background: var(--blue); }

  /* ROW ACTIONS */
  .row-actions { display: flex; gap: 6px; }
  .row-btn {
    padding: 4px 10px;
    border-radius: 4px;
    font-size: 11px;
    font-family: 'IBM Plex Mono', monospace;
    cursor: pointer;
    border: 1px solid transparent;
    transition: all 0.12s;
    font-weight: 500;
  }
  .row-btn-ok {
    background: var(--green-bg);
    border-color: rgba(34,211,160,0.25);
    color: var(--green);
  }
  .row-btn-ok:hover { background: rgba(34,211,160,0.18); }
  .row-btn-del {
    background: var(--red-bg);
    border-color: rgba(240,90,90,0.25);
    color: var(--red);
  }
  .row-btn-del:hover { background: rgba(240,90,90,0.18); }

  /* EMPTY / LOADING */
  .empty-state {
    text-align: center;
    padding: 60px 20px;
    color: var(--muted);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
  }
  .loading-state {
    text-align: center;
    padding: 40px;
    color: var(--muted);
  }
  .spinner {
    display: inline-block;
    width: 18px; height: 18px;
    border: 2px solid var(--border-hi);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
    vertical-align: middle;
    margin-right: 8px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* TOAST */
  .toast {
    position: fixed;
    bottom: 24px; right: 24px;
    padding: 12px 20px;
    border-radius: 8px;
    font-size: 13px;
    font-family: 'IBM Plex Mono', monospace;
    font-weight: 500;
    pointer-events: none;
    z-index: 9999;
    opacity: 0;
    transform: translateY(8px);
    transition: all 0.2s;
  }
  .toast.show {
    opacity: 1;
    transform: translateY(0);
  }
  .toast-success {
    background: var(--green-bg);
    border: 1px solid rgba(34,211,160,0.3);
    color: var(--green);
  }
  .toast-error {
    background: var(--red-bg);
    border: 1px solid rgba(240,90,90,0.3);
    color: var(--red);
  }

  /* DB PATH NOTICE */
  .db-notice {
    display: flex; align-items: center; gap: 10px;
    background: rgba(124,106,247,0.06);
    border: 1px solid rgba(124,106,247,0.2);
    border-radius: 8px;
    padding: 12px 16px;
    margin-bottom: 24px;
    font-size: 12px;
    font-family: 'IBM Plex Mono', monospace;
    color: var(--muted);
  }
  .db-notice strong { color: var(--accent); }

  @media (max-width: 900px) {
    .stats-grid { grid-template-columns: repeat(2,1fr); }
  }
  @media (max-width: 600px) {
    .stats-grid { grid-template-columns: 1fr 1fr; }
    .topbar { flex-direction: column; gap: 12px; align-items: flex-start; }
  }
</style>
</head>
<body>
<div class="wrap">

  <!-- TOPBAR -->
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

  <!-- DB PATH NOTICE -->
  <div class="db-notice" id="dbNotice">
    <span>📂</span>
    <span>Base de données : <strong id="dbPath">chargement...</strong></span>
  </div>

  <!-- STATS -->
  <div class="stats-grid" id="statsGrid">
    <div class="stat-card s-total"><div class="stat-label">Total</div><div class="stat-value" id="s-total">—</div></div>
    <div class="stat-card s-pending"><div class="stat-label">En attente</div><div class="stat-value" id="s-pending">—</div></div>
    <div class="stat-card s-completed"><div class="stat-label">Complétées</div><div class="stat-value" id="s-completed">—</div></div>
    <div class="stat-card s-failed"><div class="stat-label">Échouées</div><div class="stat-value" id="s-failed">—</div></div>
  </div>

  <!-- TOOLBAR -->
  <div class="toolbar">
    <div class="toolbar-left">
      <span class="section-title">DONATIONS</span>
      <span class="count-badge" id="countBadge">0</span>
    </div>
    <div class="btn-group">
      <button class="btn btn-ghost" id="refreshBtn"><span class="btn-icon">↻</span> Refresh</button>
      <button class="btn btn-green" id="completeAllBtn"><span class="btn-icon">✓</span> Tout compléter</button>
      <button class="btn btn-red" id="deletePendingBtn"><span class="btn-icon">✕</span> Suppr. pending</button>
    </div>
  </div>

  <!-- TABLE -->
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>#ID</th>
          <th>Donateur</th>
          <th>Receveur</th>
          <th>Brut</th>
          <th>Net</th>
          <th>Statut</th>
          <th>Date</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="tableBody">
        <tr><td colspan="8" class="loading-state"><span class="spinner"></span>Chargement...</td></tr>
      </tbody>
    </table>
  </div>

</div>

<!-- TOAST -->
<div class="toast" id="toast"></div>

<script>
(function() {
  var BASE = window.location.origin;
  var PWD = (function() {
    var search = window.location.search.substring(1);
    var pairs = search.split('&');
    for (var i = 0; i < pairs.length; i++) {
      var kv = pairs[i].split('=');
      if (kv[0] === 'password') return decodeURIComponent(kv[1] || '');
    }
    return '';
  })();

  /* ---- CLOCK ---- */
  function tickClock() {
    var now = new Date();
    var h = now.getHours().toString().padStart(2,'0');
    var m = now.getMinutes().toString().padStart(2,'0');
    var s = now.getSeconds().toString().padStart(2,'0');
    document.getElementById('clock').textContent = h + ':' + m + ':' + s;
  }
  setInterval(tickClock, 1000);
  tickClock();

  /* ---- TOAST ---- */
  var toastTimer = null;
  function showToast(msg, isError) {
    var el = document.getElementById('toast');
    el.textContent = msg;
    el.className = 'toast show ' + (isError ? 'toast-error' : 'toast-success');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function() { el.className = 'toast'; }, 3000);
  }

  /* ---- BADGE CLASS ---- */
  function badgeClass(st) {
    if (st === 'pending') return 'badge-pending';
    if (st === 'completed') return 'badge-completed';
    if (st === 'failed') return 'badge-failed';
    if (st === 'requeue') return 'badge-requeue';
    return 'badge-pending';
  }

  /* ---- LOAD STATS ---- */
  function loadStats() {
    fetch(BASE + '/admin/stats?password=' + PWD)
      .then(function(r) { return r.json(); })
      .then(function(d) {
        document.getElementById('s-total').textContent    = d.total     !== undefined ? d.total     : '?';
        document.getElementById('s-pending').textContent  = d.pending   !== undefined ? d.pending   : '?';
        document.getElementById('s-completed').textContent= d.completed !== undefined ? d.completed : '?';
        document.getElementById('s-failed').textContent   = d.failed    !== undefined ? d.failed    : '?';
        if (d.db_path) {
          document.getElementById('dbPath').textContent = d.db_path;
        }
      })
      .catch(function(e) { console.error('Stats error:', e); });
  }

  /* ---- FORMAT DATE ---- */
  function fmtDate(str) {
    if (!str) return '—';
    var d = new Date(str);
    if (isNaN(d.getTime())) return str;
    var day = d.getDate().toString().padStart(2,'0');
    var mo  = (d.getMonth()+1).toString().padStart(2,'0');
    var yr  = d.getFullYear();
    var hr  = d.getHours().toString().padStart(2,'0');
    var min = d.getMinutes().toString().padStart(2,'0');
    return day+'/'+mo+'/'+yr+' '+hr+':'+min;
  }

  /* ---- LOAD TABLE ---- */
  function loadTable() {
    fetch(BASE + '/admin/donations?password=' + PWD)
      .then(function(r) { return r.json(); })
      .then(function(d) {
        var tbody = document.getElementById('tableBody');
        var list = d.donations;
        document.getElementById('countBadge').textContent = list ? list.length : 0;
        if (!list || list.length === 0) {
          tbody.innerHTML = '<tr><td colspan="8" class="empty-state">/ / Aucune donation trouvée / /</td></tr>';
          return;
        }
        var rows = '';
        for (var i = 0; i < list.length; i++) {
          var item = list[i];
          var donorDisplay    = item.donor_name  ? item.donor_name  : String(item.player_id);
          var receiverDisplay = item.target_name ? item.target_name : String(item.target_player_id);
          var itemStatus      = item.status || 'pending';
          var itemId          = item.id;
          rows += '<tr>';
          rows += '<td class="id-cell">#' + itemId + '</td>';
          rows += '<td class="name-cell">' + donorDisplay + '</td>';
          rows += '<td class="name-cell">' + receiverDisplay + '</td>';
          rows += '<td class="amount-cell amount-gross">' + item.amount_robux + ' R$</td>';
          rows += '<td class="amount-cell amount-net">' + item.final_amount + ' R$</td>';
          rows += '<td><span class="badge ' + badgeClass(itemStatus) + '">' + itemStatus + '</span></td>';
          rows += '<td class="date-cell">' + fmtDate(item.created_at) + '</td>';
          rows += '<td><div class="row-actions">';
          rows += '<button class="row-btn row-btn-ok" data-id="' + itemId + '" onclick="markOK(this)">✓ OK</button>';
          rows += '<button class="row-btn row-btn-del" data-id="' + itemId + '" onclick="delRow(this)">✕ DEL</button>';
          rows += '</div></td>';
          rows += '</tr>';
        }
        tbody.innerHTML = rows;
      })
      .catch(function(e) {
        console.error('Table error:', e);
        document.getElementById('tableBody').innerHTML =
          '<tr><td colspan="8" class="empty-state">Erreur de chargement</td></tr>';
      });
  }

  /* ---- ROW ACTIONS (global scope for onclick) ---- */
  window.markOK = function(btn) {
    var id = btn.getAttribute('data-id');
    fetch(BASE + '/admin/donations/' + id + '/status?password=' + PWD, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{"status":"completed"}'
    })
    .then(function(r) { return r.json(); })
    .then(function() { showToast('Donation #' + id + ' complétée'); loadTable(); loadStats(); })
    .catch(function() { showToast('Erreur', true); });
  };

  window.delRow = function(btn) {
    var id = btn.getAttribute('data-id');
    if (!confirm('Supprimer la donation #' + id + ' ?')) return;
    fetch(BASE + '/admin/donations/' + id + '?password=' + PWD, { method: 'DELETE' })
    .then(function(r) { return r.json(); })
    .then(function() { showToast('Donation #' + id + ' supprimée'); loadTable(); loadStats(); })
    .catch(function() { showToast('Erreur', true); });
  };

  /* ---- TOOLBAR BUTTONS ---- */
  document.getElementById('refreshBtn').onclick = function() {
    loadStats(); loadTable(); showToast('Données rechargées');
  };

  document.getElementById('completeAllBtn').onclick = function() {
    if (!confirm('Marquer TOUTES les donations pending comme completed ?')) return;
    fetch(BASE + '/admin/mark-completed?password=' + PWD, { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(d) { showToast(d.updated + ' donation(s) complétée(s)'); loadTable(); loadStats(); })
    .catch(function() { showToast('Erreur', true); });
  };

  document.getElementById('deletePendingBtn').onclick = function() {
    if (!confirm('Supprimer TOUTES les donations pending ?')) return;
    fetch(BASE + '/admin/cleanup?password=' + PWD, { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(d) { showToast(d.deleted + ' donation(s) supprimée(s)'); loadTable(); loadStats(); })
    .catch(function() { showToast('Erreur', true); });
  };

  /* ---- INIT ---- */
  loadStats();
  loadTable();
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
            "total": total, "pending": pending,
            "completed": completed, "failed": failed,
            "db_path": DATABASE_PATH  # ✅ surface the DB path in the dashboard
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
        conn.close()

        donations = []
        for row in rows:
            donor_info  = get_user_info(row[1])
            target_info = get_user_info(row[2])
            donations.append({
                "id": row[0],
                "player_id": row[1],
                "donor_name":  donor_info.get("name")  if donor_info  else None,
                "target_player_id": row[2],
                "target_name": target_info.get("name") if target_info else None,
                "amount_robux": row[3],
                "final_amount": row[4],
                "status": row[5],
                "created_at": row[6]
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
        if new_status not in ("pending", "completed", "failed"):
            return jsonify({"error": "Invalid status"}), 400
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE donations SET status = ? WHERE id = ?', (new_status, donation_id))
        conn.commit()
        conn.close()
        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/donations/<int:donation_id>', methods=['DELETE'])
def admin_delete_donation(donation_id):
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('DELETE FROM donations WHERE id = ?', (donation_id,))
        conn.commit()
        conn.close()
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
        c.execute("SELECT COUNT(*) FROM donations WHERE status = 'pending'")
        cnt = c.fetchone()[0]
        c.execute("DELETE FROM donations WHERE status = 'pending'")
        conn.commit()
        conn.close()
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
        conn.close()
        return jsonify({"success": True, "updated": updated}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================================
# STARTUP
# ============================================================================

init_db()
logger.info("✅ API Donation en cours de démarrage...")
logger.info(f"🗄️ Database path: {DATABASE_PATH}")
logger.info(f"🔧 DevProducts configurés: {list(DEVPRODUCT_AMOUNTS.keys())}")
logger.info(f"🔔 Discord webhook: {'configuré' if DISCORD_WEBHOOK_URL else 'NON configuré'}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False, use_reloader=False)
