# bot.py
"""
Discord Account Generator Bot
Prefix: g!
All commands in English.
"""

import discord
from discord.ext import commands, tasks
import aiosqlite
import asyncio
import aiohttp
import random
import string
import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

# ==================== CONFIG ====================
TOKEN = "MTU0Mzc4MTUwODcxMDQwODE5Mg.GkZzvW.-rtC-TtdtPTHGEHbKZaf533rvB_IYbpfE06qi8"
OWNER_IDS = [1542250734450376820]  # your discord user id(s)
DEFAULT_PREFIX = "g!"
DB_PATH = "genbot.db"
COOLDOWN_DEFAULT = 30  # seconds

# ==================== DATABASE ====================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS guilds (
            guild_id INTEGER PRIMARY KEY,
            prefix TEXT DEFAULT 'g!',
            free_channel INTEGER,
            vip_channel INTEGER,
            boost_channel INTEGER,
            feedback_channel INTEGER,
            premium_role_free INTEGER,
            premium_role_vip INTEGER,
            premium_role_boost INTEGER,
            staff_role INTEGER,
            vanity_role INTEGER,
            vanity_string TEXT,
            drop_mode TEXT DEFAULT 'embed',  -- embed | webhook
            drop_webhook TEXT,
            cooldown_free INTEGER DEFAULT 60,
            cooldown_vip INTEGER DEFAULT 30,
            cooldown_boost INTEGER DEFAULT 15,
            cooldown_all INTEGER DEFAULT 0,
            server_description TEXT,
            server_invite TEXT,
            server_icon TEXT,
            server_banner TEXT,
            logs_channel INTEGER
        );

        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            service TEXT,          -- free / vip / boost
            service_name TEXT,     -- e.g. netflix, spotify
            accounts TEXT,         -- newline separated email:pass or tokens
            UNIQUE(guild_id, service, service_name)
        );

        CREATE TABLE IF NOT EXISTS keys (
            key_code TEXT PRIMARY KEY,
            guild_id INTEGER,
            tier TEXT,             -- free / vip / boost
            duration_days INTEGER,
            max_uses INTEGER DEFAULT 1,
            uses INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at REAL,
            revoked INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS premium_users (
            guild_id INTEGER,
            user_id INTEGER,
            tier TEXT,
            expires_at REAL,
            key_used TEXT,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS blacklist (
            guild_id INTEGER,
            user_id INTEGER,
            reason TEXT,
            added_by INTEGER,
            added_at REAL,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS cooldowns (
            guild_id INTEGER,
            user_id INTEGER,
            service TEXT,
            last_gen REAL,
            PRIMARY KEY (guild_id, user_id, service)
        );

        CREATE TABLE IF NOT EXISTS gen_stats (
            guild_id INTEGER,
            service TEXT,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, service)
        );

        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            content TEXT,
            created_at REAL
        );

        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            content TEXT,
            created_at REAL
        );

        CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            content TEXT,
            created_at REAL
        );
        """)
        await db.commit()

# ==================== BOT SETUP ====================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

async def get_prefix(bot, message):
    if not message.guild:
        return DEFAULT_PREFIX
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT prefix FROM guilds WHERE guild_id = ?", (message.guild.id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else DEFAULT_PREFIX

bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)

# ==================== HELPERS ====================
def is_owner():
    async def predicate(ctx):
        return ctx.author.id in OWNER_IDS
    return commands.check(predicate)

def is_staff():
    async def predicate(ctx):
        if ctx.author.id in OWNER_IDS:
            return True
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT staff_role FROM guilds WHERE guild_id = ?", (ctx.guild.id,)) as cur:
                row = await cur.fetchone()
                if row and row[0]:
                    role = ctx.guild.get_role(row[0])
                    if role and role in ctx.author.roles:
                        return True
        return ctx.author.guild_permissions.administrator
    return commands.check(predicate)

async def ensure_guild(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO guilds (guild_id) VALUES (?)", (guild_id,))
        await db.commit()

async def is_blacklisted(guild_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM blacklist WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)) as cur:
            return await cur.fetchone() is not None

async def get_user_tier(guild_id: int, user_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT tier, expires_at FROM premium_users WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id)
        ) as cur:
            row = await cur.fetchone()
            if row and row[1] > time.time():
                return row[0]
            elif row:
                # expired
                await db.execute("DELETE FROM premium_users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
                await db.commit()
    return None

async def check_cooldown(guild_id: int, user_id: int, service: str) -> int:
    """Returns remaining seconds or 0 if ready."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT last_gen FROM cooldowns WHERE guild_id = ? AND user_id = ? AND service = ?",
            (guild_id, user_id, service)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return 0
        last = row[0]
        # get cooldown value
        async with db.execute(
            f"SELECT cooldown_{service}, cooldown_all FROM guilds WHERE guild_id = ?",
            (guild_id,)
        ) as cur:
            g = await cur.fetchone()
        cd = g[1] if g and g[1] > 0 else (g[0] if g else COOLDOWN_DEFAULT)
        remaining = int(cd - (time.time() - last))
        return max(0, remaining)

async def set_cooldown(guild_id: int, user_id: int, service: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO cooldowns (guild_id, user_id, service, last_gen) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, service, time.time())
        )
        await db.commit()

