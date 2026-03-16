import asyncio
import json
import os
import sys
import logging
import traceback
import discord
import aiohttp
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import aiosqlite
from flask import Flask, jsonify, request, send_from_directory, make_response, redirect
from flask_cors import CORS
import threading

# Flask app
web_app = Flask(__name__)
CORS(web_app)

# Bot instance for web
web_bot = None

# Web routes
@web_app.route('/')
def index():
    return send_from_directory('web', 'index.html')

@web_app.route('/<path:filename>')
def serve_static(filename):
    return send_from_directory('web', filename)

@web_app.route('/api/stats')
def get_stats():
    if not web_bot:
        return jsonify({'servers': 0, 'members': 0, 'messages': 0, 'ping': 0})
    
    return jsonify({
        'servers': len(web_bot.guilds),
        'members': sum(g.member_count for g in web_bot.guilds),
        'messages': 0,
        'ping': int(web_bot.latency * 1000) if web_bot.latency else 0
    })

@web_app.route('/api/servers')
def get_servers():
    if not web_bot:
        return jsonify([])
    
    servers = []
    for guild in web_bot.guilds:
        servers.append({
            'id': str(guild.id),
            'name': guild.name,
            'icon': guild.icon.url if guild.icon else None,
            'members': guild.member_count,
            'channels': len(guild.channels),
            'roles': len(guild.roles)
        })
    return jsonify(servers)

@web_app.route('/api/bot-info')
def get_bot_info():
    if not web_bot:
        return jsonify({})
    return jsonify({
        'owner_id': str(config['bot']['owner_id']),
        'username': web_bot.user.username,
        'avatar': str(web_bot.user.display_avatar.url) if web_bot.user.display_avatar else None
    })

@web_app.route('/api/owner')
def get_owner():
    return jsonify({'owner_id': str(config['bot']['owner_id'])})

