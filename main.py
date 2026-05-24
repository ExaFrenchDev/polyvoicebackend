from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import os
import sqlite3
import hashlib
import hmac
import asyncio
import threading
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
import logging
import requests

load_dotenv()

app = Flask(__name__)

# Configuration
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))
ROBLOX_GAME_ID = int(os.getenv("ROBLOX_GAME_ID"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
GROUP_ID = int(os.getenv("GROUP_ID"))
ACCOUNT_COOKIE = os.getenv("ROBLOX_ACCOUNT_COOKIE")
DATABASE_PATH = "donations.db"
ADMIN_DISCORD_IDS = list(map(int, os.getenv("ADMIN_DISCORD_IDS", "").split(","))) if os.getenv("ADMIN_DISCORD_IDS") else []

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEVPRODUCT_AMOUNTS = {
    "123456": 5,
    "123457": 10,
    "123458": 50,
    "123459": 100,
    "123460": 500,
    "123461": 1000,
    "123462": 5000,
    "123463": 10000,
}

TAX_RATE = 0.40
WAIT_DAYS = 7
MIN_GROUP_TENURE_DAYS = 7


# ============================================================================
# DATABASE
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
# PERMISSIONS
# ============================================================================

def is_admin(user_id):
    """Vérifier si un utilisateur Discord est admin"""
    return user_id in ADMIN_DISCORD_IDS


# ============================================================================
# ROBLOX API
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
    """Récupérer les infos utilisateur Roblox"""
    try:
        resp = requests.get(f"https://users.roblox.com/v1/users/{user_id}")
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error(f"Erreur récupération user {user_id}: {e}")
    return None


def get_group_membership(user_id, group_id):
    """Vérifier si l'utilisateur est dans le groupe"""
    try:
        resp = requests.get(f"https://groups.roblox.com/v1/users/{user_id}/groups")
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
    """Vérifier si un joueur est éligible"""
    membership = get_group_membership(user_id, group_id)
    
    if not membership.get("in_group"):
        return {"eligible": False, "reason": "Not in group"}
    
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
    """Transférer les Robux au joueur"""
    try:
        session = requests.Session()
        session.cookies.set('.ROBLOSECURITY', ACCOUNT_COOKIE)
        
        headers = {
            'X-CSRF-TOKEN': '',
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        }
        
        csrf_response = session.post('https://www.roblox.com/home', headers=headers)
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
            logger.error(f"❌ Erreur HTTP {transfer_response.status_code}: {transfer_response.text}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Erreur transfert Robux: {e}")
        return False


# ============================================================================
# DISCORD BOT
# ============================================================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


class DonationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.channel_id = DISCORD_CHANNEL_ID
        self.check_donations.start()
    
    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"✅ Discord bot connecté: {self.bot.user}")
    
    async def send_donation_notification(self, player_name, target_name, amount, final_amount, taxes):
        """Envoyer une notification Discord"""
        channel = self.bot.get_channel(DISCORD_CHANNEL_ID)
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
        
        await channel.send(embed=embed)
    
    @tasks.loop(minutes=30)
    async def check_donations(self):
        """Vérifier les donations en attente"""
        logger.info("Vérification des donations en attente...")
        
        conn = get_db()
        c = conn.cursor()
        
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
            
            eligibility = check_eligibility(user_id, GROUP_ID)
            
            if eligibility["eligible"]:
                success = transfer_robux(user_id, amount)
                
                if success:
                    c.execute('''
                        UPDATE donations 
                        SET status = 'completed', processed_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    ''', (donation['id'],))
                    
                    logger.info(f"Transfert réussi pour donation {donation['id']}")
                    
                    channel = self.bot.get_channel(DISCORD_CHANNEL_ID)
                    if channel:
                        await channel.send(
                            f"✅ Transfert complété pour {user_id}: {amount} Robux"
                        )
                else:
                    c.execute('''
                        UPDATE donations 
                        SET retry_count = retry_count + 1, last_retry = CURRENT_TIMESTAMP
                        WHERE id = ?
                    ''', (donation['id'],))
            else:
                c.execute('''
                    UPDATE donations 
                    SET status = 'requeue', retry_count = retry_count + 1, last_retry = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (donation['id'],))
                
                logger.info(f"Donation {donation['id']} re-queued: {eligibility['reason']}")
        
        conn.commit()
        conn.close()
    
    # ========================================================================
    # SLASH COMMANDS ADMIN
    # ========================================================================
    
    @discord.app_commands.command(name="donations_stats", description="Voir les statistiques des donations")
    async def donations_stats(self, interaction: discord.Interaction):
        """Afficher les statistiques de donations (ADMIN)"""
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Vous n'avez pas les permissions.", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        conn = get_db()
        c = conn.cursor()
        
        c.execute('SELECT COUNT(*) as total FROM donations')
        total = c.fetchone()['total']
        
        c.execute('SELECT COUNT(*) as pending FROM donations WHERE status = "pending"')
        pending = c.fetchone()['pending']
        
        c.execute('SELECT COUNT(*) as completed FROM donations WHERE status = "completed"')
        completed = c.fetchone()['completed']
        
        c.execute('SELECT SUM(amount_robux) as total_robux FROM donations')
        total_robux = c.fetchone()['total_robux'] or 0
        
        c.execute('SELECT SUM(final_amount) as final_robux FROM donations WHERE status = "completed"')
        final_robux = c.fetchone()['final_robux'] or 0
        
        conn.close()
        
        embed = discord.Embed(
            title="📊 Statistiques Donations",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        embed.add_field(name="Total donations", value=f"```{total}```", inline=True)
        embed.add_field(name="En attente", value=f"```{pending}```", inline=True)
        embed.add_field(name="Complétées", value=f"```{completed}```", inline=True)
        embed.add_field(name="Robux total reçus", value=f"```{total_robux}```", inline=False)
        embed.add_field(name="Robux transférés (net)", value=f"```{final_robux}```", inline=False)
        
        await interaction.followup.send(embed=embed)
    
    @discord.app_commands.command(name="donation_status", description="Voir le statut d'une donation")
    @discord.app_commands.describe(donation_id="ID de la donation")
    async def donation_status(self, interaction: discord.Interaction, donation_id: int):
        """Voir le statut d'une donation (ADMIN)"""
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Vous n'avez pas les permissions.", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        conn = get_db()
        c = conn.cursor()
        
        c.execute('SELECT * FROM donations WHERE id = ?', (donation_id,))
        donation = c.fetchone()
        conn.close()
        
        if not donation:
            await interaction.followup.send("❌ Donation non trouvée")
            return
        
        player_info = get_user_info(donation['player_id'])
        target_info = get_user_info(donation['target_player_id'])
        
        embed = discord.Embed(
            title=f"📋 Donation #{donation_id}",
            color=discord.Color.green() if donation['status'] == 'completed' else discord.Color.orange(),
            timestamp=datetime.now()
        )
        embed.add_field(
            name="Donateur",
            value=f"```{player_info.get('name', 'N/A') if player_info else 'N/A'} (ID: {donation['player_id']})```",
            inline=False
        )
        embed.add_field(
            name="Receveur",
            value=f"```{target_info.get('name', 'N/A') if target_info else 'N/A'} (ID: {donation['target_player_id']})```",
            inline=False
        )
        embed.add_field(name="Montant brut", value=f"```{donation['amount_robux']} Robux```", inline=True)
        embed.add_field(name="Montant net", value=f"```{donation['final_amount']} Robux```", inline=True)
        embed.add_field(name="Statut", value=f"```{donation['status']}```", inline=True)
        embed.add_field(name="Créée le", value=f"```{donation['created_at']}```", inline=False)
        if donation['processed_at']:
            embed.add_field(name="Traitée le", value=f"```{donation['processed_at']}```", inline=False)
        embed.add_field(name="Tentatives", value=f"```{donation['retry_count']}/5```", inline=True)
        
        await interaction.followup.send(embed=embed)
    
    @discord.app_commands.command(name="pending_donations", description="Lister les donations en attente")
    @discord.app_commands.describe(limit="Nombre de donations à afficher (max 10)")
    async def pending_donations(self, interaction: discord.Interaction, limit: int = 5):
        """Lister les donations en attente (ADMIN)"""
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Vous n'avez pas les permissions.", ephemeral=True)
            return
        
        if limit > 10:
            limit = 10
        
        await interaction.response.defer()
        
        conn = get_db()
        c = conn.cursor()
        
        c.execute('''
            SELECT * FROM donations 
            WHERE status = 'pending' 
            ORDER BY created_at DESC 
            LIMIT ?
        ''', (limit,))
        
        donations = c.fetchall()
        conn.close()
        
        if not donations:
            await interaction.followup.send("✅ Aucune donation en attente")
            return
        
        embed = discord.Embed(
            title=f"⏳ Donations en attente ({len(donations)})",
            color=discord.Color.orange(),
            timestamp=datetime.now()
        )
        
        for donation in donations:
            days_left = (datetime.fromisoformat(donation['created_at']) + timedelta(days=WAIT_DAYS) - datetime.now()).days
            embed.add_field(
                name=f"ID #{donation['id']} → Joueur {donation['target_player_id']}",
                value=f"```{donation['final_amount']} Robux | Prêt dans {max(0, days_left)} jours```",
                inline=False
            )
        
        await interaction.followup.send(embed=embed)
    
    @discord.app_commands.command(name="user_donations", description="Voir toutes les donations d'un utilisateur")
    @discord.app_commands.describe(roblox_user_id="ID Roblox de l'utilisateur")
    async def user_donations(self, interaction: discord.Interaction, roblox_user_id: int):
        """Voir toutes les donations d'un utilisateur (ADMIN)"""
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Vous n'avez pas les permissions.", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        conn = get_db()
        c = conn.cursor()
        
        c.execute('''
            SELECT * FROM donations 
            WHERE target_player_id = ? 
            ORDER BY created_at DESC
        ''', (roblox_user_id,))
        
        donations = c.fetchall()
        conn.close()
        
        if not donations:
            await interaction.followup.send(f"❌ Aucune donation pour l'utilisateur {roblox_user_id}")
            return
        
        user_info = get_user_info(roblox_user_id)
        user_name = user_info.get('name', 'N/A') if user_info else 'N/A'
        
        embed = discord.Embed(
            title=f"💝 Donations de {user_name}",
            color=discord.Color.purple(),
            timestamp=datetime.now()
        )
        
        total_robux = 0
        for donation in donations:
            total_robux += donation['final_amount'] or 0
            embed.add_field(
                name=f"ID #{donation['id']} - {donation['status']}",
                value=f"```{donation['final_amount']} Robux | {donation['created_at'][:10]}```",
                inline=False
            )
        
        embed.set_footer(text=f"Total: {total_robux} Robux")
        await interaction.followup.send(embed=embed)
    
    @discord.app_commands.command(name="force_check", description="Forcer la vérification immédiate des donations")
    async def force_check(self, interaction: discord.Interaction):
        """Forcer la vérification immédiate (ADMIN)"""
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Vous n'avez pas les permissions.", ephemeral=True)
            return
        
        await interaction.response.send_message("⚙️ Vérification forcée en cours...")
        await self.check_donations()
        await interaction.followup.send("✅ Vérification terminée")


async def setup_bot():
    await bot.add_cog(DonationCog(bot))


# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/webhook/devproduct', methods=['POST'])
def handle_devproduct_purchase():
    """Webhook Roblox pour les achats DevProduct"""
    
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
    
    player_info = get_user_info(user_id)
    target_info = get_user_info(target_user_id)
    
    player_name = player_info.get("name", f"User {user_id}") if player_info else f"User {user_id}"
    target_name = target_info.get("name", f"User {target_user_id}") if target_info else f"User {target_user_id}"
    
    cog = bot.get_cog("DonationCog")
    if cog:
        asyncio.run_coroutine_threadsafe(
            cog.send_donation_notification(player_name, target_name, amount, final_amount, taxes),
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

init_db()

async def bot_startup():
    await setup_bot()
    await bot.start(DISCORD_TOKEN)

def run_bot():
    asyncio.new_event_loop().run_until_complete(bot_startup())

bot_thread = threading.Thread(target=run_bot, daemon=True)
bot_thread.start()

logger.info("✅ Flask + Discord Bot en cours de démarrage...")

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(
        host='0.0.0.0',
        port=port,
        debug=False,
        use_reloader=False
    )