async def take_account(guild_id: int, service: str, service_name: str = None) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        if service_name:
            async with db.execute(
                "SELECT id, accounts FROM stock WHERE guild_id = ? AND service = ? AND service_name = ?",
                (guild_id, service, service_name)
            ) as cur:
                row = await cur.fetchone()
        else:
            async with db.execute(
                "SELECT id, accounts, service_name FROM stock WHERE guild_id = ? AND service = ? AND accounts != '' LIMIT 1",
                (guild_id, service)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    service_name = row[2]
        if not row or not row[1].strip():
            return None
        accounts = [a.strip() for a in row[1].strip().splitlines() if a.strip()]
        if not accounts:
            return None
        account = accounts.pop(0)
        new_stock = "\n".join(accounts)
        await db.execute("UPDATE stock SET accounts = ? WHERE id = ?", (new_stock, row[0]))
        # stats
        await db.execute(
            "INSERT INTO gen_stats (guild_id, service, count) VALUES (?, ?, 1) "
            "ON CONFLICT(guild_id, service) DO UPDATE SET count = count + 1",
            (guild_id, service)
        )
        await db.commit()
        return account

def generate_key(length=16) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))

# ==================== EVENTS ====================
@bot.event
async def on_ready():
    await init_db()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Prefix: {DEFAULT_PREFIX}")
    activity = discord.Activity(type=discord.ActivityType.watching, name="g!help | Account Gen")
    await bot.change_presence(activity=activity)

@bot.event
async def on_guild_join(guild):
    await ensure_guild(guild.id)

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    # vanity status check (simple)
    if message.guild:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT vanity_role, vanity_string FROM guilds WHERE guild_id = ?",
                (message.guild.id,)
            ) as cur:
                row = await cur.fetchone()
            if row and row[0] and row[1]:
                vanity = row[1].lower()
                status = str(message.author.activity).lower() if message.author.activity else ""
                if vanity in status:
                    role = message.guild.get_role(row[0])
                    if role and role not in message.author.roles:
                        try:
                            await message.author.add_roles(role, reason="Vanity status")
                        except:
                            pass
    await bot.process_commands(message)

# ==================== ERROR HANDLER ====================
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command.")
    elif isinstance(error, commands.CheckFailure):
        await ctx.send("❌ You are not allowed to use this command.")
    else:
        await ctx.send(f"❌ Error: `{str(error)[:200]}`")

# ============================================================
# 📂 ADMIN
# ============================================================
@bot.command(name="blacklist")
@is_staff()
async def blacklist_cmd(ctx, action: str = None, member: discord.Member = None, *, reason: str = "No reason"):
    """Manage the blacklist of generators (gen and pgen)."""
    await ensure_guild(ctx.guild.id)
    if not action:
        await ctx.send("Usage: `g!blacklist add/remove/list @user [reason]`")
        return
    action = action.lower()
    if action == "add":
        if not member:
            await ctx.send("Mention a user.")
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO blacklist (guild_id, user_id, reason, added_by, added_at) VALUES (?, ?, ?, ?, ?)",
                (ctx.guild.id, member.id, reason, ctx.author.id, time.time())
            )
            await db.commit()
        await ctx.send(f"✅ {member.mention} has been blacklisted. Reason: {reason}")
    elif action == "remove":
        if not member:
            await ctx.send("Mention a user.")
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM blacklist WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, member.id))
            await db.commit()
        await ctx.send(f"✅ {member.mention} has been removed from the blacklist.")
    elif action == "list":
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id, reason FROM blacklist WHERE guild_id = ?", (ctx.guild.id,)) as cur:
                rows = await cur.fetchall()
        if not rows:
            await ctx.send("Blacklist is empty.")
            return
        text = "\n".join([f"<@{r[0]}> — {r[1]}" for r in rows[:20]])
        await ctx.send(f"**Blacklist:**\n{text}")
    else:
        await ctx.send("Action must be `add`, `remove` or `list`.")

# ============================================================
# 📂 COMMUNITY
# ============================================================
@bot.command(name="feedback")
async def feedback_cmd(ctx, *, content: str = None):
    """Leave a review about your experience on the server."""
    if not content:
        await ctx.send("Please write your feedback after the command.")
        return
    await ensure_guild(ctx.guild.id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO feedback (guild_id, user_id, content, created_at) VALUES (?, ?, ?, ?)",
            (ctx.guild.id, ctx.author.id, content, time.time())
        )
        async with db.execute("SELECT feedback_channel FROM guilds WHERE guild_id = ?", (ctx.guild.id,)) as cur:
            row = await cur.fetchone()
        await db.commit()
    channel_id = row[0] if row else None
    if channel_id:
        channel = ctx.guild.get_channel(channel_id)
        if channel:
            emb = discord.Embed(title="New Feedback", description=content, color=0x00ff99, timestamp=datetime.utcnow())
            emb.set_author(name=str(ctx.author), icon_url=ctx.author.display_avatar.url)
            await channel.send(embed=emb)
    await ctx.send("✅ Feedback submitted. Thank you!")