# Session check - returns user servers
@web_app.route('/api/session')
def check_session():
    token = request.headers.get('Authorization') or request.cookies.get('token')
    
    if not token:
        return jsonify({'authenticated': False})
    
    if token == 'owner_session':
        # Get user info from cookie or Discord
        import requests
        user_id = request.cookies.get('user_id')
        user_info = {'id': user_id, 'username': 'Kullanıcı', 'tag': '#0000', 'avatar': None}
        
        # Try to get user info using token from cookie
        access_token = request.cookies.get('access_token')
        if access_token and web_bot:
            try:
                headers = {'Authorization': f'Bearer {access_token}'}
                resp = requests.get('https://discord.com/api/users/@me', headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    avatar_hash = data.get('avatar')
                    avatar_url = f"https://cdn.discordapp.com/avatars/{data['id']}/{avatar_hash}.png" if avatar_hash else f"https://cdn.discordapp.com/embed/avatars/{int(data['discriminator']) % 5}.png"
                    user_info = {
                        'id': data.get('id'),
                        'username': data.get('username'),
                        'tag': f"#{data.get('discriminator')}",
                        'avatar': avatar_url
                    }
            except:
                pass
        
        # Get servers where user is admin
        servers = []
        
        if web_bot:
            for guild in web_bot.guilds:
                # Check if user is admin on this server
                if user_id:
                    member = guild.get_member(int(user_id))
                    if member and (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
                        servers.append({
                            'id': str(guild.id),
                            'name': guild.name,
                            'icon': guild.icon.url if guild.icon else None,
                            'members': guild.member_count,
                            'channels': len(guild.channels),
                            'roles': len(guild.roles),
                            'created': guild.created_at.strftime('%d/%m/%Y')
                        })
                else:
                    # Fallback: show all servers if no user_id
                    servers.append({
                        'id': str(guild.id),
                        'name': guild.name,
                        'icon': guild.icon.url if guild.icon else None,
                        'members': guild.member_count,
                        'channels': len(guild.channels),
                        'roles': len(guild.roles),
                        'created': guild.created_at.strftime('%d/%m/%Y')
                    })
        
        return jsonify({
            'authenticated': True,
            'user': user_info,
            'servers': servers
        })
    
    return jsonify({'authenticated': False})

# Get user's servers (where user is admin)
@web_app.route('/api/my-servers')
def get_my_servers():
    token = request.headers.get('Authorization') or request.cookies.get('token')
    if token != 'owner_session':
        return jsonify([])
    
    if not web_bot:
        return jsonify([])
    
    servers = []
    for guild in web_bot.guilds:
        servers.append({
            'id': str(guild.id),
            'name': guild.name,
            'icon': guild.icon.url if guild.icon else None,
            'members': guild.member_count,
            'channels': len(guild.channels),
            'roles': len(guild.roles),
            'created': guild.created_at.strftime('%d/%m/%Y')
        })
    
    return jsonify(servers)

# Server-specific settings
@web_app.route('/api/server/<server_id>/settings')
def get_server_settings(server_id):
    # Permission check
    token = request.cookies.get('token')
    user_id = request.cookies.get('user_id')
    
    if not token or token != 'owner_session':
        return jsonify({'error': 'Giriş yapmalısınız'}), 401
    
    # Check if user has permission on this server
    if web_bot:
        guild = web_bot.get_guild(int(server_id))
        if not guild:
            return jsonify({'error': 'Sunucu bulunamadı'}), 404
        
        member = guild.get_member(int(user_id)) if user_id else None
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            return jsonify({'error': 'Bu sunucuda yetkiniz yok'}), 403
    
    # Try to get from database, else return defaults
    import asyncio
    async def fetch_settings():
        try:
            cursor = await web_bot.db.execute(
                "SELECT * FROM servers WHERE server_id = ?", (server_id,)
            )
            row = await cursor.fetchone()
            if row:
                return {
                    'welcome': {
                        'enabled': bool(row[2]),
                        'channel': row[3] or '',
                        'message': row[4] or 'Hoş geldin {user}!'
                    },
                    'leave': {
                        'enabled': bool(row[5]),
                        'channel': row[6] or '',
                        'message': row[7] or 'Güle güle {user}!'
                    },
                    'moderation': {
                        'antispam': bool(row[10]),
                        'antilink': bool(row[11]),
                        'autorole': bool(row[8]) if row[8] else False,
                        'autorole_role_id': row[8] or '',
                        'log_channel_id': ''
                    },
                    'economy': {
                        'enabled': True,
                        'daily_reward': 250,
                        'start_balance': 500
                    },
                    'levels': {
                        'enabled': bool(row[9]) if row[9] is not None else True,
                        'xp_per_message': 5
                    }
                }
        except Exception as e:
            print(f"DB Error: {e}")
        return None
    
    result = asyncio.run(fetch_settings())
    if result:
        return jsonify({'settings': result})
    
    # Default settings
    return jsonify({
        'settings': {
            'welcome': {'enabled': False, 'channel': '', 'message': 'Hoş geldin {user}!'},
            'leave': {'enabled': False, 'channel': '', 'message': 'Güle güle {user}!'},
            'moderation': {'antispam': False, 'antilink': False, 'anticaps': False, 'autorole': False, 'autorole_role_id': '', 'log_channel_id': ''},
            'economy': {'enabled': True, 'daily_reward': 250, 'start_balance': 500},
            'levels': {'enabled': True, 'xp_per_message': 5}
        }
    })

@web_app.route('/api/server/<server_id>/settings', methods=['POST'])
def save_server_settings(server_id):
    # Permission check
    token = request.cookies.get('token')
    user_id = request.cookies.get('user_id')
    
    if not token or token != 'owner_session':
        return jsonify({'success': False, 'message': 'Giriş yapmalısınız'}), 401
    
    # Check if user has permission
    if web_bot:
        guild = web_bot.get_guild(int(server_id))
        if not guild:
            return jsonify({'success': False, 'message': 'Sunucu bulunamadı'}), 404
        
        member = guild.get_member(int(user_id)) if user_id else None
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            return jsonify({'success': False, 'message': 'Bu sunucuda yetkiniz yok'}), 403
    
    data = request.json
    
    welcome = data.get('welcome', {})
    leave = data.get('leave', {})
    moderation = data.get('moderation', {})
    economy = data.get('economy', {})
    levels = data.get('levels', {})
    
    import asyncio
    async def save():
        try:
            await web_bot.db.execute('''
                INSERT OR REPLACE INTO servers 
                (server_id, name, welcome_enabled, welcome_channel_id, welcome_message,
                 leave_enabled, leave_channel_id, leave_message, auto_role, level_system,
                 anti_spam, anti_invite)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                server_id,
                guild.name if guild else '',
                1 if welcome.get('enabled') else 0,
                welcome.get('channel_id', ''),
                welcome.get('message', ''),
                1 if leave.get('enabled') else 0,
                leave.get('channel_id', ''),
                leave.get('message', ''),
                moderation.get('autorole_role_id', ''),
                1 if levels.get('enabled') else 0,
                1 if moderation.get('antispam') else 0,
                1 if moderation.get('antilink') else 0
            ))
            await web_bot.db.commit()
            return True
        except Exception as e:
            print(f"Save Error: {e}")
            return False
    
    success = asyncio.run(save())
    if success:
        return jsonify({'success': True, 'message': 'Ayarlar kaydedildi!'})
    return jsonify({'success': False, 'message': 'Kaydetme hatası'})

@web_app.route('/api/server/<server_id>/welcome', methods=['POST'])
def save_server_welcome(server_id):
    return jsonify({'success': True})

@web_app.route('/api/server/<server_id>/moderation', methods=['POST'])
def save_server_moderation(server_id):
    return jsonify({'success': True})

@web_app.route('/api/server/<server_id>/economy', methods=['POST'])
def save_server_economy(server_id):
    return jsonify({'success': True})

@web_app.route('/api/server/<server_id>/levels', methods=['POST'])
def save_server_levels(server_id):
    return jsonify({'success': True})

# Login - Redirect to Discord OAuth
@web_app.route('/api/discord-oauth')
def discord_oauth():
    client_id = config['bot'].get('client_id', '')
    if not client_id or client_id == 'BOT_CLIENT_ID':
        return jsonify({'error': 'OAuth ayarlanmamış! config.json client_id ekle'})
    
    # Web URL - ngrok veya hosting URL'sini buraya ekle
    web_url = config['web'].get('url', 'http://localhost:5000').rstrip('/')
    redirect_uri = f"{web_url}/oauth/callback"
    scope = 'identify guilds'
    
    oauth_url = f"https://discord.com/oauth2/authorize?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&scope={scope}"
    return redirect(oauth_url)

@web_app.route('/api/login')
def login():
    return redirect('/api/discord-oauth')

# Basit giriş endpoint
@web_app.route('/api/login-simple', methods=['POST', 'GET'])
def simple_login():
    servers = []
    if web_bot:
        for guild in web_bot.guilds:
            servers.append({
                'id': str(guild.id),
                'name': guild.name,
                'icon': guild.icon.url if guild.icon else None,
                'members': guild.member_count,
                'channels': len(guild.channels),
                'roles': len(guild.roles),
                'created': guild.created_at.strftime('%d/%m/%Y')
            })
    
    response = make_response(jsonify({
        'success': True,
        'user': {'id': str(config['bot']['owner_id']), 'username': 'Bot Sahibi'},
        'servers': servers
    }))
    response.set_cookie('token', 'owner_session', max_age=86400*7)
    return response

# OAuth Callback
@web_app.route('/oauth/callback')
def oauth_callback():
    try:
        import requests
        
        code = request.args.get('code')
        
        if not code:
            return redirect('/?error=no_code')
        
        client_id = config['bot'].get('client_id', '')
        client_secret = config['bot'].get('client_secret', '')
        
        # Web URL - ngrok veya hosting URL
        web_url = config['web'].get('url', 'http://localhost:5000').rstrip('/')
        redirect_uri = f"{web_url}/oauth/callback"
        
        if not client_id or not client_secret:
            return redirect('/?error=no_config')
        
        # Exchange code for token
        token_url = 'https://discord.com/api/oauth2/token'
        data = {
            'client_id': client_id,
            'client_secret': client_secret,
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': redirect_uri
        }
        
        token_resp = requests.post(token_url, data=data)
        if token_resp.status_code != 200:
            return redirect('/?error=token_failed')
        
        token_data = token_resp.json()
        access_token = token_data.get('access_token')
        
        # Get user info
        headers = {'Authorization': f'Bearer {access_token}'}
        user_resp = requests.get('https://discord.com/api/users/@me', headers=headers)
        if user_resp.status_code != 200:
            return redirect('/?error=user_failed')
        
        user_data = user_resp.json()
        user_id = str(user_data.get('id'))
        
        # Session oluştur
        response = make_response(redirect('/#loggedin'))
        response.set_cookie('token', 'owner_session', max_age=86400*7)
        response.set_cookie('user_id', user_id, max_age=86400*7)
        response.set_cookie('access_token', access_token, max_age=86400*7)
        return response
        
    except Exception as e:
        print(f"OAuth Error: {e}")
        return redirect('/?error=oauth_error')

# Logout
@web_app.route('/api/logout')
def logout():
    response = make_response(redirect('/'))
    response.set_cookie('token', '', expires=0)
    response.set_cookie('user_id', '', expires=0)
    response.set_cookie('access_token', '', expires=0)
    return response

# Premium users veritabanı
import json
DATA_DIR = 'data'
os.makedirs(DATA_DIR, exist_ok=True)
PREMIUM_FILE = os.path.join(DATA_DIR, 'premium.json')
SETTINGS_FILE = os.path.join(DATA_DIR, 'settings.json')

def load_premium_users():
    if os.path.exists(PREMIUM_FILE):
        with open(PREMIUM_FILE, 'r') as f:
            return json.load(f)
    return []

def save_premium_users(users):
    with open(PREMIUM_FILE, 'w') as f:
        json.dump(users, f)

@web_app.route('/api/premium/users')
def get_premium_users():
    users = load_premium_users()
    return jsonify(users)

@web_app.route('/api/premium/add', methods=['POST'])
def add_premium_user():
    data = request.json
    user_id = str(data.get('user_id'))
    days = data.get('days', 30)
    
    users = load_premium_users()
    
    # Mevcut kullanıcıyı güncelle veya yeni ekle
    existing = False
    for u in users:
        if u['id'] == user_id:
            u['days'] = days
            u['added_at'] = str(datetime.now())
            existing = True
            break
    
    if not existing:
        users.append({
            'id': user_id,
            'days': days,
            'added_at': str(datetime.now())
        })
    
    save_premium_users(users)
    return jsonify({'success': True})

@web_app.route('/api/premium/remove', methods=['POST'])
def remove_premium_user():
    data = request.json
    user_id = str(data.get('user_id'))
    
    users = load_premium_users()
    users = [u for u in users if u['id'] != user_id]
    
    save_premium_users(users)
    return jsonify({'success': True})

@web_app.route('/api/settings', methods=['POST'])
def save_settings():
    data = request.json
    
    settings = {
        'status': data.get('status', 'online'),
        'activity': data.get('activity', 'h!yardim'),
        'economy': data.get('economy', {})
    }
    
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f)
    
    return jsonify({'success': True, 'message': 'Ayarlar kaydedildi!'})

def run_web():
    port = int(os.environ.get('PORT', 5000))
    web_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# Load config
with open('config.json', 'r', encoding='utf-8') as f:
    config = json.load(f)

# Get token from environment variable (for Render.com)
BOT_TOKEN = os.environ.get('DISCORD_TOKEN', config['bot']['token'])

# Update config with environment variables
if os.environ.get('CLIENT_ID'):
    config['bot']['client_id'] = os.environ.get('CLIENT_ID')
if os.environ.get('CLIENT_SECRET'):
    config['bot']['client_secret'] = os.environ.get('CLIENT_SECRET')
if os.environ.get('WEB_URL'):
    config['web']['url'] = os.environ.get('WEB_URL')

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True
intents.voice_states = True
intents.guilds = True
intents.guild_messages = True
intents.guild_reactions = True
intents.dm_messages = True

bot = commands.Bot(
    command_prefix=config['bot']['prefix'],
    intents=intents,
    case_insensitive=True,
    help_command=None,
    owner_id=config['bot']['owner_id']
)

bot.config = config
bot.db = None
bot.start_time = datetime.now()
bot.cache = {}
bot.music_players = {}
bot.giveaways = {}
bot.tickets = {}
bot.afk = {}

logging.basicConfig(level=logging.INFO, format='%(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('huh')
logger.setLevel(logging.INFO)

async def init_db():
    bot.db = await aiosqlite.connect(config['database']['name'])
    logger.info("Veritabani basariyla olusturuldu!")
    
    await bot.db.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 500,
            bank INTEGER DEFAULT 0,
            total_messages INTEGER DEFAULT 0,
            last_daily TEXT,
            last_weekly TEXT,
            last_work TEXT
        )
    ''')
    
    await bot.db.execute('''
        CREATE TABLE IF NOT EXISTS servers (
            server_id TEXT PRIMARY KEY,
            name TEXT,
            welcome_enabled INTEGER DEFAULT 0,
            welcome_channel_id TEXT,
            welcome_message TEXT,
            leave_enabled INTEGER DEFAULT 0,
            leave_channel_id TEXT,
            leave_message TEXT,
            auto_role TEXT,
            level_system INTEGER DEFAULT 1,
            anti_spam INTEGER DEFAULT 0,
            anti_invite INTEGER DEFAULT 0
        )
    ''')
    
    await bot.db.execute('''
        CREATE TABLE IF NOT EXISTS levels (
            server_id TEXT,
            user_id TEXT,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1,
            messages INTEGER DEFAULT 0,
            PRIMARY KEY (server_id, user_id)
        )
    ''')
    
    await bot.db.execute('''
        CREATE TABLE IF NOT EXISTS mutes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            guild_id TEXT,
            reason TEXT,
            expires_at INTEGER,
            is_active INTEGER DEFAULT 1
        )
    ''')
    
    await bot.db.execute('''
        CREATE TABLE IF NOT EXISTS giveaways (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT,
            channel_id TEXT,
            guild_id TEXT,
            prize TEXT,
            winners INTEGER,
            ends_at INTEGER,
            created_by TEXT,
            is_ended INTEGER DEFAULT 0,
            participants TEXT
        )
    ''')
    
    await bot.db.execute('''
        CREATE TABLE IF NOT EXISTS afk (
            user_id TEXT PRIMARY KEY,
            reason TEXT,
            started_at TEXT
        )
    ''')
    
    await bot.db.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT,
            user_id TEXT,
            guild_id TEXT,
            created_at TEXT,
            status TEXT DEFAULT 'open'
        )
    ''')
    
    await bot.db.execute('''
        CREATE TABLE IF NOT EXISTS warns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            guild_id TEXT,
            reason TEXT,
            moderator_id TEXT,
            created_at TEXT
        )
    ''')
    
    await bot.db.commit()
    logger.info("Veritabani tablolari olusturuldu!")

async def get_user(user_id):
    cursor = await bot.db.execute(
        'SELECT * FROM users WHERE user_id = ?', (str(user_id),)
    )
    user = await cursor.fetchone()
    if user is None:
        await bot.db.execute(
            'INSERT INTO users (user_id, username, balance) VALUES (?, ?, ?)',
            (str(user_id), 'Unknown', config['economy']['starting_balance'])
        )
        await bot.db.commit()
        return await get_user(user_id)
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, user))

async def get_server(server_id):
    cursor = await bot.db.execute(
        'SELECT * FROM servers WHERE server_id = ?', (str(server_id),)
    )
    server = await cursor.fetchone()
    if server is None:
        await bot.db.execute(
            'INSERT INTO servers (server_id, name) VALUES (?, ?)',
            (str(server_id), 'Unknown')
        )
        await bot.db.commit()
        return await get_server(server_id)
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, server))

async def update_user(user_id, data):
    user = await get_user(user_id)
    for key, value in data.items():
        user[key] = value
    columns = ', '.join([f"{k} = ?" for k in data.keys()])
    await bot.db.execute(
        f'UPDATE users SET {columns} WHERE user_id = ?',
        list(data.values()) + [str(user_id)]
    )
    await bot.db.commit()
    return user

async def get_server(server_id):
    cursor = await bot.db.execute(
        'SELECT * FROM servers WHERE server_id = ?', (str(server_id),)
    )
    server = await cursor.fetchone()
    if server is None:
        await bot.db.execute(
            'INSERT INTO servers (server_id, name) VALUES (?, ?)',
            (str(server_id), 'Unknown')
        )
        await bot.db.commit()
        return await get_server(server_id)
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, server))

async def update_server(server_id, data):
    server = await get_server(server_id)
    for key, value in data.items():
        server[key] = value
    columns = ', '.join([f"{k} = ?" for k in data.keys()])
    await bot.db.execute(
        f'UPDATE servers SET {columns} WHERE server_id = ?',
        list(data.values()) + [str(server_id)]
    )
    await bot.db.commit()
    return server

@bot.event
async def on_ready():
    logger.info(f"Bot aktif! {bot.user}")
    logger.info(f"Sunucu sayisi: {len(bot.guilds)}")
    
    bot.get_user = get_user
    bot.get_server = get_server
    
    await init_db()
    
    try:
        synced = await bot.tree.sync()
        logger.info(f"{len(synced)} slash komut yüklendi")
    except Exception as e:
        logger.error(f"Slash sync hatasi: {e}")
    
    status_task.start()
    giveaway_task.start()
    mute_task.start()
    backup_task.start()
    
    await bot.change_presence(
        status=discord.Status[config['bot'].get('default_status', 'online')],
        activity=discord.Game(config['bot']['activity_text'])
    )

@tasks.loop(minutes=5)
async def status_task():
    statuses = [
        f"{len(bot.guilds)} sunucu",
        f"{sum(g.member_count for g in bot.guilds)} kullanan",
        "h!yardim",
        "En iyi bot"
    ]
    import random
    status = random.choice(statuses)
    await bot.change_presence(activity=discord.Game(status))

@tasks.loop(seconds=30)
async def giveaway_task():
    try:
        cursor = await bot.db.execute(
            "SELECT * FROM giveaways WHERE is_ended = 0 AND ends_at < ?",
            (int(datetime.now().timestamp()),)
        )
        giveaways = await cursor.fetchall()
        
        for giveaway in giveaways:
            if len(giveaways) == 0:
                break
            
            participants = json.loads(giveaway[9])
            if len(participants) == 0:
                await bot.db.execute(
                    "UPDATE giveaways SET is_ended = 1 WHERE id = ?",
                    (giveaway[0],)
                )
                await bot.db.commit()
                continue
            
            import random
            winners = random.sample(participants, min(giveaway[5], len(participants)))
            
            channel = bot.get_channel(int(giveaway[3]))
            message = await channel.fetch_message(int(giveaway[1]))
            
            embed = discord.Embed(
                title="🎉 Cekilis Sonuclandi!",
                description=f"**Odul:** {giveaway[6]}\n\n**Kazananlar:**\n" + 
                           "\n".join([f"<@{w}>" for w in winners]),
                color=discord.Color.green()
            )
            await message.reply(embed=embed)
            
            await bot.db.execute(
                "UPDATE giveaways SET is_ended = 1 WHERE id = ?",
                (giveaway[0],)
            )
            await bot.db.commit()
    except Exception as e:
        logger.error(f"Cekilis hatasi: {e}")

@tasks.loop(minutes=1)
async def mute_task():
    try:
        cursor = await bot.db.execute(
            "SELECT * FROM mutes WHERE is_active = 1 AND expires_at < ?",
            (int(datetime.now().timestamp()),)
        )
        mutes = await cursor.fetchall()
        
        for mute in mutes:
            guild = bot.get_guild(int(mute[2]))
            if guild:
                member = guild.get_member(int(mute[1]))
                if member:
                    try:
                        await member.timeout(None)
                    except:
                        pass
            
            await bot.db.execute(
                "UPDATE mutes SET is_active = 0 WHERE id = ?",
                (mute[0],)
            )
        await bot.db.commit()
    except Exception as e:
        logger.error(f"Mute hatasi: {e}")

@tasks.loop(hours=6)
async def backup_task():
    try:
        import shutil
        shutil.copy(config['database']['name'], f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
        logger.info("Yedekleme basarili!")
    except Exception as e:
        logger.error(f"Yedekleme hatasi: {e}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    try:
        if message.guild:
            user = await get_user(message.author.id)
            await update_user(message.author.id, {
                'total_messages': user.get('total_messages', 0) + 1
            })
            
            server = await get_server(message.guild.id)
            
            if server.get('anti_spam', 0):
                pass
            
            if server.get('anti_invite', 0):
                if 'discord.gg' in message.content or 'discordapp.com/invite' in message.content:
                    if not message.author.guild_permissions.manage_messages:
                        await message.delete()
                        await message.channel.send(f"{message.author.mention} Reklam yapmak yasak!", delete_after=3)
                        return
            
            if server.get('level_system', 1):
                cursor = await bot.db.execute(
                    "SELECT * FROM levels WHERE server_id = ? AND user_id = ?",
                    (str(message.guild.id), str(message.author.id))
                )
                level_data = await cursor.fetchone()
                
                if level_data:
                    new_xp = level_data[3] + config['leveling']['xp_per_message']
                    new_level = level_data[4]
                    xp_needed = new_level * 100
                    
                    if new_xp >= xp_needed:
                        new_level += 1
                        new_xp = 0
                        
                        await bot.db.execute(
                            "UPDATE levels SET xp = ?, level = ?, messages = messages + 1 WHERE server_id = ? AND user_id = ?",
                            (new_xp, new_level, str(message.guild.id), str(message.author.id))
                        )
                        
                        for lr in config['leveling']['level_roles']:
                            if new_level == lr['level']:
                                role = discord.utils.get(message.guild.roles, name=lr['role'])
                                if role:
                                    await message.author.add_roles(role)
                                    await message.channel.send(f"🎉 Tebrikler {message.author.mention}! Seviye atladin! ({new_level})")
                                break
                    else:
                        await bot.db.execute(
                            "UPDATE levels SET xp = ?, messages = messages + 1 WHERE server_id = ? AND user_id = ?",
                            (new_xp, str(message.guild.id), str(message.author.id))
                        )
                else:
                    await bot.db.execute(
                        "INSERT INTO levels (server_id, user_id, xp, level, messages) VALUES (?, ?, ?, ?, ?)",
                        (str(message.guild.id), str(message.author.id), config['leveling']['xp_per_message'], 1, 1)
                    )
                await bot.db.commit()
        
        cursor = await bot.db.execute(
            "SELECT * FROM afk WHERE user_id = ?", (str(message.author.id),)
        )
        afk_data = await cursor.fetchone()
        if afk_data:
            await bot.db.execute("DELETE FROM afk WHERE user_id = ?", (str(message.author.id),))
            await bot.db.commit()
            await message.author.edit(nick=None)
            await message.channel.send(f"{message.author.mention} Artik AFK degil!")
    except Exception as e:
        logger.error(f"on_message hata: {e}")
    
    await bot.process_commands(message)

@bot.event
async def on_member_join(member):
    if member.bot:
        server = await get_server(member.guild.id)
        await update_server(member.guild.id, {
            'stats_bots': server.get('stats_bots', 0) + 1
        })
        return
    
    server = await get_server(member.guild.id)
    await update_server(member.guild.id, {
        'stats_members': server.get('stats_members', 0) + 1
    })
    
    if server.get('auto_role'):
        role = member.guild.get_role(int(server['auto_role']))
        if role:
            await member.add_roles(role)
    
    if server.get('welcome_enabled') and server.get('welcome_channel'):
        channel = member.guild.get_channel(int(server['welcome_channel']))
        if channel:
            msg = server.get('welcome_message', 'Hos geldin {user}!')
            msg = msg.replace('{user}', member.name).replace('{mention}', member.mention)
            msg = msg.replace('{server}', member.guild.name).replace('{count}', str(member.guild.member_count))
            await channel.send(msg)

@bot.event
async def on_member_remove(member):
    server = await get_server(member.guild.id)
    if member.bot:
        await update_server(member.guild.id, {
            'stats_bots': max(0, server.get('stats_bots', 0) - 1)
        })
    else:
        await update_server(member.guild.id, {
            'stats_members': max(0, server.get('stats_members', 0) - 1)
        })
    
    if server.get('leave_enabled') and server.get('leave_channel'):
        channel = member.guild.get_channel(int(server['leave_channel']))
        if channel:
            msg = server.get('leave_message', 'Gule gule {user}!')
            msg = msg.replace('{user}', member.name).replace('{server}', member.guild.name)
            await channel.send(msg)

@bot.event
async def on_guild_join(guild):
    await get_server(guild.id)
    logger.info(f"Yeni sunucuya katildi: {guild.name}")

@bot.event
async def on_guild_remove(guild):
    logger.info(f"Sunucudan ayrildi: {guild.name}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("Bu komutu kullanmak icin yetkin yok!")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Eksik arguman: {error.param}")
    else:
        logger.error(f"Komut hatasi: {error}")
        await ctx.send(f"Bir hata olustu: {str(error)}")

async def load_cogs():
    cogs = [
        'cogs.help',
        'cogs.fun2',
        'cogs.moderation',
        'cogs.economy', 
        'cogs.fun',
        'cogs.utility',
        'cogs.leveling',
        'cogs.system',
        'cogs.games',
        'cogs.mega',
        'cogs.aichat',
    ]
    
    for cog in cogs:
        try:
            await bot.load_extension(cog)
            logger.info(f"{cog} yüklendi")
        except Exception as e:
            logger.error(f"{cog} yüklenemedi: {e}")

async def main():
    global web_bot
    web_bot = bot
    
    # Web sunucusunu ayrı thread'de başlat
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    logger.info("🌐 Web sunucusu başlatıldı!")
    
    await load_cogs()
    
    async with bot:
        try:
            await bot.start(BOT_TOKEN)
        except discord.LoginFailure:
            logger.error("Token gecersiz!")
        except Exception as e:
            logger.error(f"Baslatma hatasi: {e}")
            traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(main())
