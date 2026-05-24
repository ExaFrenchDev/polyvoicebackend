from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import os
import sqlite3
import hashlib
import hmac
import asyncio
import aiohttp
from dotenv import load_dotenv
import discord
from discord.ext import tasks
import logging
import requests
import json
from http.cookies import SimpleCookie

load_dotenv()

app = Flask(__name__)

# Configuration
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))
ROBLOX_GAME_ID = int(os.getenv("ROBLOX_GAME_ID"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # Pour vérifier les appels webhook
GROUP_ID = int(os.getenv("GROUP_ID"))
ACCOUNT_COOKIE = os.getenv("ROBLOX_ACCOUNT_COOKIE")  # Cookie du compte pour transférer les Robux
DATABASE_PATH = "donations.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# DevProduct IDs mapping (à configurer)
DEVPRODUCT_AMOUNTS = {
    "123456": 5,      # DevProduct ID: 123456 = 5 Robux donation
    "123457": 10,
    "123458": 50,
    "123459": 100,
    "123460": 500,
    "123461": 1000,
    "123462": 5000,
    "123463": 10000,
}

TAX_RATE = 0.40  # 40% taxes Roblox
WAIT_DAYS = 7  # Délai avant transfert
MIN_GROUP_TENURE_DAYS = 7  # Durée minimale dans le groupe


# ============================================================================
# DATABASE SETUP
# ============================================================================

def init_db():
    """Initialiser la base de données SQLite"""
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
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS group_members (
            user_id INTEGER PRIMARY KEY,
            join_date TIMESTAMP,
            last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()


def get_db():
    """Obtenir une connexion à la base de données"""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ============================================================================
# ROBLOX API FUNCTIONS
# ============================================================================

def verify_webhook_signature(data, signature):
    """Vérifier la signature du webhook Roblox"""
    expected_sig = hmac.new(
        WEBHOOK_SECRET.encode(),
        data,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected_sig)


def get_user_info(user_id):
    """Récupérer les infos utilisateur Roblox via l'API"""
    try:
        resp = requests.get(f"https://users.roblox.com/v1/users/{user_id}")
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error(f"Erreur récupération user {user_id}: {e}")
    return None


def get_group_membership(user_id, group_id):
    """Vérifier si l'utilisateur est dans le groupe et depuis quand"""
    try:
        resp = requests.get(f"https://groups.roblox.com/v1/users/{user_id}/groups")
        if resp.status_code == 200:
            data = resp.json()
            for group in data.get("data", []):
                if group["group"]["id"] == group_id:
                    return {
                        "in_group": True,
                        "join_date": group.get("joinedDate"),  # ISO format
                        "role": group.get("role", {}).get("name")
                    }
            return {"in_group": False}
    except Exception as e:
        logger.error(f"Erreur vérification groupe pour {user_id}: {e}")
    return {"in_group": False}


def check_eligibility(user_id, group_id, days_required=MIN_GROUP_TENURE_DAYS):
    """Vérifier si un joueur est éligible pour le transfert"""
    membership = get_group_membership(user_id, group_id)
    
    if not membership.get("in_group"):
        return {"eligible": False, "reason": "Not in group"}
    
    # Vérifier la durée dans le groupe
    if membership.get("join_date"):
        join_date = datetime.fromisoformat(membership["join_date"].replace("Z", "+00:00"))
        days_in_group = (datetime.now(join_date.tzinfo) - join_date).days
        
        if days_in_group < days_required:
            return {
                "eligible": False,
                "reason": f"Not long enough in group ({days_in_group}/{days_required} days)"
            }
    
    return {"eligible": True, "reason": "Meets all requirements"}


def transfer_robux(target_user_id, amount_robux):
    """Transférer les Robux au joueur via le compte bot (via le cookie)"""
    try:
        # Setup session avec le cookie du compte bot
        session = requests.Session()
        session.cookies.set('.ROBLOSECURITY', ACCOUNT_COOKIE)
        
        # Headers nécessaires
        headers = {
            'X-CSRF-TOKEN': '',
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        }
        
        # Étape 1: Récupérer le X-CSRF-TOKEN
        csrf_response = session.post('https://www.roblox.com/home', headers=headers)
        if 'X-CSRF-TOKEN' in csrf_response.headers:
            headers['X-CSRF-TOKEN'] = csrf_response.headers['X-CSRF-TOKEN']
        
        # Étape 2: Transférer les Robux via l'endpoint de transfert
        # (Endpoint privé Roblox - utilise l'API de transfer Robux)
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
            logger.error(f"❌ Erreur HTTP {transfer_response.status_code}: {transfer_response.text}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Erreur transfert Robux: {e}")
        return False


# ============================================================================
# DISCORD BOT
# ============================================================================

class DonationBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.channel_id = DISCORD_CHANNEL_ID
    
    async def on_ready(self):
        logger.info(f"Discord bot connecté: {self.user}")
        self.check_donations.start()
    
    async def send_donation_notification(self, player_name, target_name, amount, final_amount, taxes):
        """Envoyer une notification Discord pour une nouvelle donation"""
        channel = self.get_channel(DISCORD_CHANNEL_ID)
        if not channel:
            logger.error("Canal Discord non trouvé")
            return
        
        embed = discord.Embed(
            title="💰 Nouvelle Donation!",
            color=discord.Color.gold(),
            timestamp=datetime.now()
        )
        embed.add_field(name="Donateur", value=f"```{player_name}```", inline=False)
        embed.add_field(name="Pour", value=f"```{target_name}```", inline=True)
        embed.add_field(name="Montant", value=f"```{amount} Robux```", inline=True)
        embed.add_field(name="Après taxes (-40%)", value=f"```{final_amount} Robux```", inline=True)
        embed.add_field(name="Taxes Roblox", value=f"```{taxes} Robux```", inline=False)
        embed.set_footer(text=f"ID de la donation: {datetime.now().timestamp()}")
        
        await channel.send(embed=embed)
    
    @tasks.loop(minutes=30)  # Checker toutes les 30 minutes
    async def check_donations(self):
        """Vérifier les donations en attente et traiter les transfers"""
        logger.info("Vérification des donations en attente...")
        
        conn = get_db()
        c = conn.cursor()
        
        # Récupérer les donations qui ont attendu 5-7 jours
        cutoff_date = datetime.now() - timedelta(days=WAIT_DAYS)
        
        c.execute('''
            SELECT * FROM donations 
            WHERE status = 'pending' AND created_at <= ? AND retry_count < 5
            ORDER BY created_at ASC
        ''', (cutoff_date,))
        
        donations = c.fetchall()
        
        for donation in donations:
            user_id = donation['target_player_id']
            amount = donation['final_amount']
            
            # Vérifier l'éligibilité
            eligibility = check_eligibility(user_id, GROUP_ID)
            
            if eligibility["eligible"]:
                # Transférer les Robux
                success = transfer_robux(user_id, amount)
                
                if success:
                    c.execute('''
                        UPDATE donations 
                        SET status = 'completed', processed_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    ''', (donation['id'],))
                    
                    logger.info(f"Transfert réussi pour donation {donation['id']}")
                    
                    # Notifier le joueur sur Discord si besoin
                    channel = self.get_channel(DISCORD_CHANNEL_ID)
                    if channel:
                        await channel.send(
                            f"✅ Transfert complété pour {user_id}: {amount} Robux"
                        )
                else:
                    # Réessayer plus tard
                    c.execute('''
                        UPDATE donations 
                        SET retry_count = retry_count + 1, last_retry = CURRENT_TIMESTAMP
                        WHERE id = ?
                    ''', (donation['id'],))
            else:
                # Rejeter et re-queue
                c.execute('''
                    UPDATE donations 
                    SET status = 'requeue', retry_count = retry_count + 1, last_retry = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (donation['id'],))
                
                logger.info(f"Donation {donation['id']} re-queued: {eligibility['reason']}")
        
        conn.commit()
        conn.close()


# Initialiser le bot Discord
intents = discord.Intents.default()
bot = DonationBot(intents=intents)


# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/webhook/devproduct', methods=['POST'])
def handle_devproduct_purchase():
    """
    Webhook Roblox quand un joueur achète un DevProduct
    Payload attendu:
    {
        "userId": 12345,
        "targetUserId": 54321,
        "devProductId": "123456",
        "transactionId": "xxx"
    }
    """
    
    # Vérifier la signature du webhook (optionnel mais recommandé)
    signature = request.headers.get('X-Roblox-Signature')
    if signature and not verify_webhook_signature(request.data, signature):
        logger.warning("Signature webhook invalide")
        return jsonify({"error": "Invalid signature"}), 401
    
    data = request.get_json()
    
    user_id = data.get("userId")
    target_user_id = data.get("targetUserId")
    devproduct_id = str(data.get("devProductId"))
    
    if not all([user_id, target_user_id, devproduct_id]):
        return jsonify({"error": "Missing fields"}), 400
    
    if devproduct_id not in DEVPRODUCT_AMOUNTS:
        logger.warning(f"DevProduct inconnu: {devproduct_id}")
        return jsonify({"error": "Unknown DevProduct"}), 400
    
    amount = DEVPRODUCT_AMOUNTS[devproduct_id]
    final_amount = int(amount * (1 - TAX_RATE))
    taxes = amount - final_amount
    
    # Stocker dans la DB
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
    
    # Récupérer les noms des joueurs pour la notification Discord
    player_info = get_user_info(user_id)
    target_info = get_user_info(target_user_id)
    
    player_name = player_info.get("name", f"User {user_id}") if player_info else f"User {user_id}"
    target_name = target_info.get("name", f"User {target_user_id}") if target_info else f"User {target_user_id}"
    
    # Envoyer la notification Discord
    asyncio.run_coroutine_threadsafe(
        bot.send_donation_notification(player_name, target_name, amount, final_amount, taxes),
        bot.loop
    )
    
    logger.info(f"Donation {donation_id} reçue: {player_name} -> {target_name} ({amount} Robux, {final_amount} après taxes)")
    
    return jsonify({
        "success": True,
        "donationId": donation_id,
        "message": f"Donation enregistrée: {final_amount} Robux seront transférés",
        "estimatedDate": (datetime.now() + timedelta(days=WAIT_DAYS)).isoformat()
    }), 200


@app.route('/status/<int:donation_id>', methods=['GET'])
def get_donation_status(donation_id):
    """Obtenir le statut d'une donation"""
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


@app.route('/health', methods=['GET'])
def health_check():
    """Health check pour monitoring"""
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now().isoformat()
    }), 200


@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "service": "Roblox Donation Backend",
        "status": "running",
        "endpoints": {
            "webhook": "/webhook/devproduct (POST)",
            "status": "/status/<donation_id> (GET)",
            "health": "/health (GET)"
        }
    }), 200


# ============================================================================
# STARTUP
# ============================================================================

if __name__ == '__main__':
    init_db()
    
    # Démarrer le bot Discord dans un thread séparé
    import threading
    
    def run_bot():
        bot.run(DISCORD_TOKEN)
    
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Démarrer le serveur Flask
    app.run(
        host='0.0.0.0',
        port=int(os.getenv('PORT', 5000)),
        debug=os.getenv('FLASK_DEBUG', False)
    )