# ============================================================
# 📂 CONFIG
# ============================================================
@bot.command(name="feedbackconfig")
@is_staff()
async def feedbackconfig(ctx, channel: discord.TextChannel = None):
    """Choose the channel where user reviews are published."""
    await ensure_guild(ctx.guild.id)
    if not channel:
        await ctx.send("Mention a channel.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE guilds SET feedback_channel = ? WHERE guild_id = ?", (channel.id, ctx.guild.id))
        await db.commit()
    await ctx.send(f"✅ Feedback channel set to {channel.mention}")

@bot.command(name="premconfig")
@is_staff()
async def premconfig(ctx, tier: str = None, role: discord.Role = None):
    """Configure the premium role (by tier) and the staff role of this server.
    Usage: g!premconfig free/vip/boost/staff @role
    """
    await ensure_guild(ctx.guild.id)
    if not tier or not role:
        await ctx.send("Usage: `g!premconfig free/vip/boost/staff @role`")
        return
    tier = tier.lower()
    col = {"free": "premium_role_free", "vip": "premium_role_vip", "boost": "premium_role_boost", "staff": "staff_role"}.get(tier)
    if not col:
        await ctx.send("Tier must be free, vip, boost or staff.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE guilds SET {col} = ? WHERE guild_id = ?", (role.id, ctx.guild.id))
        await db.commit()
    await ctx.send(f"✅ {tier} role set to {role.mention}")

@bot.command(name="serverprofile")
@is_staff()
async def serverprofile(ctx, field: str = None, *, value: str = None):
    """Configure the public showcase of this server on the web (description, invite, icon, banner)."""
    await ensure_guild(ctx.guild.id)
    if not field:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT server_description, server_invite, server_icon, server_banner FROM guilds WHERE guild_id = ?",
                (ctx.guild.id,)
            ) as cur:
                row = await cur.fetchone()
        emb = discord.Embed(title="Server Profile", color=0x5865F2)
        emb.add_field(name="Description", value=row[0] or "Not set", inline=False)
        emb.add_field(name="Invite", value=row[1] or "Not set", inline=False)
        emb.add_field(name="Icon URL", value=row[2] or "Not set", inline=False)
        emb.add_field(name="Banner URL", value=row[3] or "Not set", inline=False)
        await ctx.send(embed=emb)
        return
    field = field.lower()
    mapping = {"description": "server_description", "invite": "server_invite", "icon": "server_icon", "banner": "server_banner"}
    if field not in mapping:
        await ctx.send("Field must be description / invite / icon / banner")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE guilds SET {mapping[field]} = ? WHERE guild_id = ?", (value, ctx.guild.id))
        await db.commit()
    await ctx.send(f"✅ Server {field} updated.")

@bot.command(name="setprefix")
@is_staff()
async def setprefix(ctx, new_prefix: str = None):
    """Change the command prefix of this server."""
    if not new_prefix:
        await ctx.send("Provide a new prefix.")
        return
    await ensure_guild(ctx.guild.id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE guilds SET prefix = ? WHERE guild_id = ?", (new_prefix, ctx.guild.id))
        await db.commit()
    await ctx.send(f"✅ Prefix changed to `{new_prefix}`")

@bot.command(name="vanityconfig")
@is_staff()
async def vanityconfig(ctx, role: discord.Role = None, *, vanity: str = None):
    """Configure the role given when someone puts the server vanity in their status."""
    await ensure_guild(ctx.guild.id)
    if not role or not vanity:
        await ctx.send("Usage: `g!vanityconfig @role vanity_text`")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE guilds SET vanity_role = ?, vanity_string = ? WHERE guild_id = ?",
            (role.id, vanity, ctx.guild.id)
        )
        await db.commit()
    await ctx.send(f"✅ Vanity role {role.mention} for status containing `{vanity}`")

# ============================================================
# 📂 GEN (Keys & Premium)
# ============================================================
@bot.command(name="delkey")
@is_staff()
async def delkey(ctx, key_code: str = None):
    """Delete a key (it can no longer be redeemed, but does not affect who already used it)."""
    if not key_code:
        await ctx.send("Provide a key.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE keys SET revoked = 1 WHERE key_code = ? AND guild_id = ?", (key_code.upper(), ctx.guild.id))
        await db.commit()
    await ctx.send(f"✅ Key `{key_code}` deleted/revoked (no longer redeemable).")

@bot.command(name="drop")
@is_staff()
async def drop_cmd(ctx, service: str = None, amount: int = 1, channel: discord.TextChannel = None):
    """Gift accounts from the stock in a channel, with tag/mention outside the embed."""
    if not service:
        await ctx.send("Usage: `g!drop free/vip/boost [amount] [#channel]`")
        return
    service = service.lower()
    if service not in ("free", "vip", "boost"):
        await ctx.send("Service must be free, vip or boost.")
        return
    target = channel or ctx.channel
    accounts = []
    for _ in range(min(amount, 10)):
        acc = await take_account(ctx.guild.id, service)
        if not acc:
            break
        accounts.append(acc)
    if not accounts:
        await ctx.send("No stock available.")
        return
    # mention outside
    mention = "@everyone" if amount > 1 else ""
    emb = discord.Embed(title=f"🎁 Account Drop — {service.upper()}", color=0x00ff00)
    emb.description = "\n".join([f"`{a}`" for a in accounts])
    emb.set_footer(text=f"Dropped by {ctx.author}")
    # check drop mode
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT drop_mode, drop_webhook FROM guilds WHERE guild_id = ?", (ctx.guild.id,)) as cur:
            row = await cur.fetchone()
    if row and row[0] == "webhook" and row[1]:
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(row[1], session=session)
            await webhook.send(content=mention, embed=emb)
    else:
        await target.send(content=mention, embed=emb)
    await ctx.send(f"✅ Dropped {len(accounts)} account(s) in {target.mention}")

