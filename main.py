from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import os
import sqlite3
import hashlib
import hmac
import logging
import requests
import math
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Configuration
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
GROUP_ID = int(os.getenv("GROUP_ID", "0"))
ACCOUNT_COOKIE = os.getenv("ROBLOX_ACCOUNT_COOKIE", "")
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
            last_retry TIMESTAMP
        )
    ''')
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
    expected_sig = hmac.new(
        WEBHOOK_SECRET.encode(),
        data,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected_sig)


def get_user_info(user_id):
    try:
        resp = requests.get(
            f"https://users.roblox.com/v1/users/{user_id}",
            timeout=3
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error(f"Erreur récupération user {user_id}: {e}")
    return None


def get_group_membership(user_id, group_id):
    try:
        resp = requests.get(
            f"https://groups.roblox.com/v1/users/{user_id}/groups",
            timeout=3
        )
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
                return {
                    "eligible": False,
                    "reason": f"Not long enough in group ({days_in_group}/{days_required} days)"
                }
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
        csrf_response = session.post(
            'https://www.roblox.com/home',
            headers=headers,
            timeout=5
        )
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
        if transfer_response.status_code == 200:
            result = transfer_response.json()
            if result.get('success'):
                logger.info(f"✅ Transfert réussi: {amount_robux} Robux à {target_user_id}")
                return True
            else:
                logger.error(f"❌ Erreur transfert: {result.get('message')}")
                return False
        else:
            logger.error(f"❌ Erreur HTTP {transfer_response.status_code}")
            return False
    except Exception as e:
        logger.error(f"❌ Erreur transfert Robux: {e}")
        return False


# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/webhook/devproduct', methods=['POST'])
def handle_devproduct_purchase():
    try:
        signature = request.headers.get('X-Roblox-Signature')
        if signature and not verify_webhook_signature(request.data, signature):
            logger.warning("Signature webhook invalide")
            return jsonify({"error": "Invalid signature"}), 401

        data = request.get_json()
        user_id = data.get("userId")
        target_user_id = data.get("targetUserId")
        devproduct_id = str(data.get("devProductId"))
        transaction_id = data.get("transactionId", "unknown")

        if not all([user_id, target_user_id, devproduct_id]):
            return jsonify({"error": "Missing fields"}), 400

        if devproduct_id not in DEVPRODUCT_AMOUNTS:
            logger.warning(f"DevProduct inconnu: {devproduct_id}")
            return jsonify({"error": "Unknown DevProduct"}), 400

        amount = DEVPRODUCT_AMOUNTS[devproduct_id]
        final_amount = math.ceil(amount * (1 - TAX_RATE))

        conn = get_db()
        c = conn.cursor()
        c.execute('''
            INSERT INTO donations 
            (player_id, target_player_id, devproduct_id, amount_robux, final_amount, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
        ''', (user_id, target_user_id, devproduct_id, amount, final_amount))
        conn.commit()
        donation_id = c.lastrowid
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


# ============================================================================
# ✅ NOUVEAU: Endpoint pour que le bot récupère les donations en attente
# ============================================================================

@app.route('/pending', methods=['GET'])
def get_pending_donations():
    """Donations en attente depuis plus de WAIT_DAYS jours — appelé par le bot Discord"""
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


# ============================================================================
# ✅ NOUVEAU: Endpoint pour que le bot mette à jour le statut d'une donation
# ============================================================================

@app.route('/donations/<int:donation_id>/status', methods=['PATCH'])
def update_donation_status(donation_id):
    """Met à jour le statut d'une donation — appelé par le bot Discord"""
    try:
        data = request.get_json()
        new_status = data.get("status")

        if new_status not in ("completed", "pending", "requeue", "failed"):
            return jsonify({"error": "Invalid status"}), 400

        conn = get_db()
        c = conn.cursor()

        if new_status == "completed":
            c.execute('''
                UPDATE donations 
                SET status = 'completed', processed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (donation_id,))
        else:
            c.execute('''
                UPDATE donations 
                SET status = ?, retry_count = retry_count + 1, last_retry = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (new_status, donation_id))

        conn.commit()
        conn.close()

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
        logger.error(f"Erreur get_donation_status: {e}")
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
                    "id": d['id'],
                    "playerId": d['player_id'],
                    "targetId": d['target_player_id'],
                    "amount": d['amount_robux'],
                    "finalAmount": d['final_amount'],
                    "status": d['status'],
                    "createdAt": d['created_at'],
                    "processedAt": d['processed_at']
                }
                for d in donations
            ]
        }), 200
    except Exception as e:
        logger.error(f"Erreur list_donations: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()}), 200


@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "service": "Roblox Donation Backend",
        "status": "running",
        "endpoints": {
            "webhook": "/webhook/devproduct (POST)",
            "pending": "/pending (GET)",
            "update_status": "/donations/<id>/status (PATCH)",
            "status": "/status/<donation_id> (GET)",
            "list": "/list (GET)",
            "health": "/health (GET)"
        }
    }), 200


# ============================================================================
# STARTUP
# ============================================================================

init_db()
logger.info("✅ API Donation en cours de démarrage...")
logger.info(f"🔧 DevProducts configurés: {list(DEVPRODUCT_AMOUNTS.keys())}")

if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=int(os.getenv('PORT', 5000)),
        debug=False,
        use_reloader=False
    )