@bot.command(name="dropconfig")
@is_staff()
async def dropconfig(ctx, mode: str = None, webhook_url: str = None):
    """Configure how drops are sent (normal embed or custom webhook)."""
    await ensure_guild(ctx.guild.id)
    if not mode:
        await ctx.send("Usage: `g!dropconfig embed` or `g!dropconfig webhook <url>`")
        return
    mode = mode.lower()
    if mode not in ("embed", "webhook"):
        await ctx.send("Mode must be embed or webhook.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        if mode == "webhook":
            await db.execute(
                "UPDATE guilds SET drop_mode = ?, drop_webhook = ? WHERE guild_id = ?",
                (mode, webhook_url, ctx.guild.id)
            )
        else:
            await db.execute("UPDATE guilds SET drop_mode = ? WHERE guild_id = ?", (mode, ctx.guild.id))
        await db.commit()
    await ctx.send(f"✅ Drop mode set to `{mode}`")

@bot.command(name="keygen")
@is_staff()
async def keygen(ctx, tier: str = None, days: int = 30, uses: int = 1, amount: int = 1):
    """Generate a premium key (only valid in this server)."""
    if not tier:
        await ctx.send("Usage: `g!keygen free/vip/boost [days] [uses] [amount]`")
        return
    tier = tier.lower()
    if tier not in ("free", "vip", "boost"):
        await ctx.send("Tier must be free, vip or boost.")
        return
    keys = []
    async with aiosqlite.connect(DB_PATH) as db:
        for _ in range(min(amount, 20)):
            code = generate_key()
            await db.execute(
                "INSERT INTO keys (key_code, guild_id, tier, duration_days, max_uses, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (code, ctx.guild.id, tier, days, uses, ctx.author.id, time.time())
            )
            keys.append(code)
        await db.commit()
    text = "\n".join([f"`{k}`" for k in keys])
    emb = discord.Embed(title="Generated Keys", description=text, color=0x5865F2)
    emb.add_field(name="Tier", value=tier)
    emb.add_field(name="Duration", value=f"{days} days")
    emb.add_field(name="Uses", value=str(uses))
    await ctx.send(embed=emb)

@bot.command(name="keyinfo")
@is_staff()
async def keyinfo(ctx, key_code: str = None):
    """View information about a key."""
    if not key_code:
        await ctx.send("Provide a key.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM keys WHERE key_code = ? AND guild_id = ?", (key_code.upper(), ctx.guild.id)) as cur:
            row = await cur.fetchone()
    if not row:
        await ctx.send("Key not found.")
        return
    emb = discord.Embed(title=f"Key Info: {row[0]}", color=0x5865F2)
    emb.add_field(name="Tier", value=row[2])
    emb.add_field(name="Duration", value=f"{row[3]} days")
    emb.add_field(name="Uses", value=f"{row[5]}/{row[4]}")
    emb.add_field(name="Revoked", value="Yes" if row[8] else "No")
    emb.add_field(name="Created", value=datetime.fromtimestamp(row[7]).strftime("%Y-%m-%d %H:%M"))
    await ctx.send(embed=emb)

@bot.command(name="keys")
@is_staff()
async def keys_cmd(ctx, tier: str = None):
    """List keys generated in this server (optionally filter by tier)."""
    async with aiosqlite.connect(DB_PATH) as db:
        if tier:
            async with db.execute(
                "SELECT key_code, tier, duration_days, uses, max_uses, revoked FROM keys WHERE guild_id = ? AND tier = ? LIMIT 30",
                (ctx.guild.id, tier.lower())
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT key_code, tier, duration_days, uses, max_uses, revoked FROM keys WHERE guild_id = ? LIMIT 30",
                (ctx.guild.id,)
            ) as cur:
                rows = await cur.fetchall()
    if not rows:
        await ctx.send("No keys found.")
        return
    lines = [f"`{r[0]}` | {r[1]} | {r[2]}d | {r[3]}/{r[4]} | {'REV' if r[5] else 'OK'}" for r in rows]
    await ctx.send("**Keys:**\n" + "\n".join(lines))

@bot.command(name="premium")
@is_staff()
async def premium_cmd(ctx, member: discord.Member = None, tier: str = None, days: int = 30):
    """Manage premium time of a user in this server."""
    if not member or not tier:
        await ctx.send("Usage: `g!premium @user free/vip/boost [days]`")
        return
    tier = tier.lower()
    if tier not in ("free", "vip", "boost"):
        await ctx.send("Invalid tier.")
        return
    expires = time.time() + days * 86400
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO premium_users (guild_id, user_id, tier, expires_at, key_used) VALUES (?, ?, ?, ?, ?)",
            (ctx.guild.id, member.id, tier, expires, "manual")
        )
        await db.commit()
        # give role
        async with db.execute(
            f"SELECT premium_role_{tier} FROM guilds WHERE guild_id = ?",
            (ctx.guild.id,)
        ) as cur:
            row = await cur.fetchone()
    if row and row[0]:
        role = ctx.guild.get_role(row[0])
        if role:
            try:
                await member.add_roles(role)
            except:
                pass
    await ctx.send(f"✅ {member.mention} now has **{tier}** premium for {days} days.")

@bot.command(name="premtime")
async def premtime(ctx, member: discord.Member = None):
    """View your remaining premium time in this server."""
    target = member or ctx.author
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT tier, expires_at FROM premium_users WHERE guild_id = ? AND user_id = ?",
            (ctx.guild.id, target.id)
        ) as cur:
            row = await cur.fetchone()
    if not row or row[1] < time.time():
        await ctx.send(f"{target.mention} has no active premium.")
        return
    remaining = int(row[1] - time.time())
    days = remaining // 86400
    hours = (remaining % 86400) // 3600
    await ctx.send(f"**{target.display_name}** — Tier: **{row[0]}** | Remaining: **{days}d {hours}h**")

@bot.command(name="redeem")
async def redeem(ctx, key_code: str = None):
    """Redeem a premium key in this server."""
    if not key_code:
        await ctx.send("Provide a key.")
        return
    if await is_blacklisted(ctx.guild.id, ctx.author.id):
        await ctx.send("You are blacklisted.")
        return
    key_code = key_code.upper()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM keys WHERE key_code = ? AND guild_id = ?", (key_code, ctx.guild.id)) as cur:
            row = await cur.fetchone()
        if not row:
            await ctx.send("Invalid key.")
            return
        if row[8]:  # revoked
            await ctx.send("This key has been revoked.")
            return
        if row[5] >= row[4]:
            await ctx.send("This key has no uses left.")
            return
        tier = row[2]
        days = row[3]
        expires = time.time() + days * 86400
        await db.execute("UPDATE keys SET uses = uses + 1 WHERE key_code = ?", (key_code,))
        await db.execute(
            "INSERT OR REPLACE INTO premium_users (guild_id, user_id, tier, expires_at, key_used) VALUES (?, ?, ?, ?, ?)",
            (ctx.guild.id, ctx.author.id, tier, expires, key_code)
        )
        await db.commit()
        async with db.execute(f"SELECT premium_role_{tier} FROM guilds WHERE guild_id = ?", (ctx.guild.id,)) as cur:
            r = await cur.fetchone()
    if r and r[0]:
        role = ctx.guild.get_role(r[0])
        if role:
            try:
                await ctx.author.add_roles(role)
            except:
                pass
    await ctx.send(f"✅ Key redeemed! You now have **{tier}** premium for **{days} days**.")

@bot.command(name="revokekey")
@is_staff()
async def revokekey(ctx, key_code: str = None):
    """Fully revoke a key: invalidate it and remove premium (and role) from everyone who used it in this server."""
    if not key_code:
        await ctx.send("Provide a key.")
        return
    key_code = key_code.upper()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE keys SET revoked = 1 WHERE key_code = ? AND guild_id = ?", (key_code, ctx.guild.id))
        async with db.execute(
            "SELECT user_id, tier FROM premium_users WHERE guild_id = ? AND key_used = ?",
            (ctx.guild.id, key_code)
        ) as cur:
            users = await cur.fetchall()
        await db.execute("DELETE FROM premium_users WHERE guild_id = ? AND key_used = ?", (ctx.guild.id, key_code))
        await db.commit()
    for uid, tier in users:
        member = ctx.guild.get_member(uid)
        if member:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(f"SELECT premium_role_{tier} FROM guilds WHERE guild_id = ?", (ctx.guild.id,)) as cur:
                    r = await cur.fetchone()
            if r and r[0]:
                role = ctx.guild.get_role(r[0])
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role)
                    except:
                        pass
    await ctx.send(f"✅ Key `{key_code}` fully revoked. Premium removed from {len(users)} user(s).")

# ============================================================
# 📂 GENERAL
# ============================================================
@bot.command(name="add")
@is_staff()
async def add_stock(ctx, service: str = None, service_name: str = None):
    """Add or replace stock. Attach a .txt file with accounts (one per line)."""
    if not service or not service_name:
        await ctx.send("Usage: `g!add free/vip/boost service_name` + attach a .txt file")
        return
    service = service.lower()
    if service not in ("free", "vip", "boost"):
        await ctx.send("Service must be free, vip or boost.")
        return
    if not ctx.message.attachments:
        await ctx.send("Attach a text file with accounts (email:pass or token per line).")
        return
    att = ctx.message.attachments[0]
    data = await att.read()
    content = data.decode("utf-8", errors="ignore").strip()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO stock (guild_id, service, service_name, accounts) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, service, service_name) DO UPDATE SET accounts = excluded.accounts",
            (ctx.guild.id, service, service_name.lower(), content)
        )
        await db.commit()
    count = len([l for l in content.splitlines() if l.strip()])
    await ctx.send(f"✅ Stock updated for **{service}/{service_name}** — {count} accounts.")

@bot.command(name="alertstock")
@is_staff()
async def alertstock(ctx, service: str = None, threshold: int = 10):
    """Alert when stock for a service falls below threshold. (simple check)"""
    if not service:
        await ctx.send("Usage: `g!alertstock free/vip/boost [threshold]`")
        return
    service = service.lower()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT service_name, accounts FROM stock WHERE guild_id = ? AND service = ?",
            (ctx.guild.id, service)
        ) as cur:
            rows = await cur.fetchall()
    low = []
    for name, accs in rows:
        count = len([l for l in (accs or "").splitlines() if l.strip()])
        if count <= threshold:
            low.append(f"**{name}**: {count}")
    if not low:
        await ctx.send(f"All {service} services are above {threshold}.")
    else:
        await ctx.send("⚠️ Low stock:\n" + "\n".join(low))

@bot.command(name="botrename")
@is_staff()
async def botrename(ctx, *, nickname: str = None):
    """Change the bot's nickname per server."""
    if not nickname:
        await ctx.send("Provide a new nickname.")
        return
    try:
        await ctx.guild.me.edit(nick=nickname[:32])
        await ctx.send(f"✅ Nickname changed to **{nickname[:32]}**")
    except Exception as e:
        await ctx.send(f"Failed: {e}")

@bot.command(name="create")
@is_staff()
async def create_service(ctx, service: str = None, service_name: str = None):
    """Create a new service (free, vip or boost)."""
    if not service or not service_name:
        await ctx.send("Usage: `g!create free/vip/boost service_name`")
        return
    service = service.lower()
    if service not in ("free", "vip", "boost"):
        await ctx.send("Must be free, vip or boost.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO stock (guild_id, service, service_name, accounts) VALUES (?, ?, ?, ?)",
            (ctx.guild.id, service, service_name.lower(), "")
        )
        await db.commit()
    await ctx.send(f"✅ Service **{service}/{service_name}** created. Use `g!add` to add stock.")

@bot.command(name="customize")
@is_owner()
async def customize(ctx, what: str = None):
    """Customize bot avatar, banner and bio. Attach image for avatar/banner. Bio: g!customize bio text..."""
    if not what:
        await ctx.send("Usage: `g!customize avatar` (attach image) | `g!customize banner` | `g!customize bio text`")
        return
    what = what.lower()
    if what == "avatar":
        if not ctx.message.attachments:
            await ctx.send("Attach an image.")
            return
        data = await ctx.message.attachments[0].read()
        await bot.user.edit(avatar=data)
        await ctx.send("✅ Avatar updated.")
    elif what == "banner":
        if not ctx.message.attachments:
            await ctx.send("Attach an image.")
            return
        data = await ctx.message.attachments[0].read()
        await bot.user.edit(banner=data)
        await ctx.send("✅ Banner updated.")
    elif what == "bio":
        text = ctx.message.content.split("bio", 1)[-1].strip()
        # note: discord.py does not support setting bio easily via bot user; this is placeholder
        await ctx.send(f"Bio set request received (manual for now): {text[:190]}")
    else:
        await ctx.send("avatar / banner / bio")

@bot.command(name="delstock")
@is_staff()
async def delstock(ctx, service: str = None, service_name: str = None):
    """Delete a complete stock file (service) from the server."""
    if not service or not service_name:
        await ctx.send("Usage: `g!delstock free/vip/boost service_name`")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM stock WHERE guild_id = ? AND service = ? AND service_name = ?",
            (ctx.guild.id, service.lower(), service_name.lower())
        )
        await db.commit()
    await ctx.send(f"✅ Stock **{service}/{service_name}** deleted.")

@bot.command(name="gen")
async def gen_cmd(ctx, service_name: str = None):
    """Generate an account from FREE, VIP or BOOST stock depending on the channel."""
    if await is_blacklisted(ctx.guild.id, ctx.author.id):
        await ctx.send("You are blacklisted from generators.")
        return
    await ensure_guild(ctx.guild.id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT free_channel, vip_channel, boost_channel FROM guilds WHERE guild_id = ?",
            (ctx.guild.id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        await ctx.send("Generator channels not configured. Use `g!setgens`.")
        return
    free_ch, vip_ch, boost_ch = row
    channel_id = ctx.channel.id
    if channel_id == free_ch:
        service = "free"
    elif channel_id == vip_ch:
        service = "vip"
    elif channel_id == boost_ch:
        service = "boost"
    else:
        await ctx.send("This channel is not a generator channel.")
        return

    # tier check for vip/boost
    user_tier = await get_user_tier(ctx.guild.id, ctx.author.id)
    if service == "vip" and user_tier not in ("vip", "boost"):
        await ctx.send("You need VIP or higher premium to use this generator.")
        return
    if service == "boost" and user_tier != "boost":
        await ctx.send("You need BOOST premium to use this generator.")
        return

    remaining = await check_cooldown(ctx.guild.id, ctx.author.id, service)
    if remaining > 0:
        await ctx.send(f"⏳ Cooldown: **{remaining}s** remaining.")
        return

    account = await take_account(ctx.guild.id, service, service_name.lower() if service_name else None)
    if not account:
        await ctx.send("No stock available for this service.")
        return

    await set_cooldown(ctx.guild.id, ctx.author.id, service)

    try:
        emb = discord.Embed(title=f"✅ Generated — {service.upper()}", color=0x00ff00)
        emb.add_field(name="Account", value=f"```{account}```")
        emb.set_footer(text=f"Requested by {ctx.author}")
        await ctx.author.send(embed=emb)
        await ctx.send(f"{ctx.author.mention} check your DMs! ✅")
    except discord.Forbidden:
        await ctx.send(f"{ctx.author.mention} open your DMs. Here is your account:\n```{account}```")

@bot.command(name="help")
async def help_cmd(ctx, category: str = None):
    """Show the list of commands."""
    emb = discord.Embed(title="Generator Bot — Help", color=0x5865F2, description=f"Prefix: `{ctx.prefix}`")
    cats = {
        "Admin": ["blacklist"],
        "Community": ["feedback"],
        "Config": ["feedbackconfig", "premconfig", "serverprofile", "setprefix", "vanityconfig"],
        "Gen": ["delkey", "drop", "dropconfig", "keygen", "keyinfo", "keys", "premium", "premtime", "redeem", "revokekey"],
        "General": ["add", "alertstock", "botrename", "create", "customize", "delstock", "gen", "help", "info", "invite",
                    "logs", "report", "sendsupport", "servervs", "setcooldown", "setgens", "stock", "suggest", "topservers", "uso"]
    }
    if category and category.lower() in [c.lower() for c in cats]:
        for c, cmds in cats.items():
            if c.lower() == category.lower():
                emb.add_field(name=c, value="\n".join([f"`{ctx.prefix}{cmd}`" for cmd in cmds]), inline=False)
    else:
        for c, cmds in cats.items():
            emb.add_field(name=c, value=", ".join([f"`{cmd}`" for cmd in cmds]), inline=False)
        emb.set_footer(text=f"Use {ctx.prefix}help <category> for details")
    await ctx.send(embed=emb)

@bot.command(name="info")
async def info_cmd(ctx):
    """Show bot information."""
    emb = discord.Embed(title="Bot Info", color=0x5865F2)
    emb.add_field(name="Servers", value=str(len(bot.guilds)))
    emb.add_field(name="Latency", value=f"{round(bot.latency*1000)}ms")
    emb.add_field(name="Prefix", value=DEFAULT_PREFIX)
    emb.set_footer(text="Account Generator Bot")
    await ctx.send(embed=emb)

@bot.command(name="invite")
async def invite_cmd(ctx):
    """Generate the invite link to add the bot to your server."""
    url = discord.utils.oauth_url(bot.user.id, permissions=discord.Permissions(administrator=True))
    await ctx.send(f"Invite me:\n{url}")

@bot.command(name="logs")
@is_staff()
async def logs_cmd(ctx, channel: discord.TextChannel = None):
    """Logging system — set the logs channel."""
    await ensure_guild(ctx.guild.id)
    if not channel:
        await ctx.send("Mention a channel for logs.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE guilds SET logs_channel = ? WHERE guild_id = ?", (channel.id, ctx.guild.id))
        await db.commit()
    await ctx.send(f"✅ Logs channel set to {channel.mention}")

@bot.command(name="report")
async def report_cmd(ctx, *, content: str = None):
    """Report a problem or suggestion to the bot owner."""
    if not content:
        await ctx.send("Write your report after the command.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO reports (guild_id, user_id, content, created_at) VALUES (?, ?, ?, ?)",
            (ctx.guild.id, ctx.author.id, content, time.time())
        )
        await db.commit()
    for oid in OWNER_IDS:
        user = bot.get_user(oid)
        if user:
            try:
                await user.send(f"**Report from {ctx.author} ({ctx.guild.name}):**\n{content}")
            except:
                pass
    await ctx.send("✅ Report sent to the owner.")

@bot.command(name="sendsupport")
@is_owner()
async def sendsupport(ctx, target: str = None, *, message: str = None):
    """Send a support message to a specific server or to all (broadcast)."""
    if not target or not message:
        await ctx.send("Usage: `g!sendsupport <guild_id|all> message`")
        return
    if target.lower() == "all":
        count = 0
        for g in bot.guilds:
            try:
                ch = g.system_channel or (g.text_channels[0] if g.text_channels else None)
                if ch:
                    await ch.send(f"**Support Message:**\n{message}")
                    count += 1
            except:
                pass
        await ctx.send(f"✅ Broadcast sent to {count} servers.")
    else:
        try:
            gid = int(target)
            g = bot.get_guild(gid)
            if not g:
                await ctx.send("Guild not found.")
                return
            ch = g.system_channel or (g.text_channels[0] if g.text_channels else None)
            if ch:
                await ch.send(f"**Support Message:**\n{message}")
                await ctx.send("✅ Sent.")
        except:
            await ctx.send("Invalid guild id.")

@bot.command(name="servervs")
@is_owner()
async def servervs(ctx):
    """Hidden owner command — list all servers."""
    lines = [f"{g.name} ({g.id}) — {g.member_count} members" for g in bot.guilds]
    # split if long
    for i in range(0, len(lines), 20):
        await ctx.send("```\n" + "\n".join(lines[i:i+20]) + "\n```")

@bot.command(name="setcooldown")
@is_staff()
async def setcooldown(ctx, target: str = None, seconds: int = None):
    """Set the wait time per service or for all."""
    if not target or seconds is None:
        await ctx.send("Usage: `g!setcooldown free/vip/boost/all <seconds>`")
        return
    target = target.lower()
    await ensure_guild(ctx.guild.id)
    async with aiosqlite.connect(DB_PATH) as db:
        if target == "all":
            await db.execute("UPDATE guilds SET cooldown_all = ? WHERE guild_id = ?", (seconds, ctx.guild.id))
        elif target in ("free", "vip", "boost"):
            await db.execute(f"UPDATE guilds SET cooldown_{target} = ? WHERE guild_id = ?", (seconds, ctx.guild.id))
        else:
            await ctx.send("Target must be free, vip, boost or all.")
            return
        await db.commit()
    await ctx.send(f"✅ Cooldown for `{target}` set to **{seconds}s**")

@bot.command(name="setgens")
@is_staff()
async def setgens(ctx, service: str = None, channel: discord.TextChannel = None):
    """Set the channel where free, vip or boost gens will run."""
    if not service or not channel:
        await ctx.send("Usage: `g!setgens free/vip/boost #channel`")
        return
    service = service.lower()
    if service not in ("free", "vip", "boost"):
        await ctx.send("Must be free, vip or boost.")
        return
    await ensure_guild(ctx.guild.id)
    col = f"{service}_channel"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE guilds SET {col} = ? WHERE guild_id = ?", (channel.id, ctx.guild.id))
        await db.commit()
    await ctx.send(f"✅ {service.upper()} generator channel set to {channel.mention}")

@bot.command(name="stock")
async def stock_cmd(ctx, service: str = None):
    """Show stock by type."""
    await ensure_guild(ctx.guild.id)
    async with aiosqlite.connect(DB_PATH) as db:
        if service:
            async with db.execute(
                "SELECT service_name, accounts FROM stock WHERE guild_id = ? AND service = ?",
                (ctx.guild.id, service.lower())
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT service, service_name, accounts FROM stock WHERE guild_id = ?",
                (ctx.guild.id,)
            ) as cur:
                rows = await cur.fetchall()
    if not rows:
        await ctx.send("No stock.")
        return
    emb = discord.Embed(title="Stock", color=0x5865F2)
    for r in rows:
        if service:
            name, accs = r
            count = len([l for l in (accs or "").splitlines() if l.strip()])
            emb.add_field(name=name, value=str(count), inline=True)
        else:
            serv, name, accs = r
            count = len([l for l in (accs or "").splitlines() if l.strip()])
            emb.add_field(name=f"{serv}/{name}", value=str(count), inline=True)
    await ctx.send(embed=emb)

@bot.command(name="suggest")
async def suggest_cmd(ctx, *, content: str = None):
    """Send suggestions to the bot owner."""
    if not content:
        await ctx.send("Write your suggestion.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO suggestions (guild_id, user_id, content, created_at) VALUES (?, ?, ?, ?)",
            (ctx.guild.id, ctx.author.id, content, time.time())
        )
        await db.commit()
    for oid in OWNER_IDS:
        user = bot.get_user(oid)
        if user:
            try:
                await user.send(f"**Suggestion from {ctx.author}:**\n{content}")
            except:
                pass
    await ctx.send("✅ Suggestion sent.")

@bot.command(name="topservers")
async def topservers(ctx):
    """Show the servers with the most generation."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT guild_id, SUM(count) as total FROM gen_stats GROUP BY guild_id ORDER BY total DESC LIMIT 10"
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        await ctx.send("No data yet.")
        return
    lines = []
    for i, (gid, total) in enumerate(rows, 1):
        g = bot.get_guild(gid)
        name = g.name if g else str(gid)
        lines.append(f"**{i}.** {name} — {total} gens")
    emb = discord.Embed(title="Top Servers by Generation", description="\n".join(lines), color=0x5865F2)
    await ctx.send(embed=emb)

@bot.command(name="uso")
async def uso_cmd(ctx):
    """Show generation statistics and cooldown."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT service, count FROM gen_stats WHERE guild_id = ?",
            (ctx.guild.id,)
        ) as cur:
            stats = await cur.fetchall()
        async with db.execute(
            "SELECT cooldown_free, cooldown_vip, cooldown_boost, cooldown_all FROM guilds WHERE guild_id = ?",
            (ctx.guild.id,)
        ) as cur:
            cds = await cur.fetchone()
    emb = discord.Embed(title="Usage Stats", color=0x5865F2)
    if stats:
        for s, c in stats:
            emb.add_field(name=s.upper(), value=str(c), inline=True)
    else:
        emb.description = "No generations yet."
    if cds:
        emb.add_field(name="Cooldowns", value=f"Free: {cds[0]}s | VIP: {cds[1]}s | Boost: {cds[2]}s | All: {cds[3]}s", inline=False)
    await ctx.send(embed=emb)

# ==================== RUN ====================
if __name__ == "__main__":
    bot.run(TOKEN)