#!/usr/bin/env python3
"""
Chromie - Discord Countdown & Event Bot
Production-ready implementation with hybrid monetization model
Updated to match full spec while preserving working intents configuration
"""

import os
import json
import traceback
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any, Tuple
import time
import asyncio
import uuid
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.errors import NotFound, Forbidden, HTTPException
from threading import Lock

# ============================================================================
# CONFIGURATION
# ============================================================================

VERSION = "2.0.0-spec"
DEFAULT_TZ = ZoneInfo("America/Chicago")
UPDATE_INTERVAL_SECONDS = 60
DEFAULT_MILESTONES = [100, 60, 30, 14, 7, 2, 1, 0]

DATA_FILE = Path(os.getenv("CHROMIE_DATA_PATH", "/var/data/chromie_state.json"))
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()

TOPGG_TOKEN = os.getenv("TOPGG_TOKEN", "").strip()
TOPGG_BOT_ID = os.getenv("TOPGG_BOT_ID", "").strip()
TOPGG_FAIL_OPEN = False

EMBED_COLOR = discord.Color.from_rgb(140, 82, 255)
VOTE_DURATION_HOURS = 12

_STATE_LOCK = Lock()
_vote_cache: Dict[int, Tuple[float, bool]] = {}  # user_id -> (cached_at, voted)
VOTE_CACHE_TTL_SECONDS = 60

BOT_OWNER_ID = None  # Set from app info

# ============================================================================
# LOGGING
# ============================================================================

LOG_THROTTLE_SECONDS = 60 * 30
_last_log = {}

def log_throttled(guild_id: int, code: str, msg: str):
    key = (guild_id, code)
    now = time.time()
    last = _last_log.get(key, 0)
    if now - last >= LOG_THROTTLE_SECONDS:
        _last_log[key] = now
        print(msg)

def log_info(msg: str):
    print(f"[INFO] {msg}")

def log_error(msg: str):
    print(f"[ERROR] {msg}")

# ============================================================================
# STATE MANAGEMENT
# ============================================================================

state: Dict[str, Any] = {
    "guilds": {}
}

def get_default_guild_state() -> Dict[str, Any]:
    """Returns a fresh guild state structure."""
    return {
        "event_channel_id": 0,
        "pinned_message_id": 0,
        "timezone": str(DEFAULT_TZ),
        "events": [],
        "settings": {
            "theme": "classic",
            "show_milestones": True,
            "milestones": DEFAULT_MILESTONES.copy(),
            "board_count": 1  # Pro feature
        },
        "supporter": {
            "last_vote_at": None,
            "vote_until": None
        },
        "pro": {
            "pro_active": False,
            "pro_until": None,
            "grace_until": None,
            "migration_mode": True  # Soft launch mode
        },
        "welcomed": False
    }

def get_guild_state(guild_id: int) -> Dict[str, Any]:
    """Get or create guild state."""
    guild_id_str = str(guild_id)
    if guild_id_str not in state["guilds"]:
        state["guilds"][guild_id_str] = get_default_guild_state()
    return state["guilds"][guild_id_str]

def save_state():
    """Save state to JSON file with locking."""
    with _STATE_LOCK:
        try:
            DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(DATA_FILE, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log_error(f"Failed to save state: {e}")

def load_state():
    """Load state from JSON file."""
    global state
    with _STATE_LOCK:
        try:
            if DATA_FILE.exists():
                with open(DATA_FILE, 'r') as f:
                    loaded = json.load(f)
                    state.update(loaded)
                log_info(f"State loaded from {DATA_FILE}")
            else:
                log_info("No existing state file, starting fresh")
        except Exception as e:
            log_error(f"Failed to load state: {e}")

# ============================================================================
# GATING FUNCTIONS
# ============================================================================

def has_active_vote(guild_state: Dict[str, Any]) -> bool:
    """
    Check if guild has an active vote (supporter status).
    Returns True if vote_until is in the future.
    """
    vote_until_str = guild_state["supporter"].get("vote_until")
    if vote_until_str:
        try:
            vote_until = datetime.fromisoformat(vote_until_str)
            if datetime.utcnow() < vote_until:
                return True
        except:
            pass
    return False

def is_pro(guild_state: Dict[str, Any]) -> bool:
    """
    Check if guild has active Pro status.
    Returns True if:
    - pro_active is True AND pro_until is in future, OR
    - grace_until is in future (migration grace period), OR
    - migration_mode is True (soft rollout)
    """
    pro_data = guild_state.get("pro", {})
    
    # Check active subscription
    if pro_data.get("pro_active", False):
        pro_until_str = pro_data.get("pro_until")
        if pro_until_str:
            try:
                pro_until = datetime.fromisoformat(pro_until_str)
                if datetime.utcnow() < pro_until:
                    return True
            except:
                pass
    
    # Check grace period
    grace_until_str = pro_data.get("grace_until")
    if grace_until_str:
        try:
            grace_until = datetime.fromisoformat(grace_until_str)
            if datetime.utcnow() < grace_until:
                return True
        except:
            pass
    
    # Migration mode allows all guilds
    if pro_data.get("migration_mode", False):
        return True
    
    return False

def get_supporter_status_text(guild_state: Dict[str, Any]) -> str:
    """Get formatted supporter status for display."""
    if has_active_vote(guild_state):
        vote_until_str = guild_state["supporter"].get("vote_until")
        if vote_until_str:
            try:
                vote_until = datetime.fromisoformat(vote_until_str)
                remaining = vote_until - datetime.utcnow()
                hours = int(remaining.total_seconds() / 3600)
                return f"✅ Supporter Active ({hours}h remaining)"
            except:
                pass
        return "✅ Supporter Active"
    return "🔒 Supporter Locked"

def get_pro_status_text(guild_state: Dict[str, Any]) -> str:
    """Get formatted Pro status for display."""
    if is_pro(guild_state):
        pro_data = guild_state.get("pro", {})
        if pro_data.get("migration_mode", False):
            return "✅ Pro Active (Migration Mode)"
        if pro_data.get("grace_until"):
            return "✅ Pro Active (Grace Period)"
        return "✅ Pro Active"
    return "🔒 Pro Locked"

# ============================================================================
# TOP.GG INTEGRATION
# ============================================================================

async def topgg_has_voted(user_id: int, *, force: bool = False) -> bool:
    """Check if user has voted on Top.gg (with caching)."""
    now = time.monotonic()
    cached = _vote_cache.get(user_id)
    
    if (not force) and cached and (now - cached[0] <= VOTE_CACHE_TTL_SECONDS):
        return cached[1]
    
    if not TOPGG_TOKEN or not TOPGG_BOT_ID:
        voted = True if TOPGG_FAIL_OPEN else False
        _vote_cache[user_id] = (now, voted)
        return voted
    
    url = f"https://top.gg/api/bots/{TOPGG_BOT_ID}/check"
    headers = {"Authorization": TOPGG_TOKEN.strip()}
    params = {"userId": str(user_id)}
    timeout = aiohttp.ClientTimeout(total=6)
    
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    voted = bool(int(data.get("voted", 0) or 0))
                else:
                    voted = True if TOPGG_FAIL_OPEN else False
    except Exception:
        voted = True if TOPGG_FAIL_OPEN else False
    
    _vote_cache[user_id] = (now, voted)
    return voted

async def update_vote_status(guild_state: Dict[str, Any], user_id: int) -> bool:
    """Update guild vote status based on user vote check."""
    voted = await topgg_has_voted(user_id, force=True)
    if voted:
        now = datetime.utcnow()
        vote_until = now + timedelta(hours=VOTE_DURATION_HOURS)
        guild_state["supporter"]["last_vote_at"] = now.isoformat()
        guild_state["supporter"]["vote_until"] = vote_until.isoformat()
        save_state()
        return True
    return False

def build_vote_view() -> discord.ui.View:
    """Build a view with vote button."""
    view = discord.ui.View()
    url = f"https://top.gg/bot/{TOPGG_BOT_ID}/vote" if TOPGG_BOT_ID else "https://top.gg"
    view.add_item(discord.ui.Button(label="Vote on Top.gg", style=discord.ButtonStyle.link, url=url))
    return view

async def send_vote_required(interaction: discord.Interaction, feature_label: str):
    """Send vote required message."""
    content = (
        f"🗳️ **Vote required** to use **{feature_label}**.\n"
        "Vote on Top.gg to unlock supporter features!"
    )
    view = build_vote_view()
    
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True, view=view)
        else:
            await interaction.response.send_message(content, ephemeral=True, view=view)
    except (NotFound, HTTPException):
        pass

# ============================================================================
# DISCORD HELPERS
# ============================================================================

async def safe_defer(interaction: discord.Interaction, ephemeral: bool = False):
    """Safely defer an interaction if not already responded."""
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral)
    except:
        pass

async def safe_send(interaction: discord.Interaction, content: str = None, embed: discord.Embed = None, ephemeral: bool = False, view: discord.ui.View = None):
    """Safely send a response or followup."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral, view=view)
        else:
            await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral, view=view)
    except Exception as e:
        log_error(f"Failed to send message: {e}")

async def retry_discord_request(coro, max_retries: int = 3):
    """Retry a Discord API request with exponential backoff."""
    for attempt in range(max_retries):
        try:
            return await coro
        except HTTPException as e:
            if e.status in [500, 502, 503, 504] and attempt < max_retries - 1:
                wait = 2 ** attempt
                log_info(f"Discord API error {e.status}, retrying in {wait}s...")
                await asyncio.sleep(wait)
            else:
                raise
        except asyncio.TimeoutError:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                log_info(f"Timeout, retrying in {wait}s...")
                await asyncio.sleep(wait)
            else:
                raise

# ============================================================================
# EVENT MANAGEMENT
# ============================================================================

def create_event(
    name: str,
    datetime_iso: str,
    timezone: str,
    created_by: int,
    description: str = "",
    repeat_type: str = "none",
    repeat_interval: int = 1,
    notify_role_id: int = 0,
    notify_channel_id: int = 0,
    milestones: List[int] = None
) -> Dict[str, Any]:
    """Create a new event dictionary."""
    now = datetime.utcnow().isoformat()
    event_id = str(uuid.uuid4())
    
    if milestones is None:
        milestones = DEFAULT_MILESTONES.copy()
    
    milestone_fired = {str(m): False for m in milestones}
    
    return {
        "id": event_id,
        "name": name,
        "datetime_iso": datetime_iso,
        "timezone": timezone,
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
        "description": description,
        "banner_url": "",
        "repeat": {
            "type": repeat_type,
            "interval": repeat_interval
        },
        "milestones": milestones,
        "milestone_fired": milestone_fired,
        "notify_role_id": notify_role_id,
        "notify_channel_id": notify_channel_id
    }

def get_event_datetime(event: Dict[str, Any]) -> datetime:
    """Parse event datetime with timezone."""
    dt = datetime.fromisoformat(event["datetime_iso"])
    tz = ZoneInfo(event["timezone"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt

def advance_repeating_event(event: Dict[str, Any]):
    """Advance a repeating event to its next occurrence."""
    repeat_type = event["repeat"]["type"]
    if repeat_type == "none":
        return
    
    interval = event["repeat"]["interval"]
    dt = get_event_datetime(event)
    
    if repeat_type == "daily":
        dt += timedelta(days=interval)
    elif repeat_type == "weekly":
        dt += timedelta(weeks=interval)
    elif repeat_type == "monthly":
        month = dt.month + interval
        year = dt.year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        dt = dt.replace(year=year, month=month)
    elif repeat_type == "yearly":
        dt = dt.replace(year=dt.year + interval)
    
    event["datetime_iso"] = dt.isoformat()
    event["updated_at"] = datetime.utcnow().isoformat()
    
    # Reset milestone fired states
    for key in event["milestone_fired"]:
        event["milestone_fired"][key] = False

def find_event(guild_state: Dict[str, Any], identifier: str) -> Optional[Dict[str, Any]]:
    """Find event by ID or name (case-insensitive)."""
    for event in guild_state["events"]:
        if event["id"] == identifier:
            return event
        if event["name"].lower() == identifier.lower():
            return event
    return None

def sort_events(guild_state: Dict[str, Any]):
    """Sort events by datetime."""
    try:
        guild_state["events"].sort(key=lambda e: get_event_datetime(e))
    except:
        pass

# ============================================================================
# PINNED EMBED
# ============================================================================

def build_pinned_embed(guild_state: Dict[str, Any], guild_name: str) -> discord.Embed:
    """Build the main pinned embed for a guild using DYNAMIC timestamps."""
    embed = discord.Embed(
        title=f"📅 {guild_name} Event Countdown",
        description=f"**Chromie v{VERSION}**",
        color=EMBED_COLOR
    )
    
    # Status indicators
    supporter_status = get_supporter_status_text(guild_state)
    pro_status = get_pro_status_text(guild_state)
    embed.add_field(name="Status", value=f"{supporter_status}\n{pro_status}", inline=False)
    
    # Events
    events = guild_state["events"]
    if not events:
        embed.add_field(name="No Events", value="Use `/addevent` to create your first event!", inline=False)
    else:
        # Sort by datetime
        sorted_events = sorted(events, key=lambda e: get_event_datetime(e))
        
        for event in sorted_events[:10]:  # Limit to 10 events
            dt = get_event_datetime(event)
            unix_timestamp = int(dt.timestamp())
            
            value_lines = [
                f"**When:** <t:{unix_timestamp}:F>",
                f"**Countdown:** <t:{unix_timestamp}:R>"
            ]
            
            if event["description"]:
                desc = event["description"][:100]
                if len(event["description"]) > 100:
                    desc += "..."
                value_lines.append(f"*{desc}*")
            
            value = "\n".join(value_lines)
            embed.add_field(name=f"📌 {event['name']}", value=value, inline=False)
            
            # Add banner if present (only first event with banner)
            if event["banner_url"] and not embed.image:
                embed.set_image(url=event["banner_url"])
    
    embed.set_footer(text="Times update automatically • Use /chronohelp for commands")
    return embed

async def update_pinned_message(bot: commands.Bot, guild_id: int, force: bool = False):
    """Update or create the pinned message for a guild (ONLY when state changes)."""
    guild_state = get_guild_state(guild_id)
    channel_id = guild_state["event_channel_id"]
    
    if not channel_id:
        return
    
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    
    channel = guild.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        return
    
    try:
        embed = build_pinned_embed(guild_state, guild.name)
        
        # Try to get existing pinned message
        message_id = guild_state["pinned_message_id"]
        message = None
        
        if message_id:
            try:
                message = await retry_discord_request(channel.fetch_message(message_id))
            except NotFound:
                log_info(f"Pinned message not found in guild {guild_id}, creating new one")
                message = None
        
        if message:
            # Edit existing message
            await retry_discord_request(message.edit(embed=embed))
            log_info(f"Updated pinned message in guild {guild_id}")
        else:
            # Create new message
            message = await retry_discord_request(channel.send(embed=embed))
            try:
                await message.pin()
            except Forbidden:
                log_info(f"Cannot pin message in guild {guild_id} (missing permissions)")
            
            guild_state["pinned_message_id"] = message.id
            save_state()
            log_info(f"Created new pinned message in guild {guild_id}")
    
    except Exception as e:
        log_error(f"Failed to update pinned message in guild {guild_id}: {e}")

# ============================================================================
# BACKGROUND TASKS
# ============================================================================

async def check_events(bot: commands.Bot):
    """Check all events for milestones and start times (ONLY update pinned on changes)."""
    for guild_id_str, guild_state in state["guilds"].items():
        guild_id = int(guild_id_str)
        guild = bot.get_guild(guild_id)
        if not guild:
            continue
        
        needs_update = False
        now = datetime.utcnow()
        
        for event in guild_state["events"][:]:
            dt = get_event_datetime(event)
            time_until = (dt - now.replace(tzinfo=dt.tzinfo)).total_seconds()
            days_until = time_until / 86400
            
            # Check if event has started
            if time_until <= 0 and not event["milestone_fired"].get("0", False):
                needs_update = True
                event["milestone_fired"]["0"] = True
                
                # Send notification
                notify_channel_id = event["notify_channel_id"] or guild_state["event_channel_id"]
                if notify_channel_id:
                    channel = guild.get_channel(notify_channel_id)
                    if channel:
                        role_mention = ""
                        if event["notify_role_id"]:
                            role = guild.get_role(event["notify_role_id"])
                            if role:
                                role_mention = f"{role.mention} "
                        
                        try:
                            await channel.send(f"🎉 {role_mention}**{event['name']}** has started!")
                        except Exception as e:
                            log_error(f"Failed to send event start notification: {e}")
                
                # Handle repeating events
                if event["repeat"]["type"] != "none":
                    advance_repeating_event(event)
                    log_info(f"Advanced repeating event {event['id']} in guild {guild_id}")
            
            # Check milestones
            if guild_state["settings"]["show_milestones"]:
                for milestone in event["milestones"]:
                    milestone_key = str(milestone)
                    if not event["milestone_fired"].get(milestone_key, False):
                        if days_until <= milestone and days_until > 0:
                            needs_update = True
                            event["milestone_fired"][milestone_key] = True
                            
                            # Send milestone notification
                            notify_channel_id = event["notify_channel_id"] or guild_state["event_channel_id"]
                            if notify_channel_id:
                                channel = guild.get_channel(notify_channel_id)
                                if channel:
                                    try:
                                        await channel.send(
                                            f"⏰ **{event['name']}** is {milestone} day{'s' if milestone != 1 else ''} away!"
                                        )
                                    except Exception as e:
                                        log_error(f"Failed to send milestone notification: {e}")
        
        if needs_update:
            save_state()
            await update_pinned_message(bot, guild_id)
            await asyncio.sleep(0.5)  # Per-guild pacing

# ============================================================================
# BOT SETUP
# ============================================================================

intents = discord.Intents.default()
intents.guilds = True
# NO message_content intent needed for slash commands

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    """Bot ready event."""
    global BOT_OWNER_ID
    
    app_info = await bot.application_info()
    BOT_OWNER_ID = app_info.owner.id
    
    log_info(f"Logged in as {bot.user.name} (ID: {bot.user.id})")
    log_info(f"Bot owner: {BOT_OWNER_ID}")
    log_info(f"Guilds: {len(bot.guilds)}")
    
    load_state()
    
    try:
        synced = await bot.tree.sync()
        log_info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        log_error(f"Failed to sync commands: {e}")
    
    if not event_check_loop.is_running():
        event_check_loop.start()

@tasks.loop(seconds=UPDATE_INTERVAL_SECONDS)
async def event_check_loop():
    """Background task to check events (does NOT edit pinned every loop)."""
    try:
        await check_events(bot)
    except Exception as e:
        log_error(f"Error in event check loop: {e}\n{traceback.format_exc()}")

# ============================================================================
# COMMANDS - GENERAL / FREE
# ============================================================================

@bot.tree.command(name="chronohelp", description="Show Chromie help and commands")
async def chronohelp_command(interaction: discord.Interaction):
    """Display help information."""
    embed = discord.Embed(
        title="📚 Chromie Help",
        description=f"**Version {VERSION}** - Event Countdown Bot",
        color=EMBED_COLOR
    )
    
    embed.add_field(
        name="🆓 Free Commands",
        value=(
            "`/setup` - Configure countdown channel\n"
            "`/addevent` - Create a new event\n"
            "`/editevent` - Modify an event\n"
            "`/deleteevent` - Remove an event\n"
            "`/listevents` - Show all events\n"
            "`/eventinfo` - View event details\n"
            "`/timezone` - Set server timezone\n"
            "`/setmilestones` - Configure milestone days\n"
            "`/healthcheck` - Check bot status\n"
            "`/vote` - Vote for supporter features"
        ),
        inline=False
    )
    
    embed.add_field(
        name="⭐ Supporter Commands (Vote Required)",
        value=(
            "`/settheme` - Change embed theme\n"
            "`/setbanner` - Add event banners"
        ),
        inline=False
    )
    
    embed.add_field(
        name="💎 Chromie Pro Features",
        value=(
            "• Multiple pinned boards per server\n"
            "• Advanced per-event milestone customization\n"
            "• Premium embed layouts\n"
            "• Priority support"
        ),
        inline=False
    )
    
    embed.set_footer(text="Use /vote to unlock supporter features!")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="setup", description="Configure the countdown channel")
@app_commands.describe(channel="The text channel for countdowns")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def setup_command(interaction: discord.Interaction, channel: discord.TextChannel):
    """Set up the countdown channel."""
    await safe_defer(interaction)
    
    guild_state = get_guild_state(interaction.guild_id)
    guild_state["event_channel_id"] = channel.id
    guild_state["pinned_message_id"] = 0  # Reset pinned message
    
    save_state()
    await update_pinned_message(bot, interaction.guild_id)
    
    embed = discord.Embed(
        title="✅ Setup Complete",
        description=f"Countdown channel set to {channel.mention}",
        color=discord.Color.green()
    )
    await safe_send(interaction, embed=embed)

@bot.tree.command(name="addevent", description="Create a new countdown event")
@app_commands.describe(
    name="Event name",
    date="Date (YYYY-MM-DD)",
    time="Time (HH:MM)",
    timezone="Timezone (e.g., America/New_York)",
    description="Event description",
    repeat="Repeat type: none, daily, weekly, monthly, yearly",
    notify_role="Role to mention",
    notify_channel="Channel for notifications"
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def addevent_command(
    interaction: discord.Interaction,
    name: str,
    date: str,
    time: str,
    timezone: str = None,
    description: str = "",
    repeat: str = "none",
    notify_role: discord.Role = None,
    notify_channel: discord.TextChannel = None
):
    """Add a new event."""
    await safe_defer(interaction)
    
    guild_state = get_guild_state(interaction.guild_id)
    
    # Parse datetime
    try:
        dt_str = f"{date} {time}"
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        tz = ZoneInfo(timezone if timezone else guild_state["timezone"])
        dt = dt.replace(tzinfo=tz)
    except Exception as e:
        await safe_send(
            interaction,
            embed=discord.Embed(
                title="❌ Invalid Date/Time",
                description=f"Please use format: YYYY-MM-DD HH:MM\nError: {e}",
                color=discord.Color.red()
            )
        )
        return
    
    # Validate repeat type
    repeat_type = repeat.lower()
    if repeat_type not in ["none", "daily", "weekly", "monthly", "yearly"]:
        await safe_send(
            interaction,
            embed=discord.Embed(
                title="❌ Invalid Repeat Type",
                description="Must be: none, daily, weekly, monthly, or yearly",
                color=discord.Color.red()
            )
        )
        return
    
    # Create event
    event = create_event(
        name=name,
        datetime_iso=dt.isoformat(),
        timezone=str(tz),
        created_by=interaction.user.id,
        description=description,
        repeat_type=repeat_type,
        notify_role_id=notify_role.id if notify_role else 0,
        notify_channel_id=notify_channel.id if notify_channel else 0,
        milestones=guild_state["settings"]["milestones"]
    )
    
    guild_state["events"].append(event)
    sort_events(guild_state)
    save_state()
    await update_pinned_message(bot, interaction.guild_id)
    
    embed = discord.Embed(
        title="✅ Event Created",
        description=f"**{name}**\nDate: <t:{int(dt.timestamp())}:F>",
        color=discord.Color.green()
    )
    embed.add_field(name="Event ID", value=event["id"], inline=False)
    await safe_send(interaction, embed=embed)

@bot.tree.command(name="editevent", description="Edit an existing event")
@app_commands.describe(
    event_identifier="Event ID or name",
    name="New name",
    date="New date (YYYY-MM-DD)",
    time="New time (HH:MM)",
    timezone="New timezone",
    description="New description",
    repeat="New repeat type",
    notify_role="New notification role",
    notify_channel="New notification channel"
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def editevent_command(
    interaction: discord.Interaction,
    event_identifier: str,
    name: str = None,
    date: str = None,
    time: str = None,
    timezone: str = None,
    description: str = None,
    repeat: str = None,
    notify_role: discord.Role = None,
    notify_channel: discord.TextChannel = None
):
    """Edit an existing event."""
    await safe_defer(interaction)
    
    guild_state = get_guild_state(interaction.guild_id)
    event = find_event(guild_state, event_identifier)
    
    if not event:
        await safe_send(
            interaction,
            embed=discord.Embed(
                title="❌ Event Not Found",
                description=f"No event found with ID or name: {event_identifier}",
                color=discord.Color.red()
            )
        )
        return
    
    # Update fields
    if name:
        event["name"] = name
    
    if date and time:
        try:
            dt_str = f"{date} {time}"
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
            tz = ZoneInfo(timezone if timezone else event["timezone"])
            dt = dt.replace(tzinfo=tz)
            event["datetime_iso"] = dt.isoformat()
            if timezone:
                event["timezone"] = timezone
        except Exception as e:
            await safe_send(
                interaction,
                embed=discord.Embed(
                    title="❌ Invalid Date/Time",
                    description=f"Error: {e}",
                    color=discord.Color.red()
                )
            )
            return
    
    if description is not None:
        event["description"] = description
    
    if repeat:
        repeat_type = repeat.lower()
        if repeat_type in ["none", "daily", "weekly", "monthly", "yearly"]:
            event["repeat"]["type"] = repeat_type
    
    if notify_role:
        event["notify_role_id"] = notify_role.id
    
    if notify_channel:
        event["notify_channel_id"] = notify_channel.id
    
    event["updated_at"] = datetime.utcnow().isoformat()
    
    sort_events(guild_state)
    save_state()
    await update_pinned_message(bot, interaction.guild_id)
    
    embed = discord.Embed(
        title="✅ Event Updated",
        description=f"**{event['name']}** has been updated",
        color=discord.Color.green()
    )
    await safe_send(interaction, embed=embed)

@bot.tree.command(name="deleteevent", description="Delete an event")
@app_commands.describe(event_identifier="Event ID or name to delete")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def deleteevent_command(interaction: discord.Interaction, event_identifier: str):
    """Delete an event."""
    await safe_defer(interaction)
    
    guild_state = get_guild_state(interaction.guild_id)
    event = find_event(guild_state, event_identifier)
    
    if not event:
        await safe_send(
            interaction,
            embed=discord.Embed(
                title="❌ Event Not Found",
                description=f"No event found with ID or name: {event_identifier}",
                color=discord.Color.red()
            )
        )
        return
    
    event_name = event["name"]
    guild_state["events"].remove(event)
    
    save_state()
    await update_pinned_message(bot, interaction.guild_id)
    
    embed = discord.Embed(
        title="✅ Event Deleted",
        description=f"**{event_name}** has been removed",
        color=discord.Color.green()
    )
    await safe_send(interaction, embed=embed)

@bot.tree.command(name="listevents", description="List all events")
@app_commands.guild_only()
async def listevents_command(interaction: discord.Interaction):
    """List all events."""
    guild_state = get_guild_state(interaction.guild_id)
    events = guild_state["events"]
    
    if not events:
        embed = discord.Embed(
            title="📅 No Events",
            description="Use `/addevent` to create your first event!",
            color=EMBED_COLOR
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    embed = discord.Embed(
        title=f"📅 Events ({len(events)})",
        color=EMBED_COLOR
    )
    
    sorted_events = sorted(events, key=lambda e: get_event_datetime(e))
    
    for event in sorted_events[:25]:  # Discord field limit
        dt = get_event_datetime(event)
        unix_ts = int(dt.timestamp())
        
        value = f"ID: `{event['id']}`\n<t:{unix_ts}:F> (<t:{unix_ts}:R>)"
        if event["repeat"]["type"] != "none":
            value += f"\n🔁 Repeats {event['repeat']['type']}"
        
        embed.add_field(name=event['name'], value=value, inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="eventinfo", description="Show detailed event information")
@app_commands.describe(event_identifier="Event ID or name")
@app_commands.guild_only()
async def eventinfo_command(interaction: discord.Interaction, event_identifier: str):
    """Show detailed event information."""
    guild_state = get_guild_state(interaction.guild_id)
    event = find_event(guild_state, event_identifier)
    
    if not event:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="❌ Event Not Found",
                color=discord.Color.red()
            ),
            ephemeral=True
        )
        return
    
    dt = get_event_datetime(event)
    unix_ts = int(dt.timestamp())
    
    embed = discord.Embed(
        title=f"📌 {event['name']}",
        description=event['description'] or "*No description*",
        color=EMBED_COLOR
    )
    
    embed.add_field(name="Event ID", value=f"`{event['id']}`", inline=False)
    embed.add_field(name="Date/Time", value=f"<t:{unix_ts}:F>", inline=False)
    embed.add_field(name="Countdown", value=f"<t:{unix_ts}:R>", inline=False)
    embed.add_field(name="Timezone", value=event['timezone'], inline=True)
    embed.add_field(name="Repeat", value=event['repeat']['type'].title(), inline=True)
    
    if event['notify_role_id']:
        role = interaction.guild.get_role(event['notify_role_id'])
        embed.add_field(name="Notify Role", value=role.mention if role else "Unknown", inline=True)
    
    if event['notify_channel_id']:
        channel = interaction.guild.get_channel(event['notify_channel_id'])
        embed.add_field(name="Notify Channel", value=channel.mention if channel else "Unknown", inline=True)
    
    if event['banner_url']:
        embed.set_image(url=event['banner_url'])
    
    milestones = ", ".join(str(m) for m in event['milestones'])
    embed.add_field(name="Milestones", value=milestones, inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="timezone", description="Manage server timezone")
@app_commands.describe(
    action="show or set",
    timezone="Timezone (e.g., America/New_York)"
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def timezone_command(interaction: discord.Interaction, action: str, timezone: str = None):
    """Set or show timezone."""
    guild_state = get_guild_state(interaction.guild_id)
    
    if action.lower() == "show":
        embed = discord.Embed(
            title="🌍 Server Timezone",
            description=f"Current timezone: **{guild_state['timezone']}**",
            color=EMBED_COLOR
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if action.lower() == "set":
        if not timezone:
            await interaction.response.send_message(
                "❌ Please provide a timezone (e.g., America/New_York)",
                ephemeral=True
            )
            return
        
        try:
            ZoneInfo(timezone)  # Validate
            guild_state["timezone"] = timezone
            save_state()
            
            embed = discord.Embed(
                title="✅ Timezone Updated",
                description=f"Server timezone set to **{timezone}**",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed)
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Invalid timezone: {e}",
                ephemeral=True
            )

@bot.tree.command(name="setmilestones", description="Set default milestone days")
@app_commands.describe(milestones="Comma-separated milestone days (e.g., 100,60,30,7,1,0)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def setmilestones_command(interaction: discord.Interaction, milestones: str):
    """Set default milestone days for the server."""
    guild_state = get_guild_state(interaction.guild_id)
    
    try:
        milestone_list = [int(x.strip()) for x in milestones.split(",")]
        milestone_list = sorted(set(milestone_list), reverse=True)
        
        guild_state["settings"]["milestones"] = milestone_list
        save_state()
        
        embed = discord.Embed(
            title="✅ Milestones Updated",
            description=f"Default milestones: {', '.join(str(m) for m in milestone_list)} days",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(
            f"❌ Invalid milestone format. Use comma-separated numbers (e.g., 100,60,30,7,1,0)\nError: {e}",
            ephemeral=True
        )

@bot.tree.command(name="healthcheck", description="Check bot permissions and configuration")
@app_commands.guild_only()
async def healthcheck_command(interaction: discord.Interaction):
    """Perform health check."""
    guild_state = get_guild_state(interaction.guild_id)
    guild = interaction.guild
    
    embed = discord.Embed(
        title="🏥 Health Check",
        color=EMBED_COLOR
    )
    
    # Check channel configuration
    channel_id = guild_state["event_channel_id"]
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel:
            embed.add_field(name="✅ Channel Configured", value=channel.mention, inline=False)
            
            # Check permissions
            perms = channel.permissions_for(guild.me)
            perm_checks = {
                "Send Messages": perms.send_messages,
                "Embed Links": perms.embed_links,
                "Read Message History": perms.read_message_history,
                "Manage Messages": perms.manage_messages,
                "View Channel": perms.view_channel
            }
            
            missing = [name for name, has in perm_checks.items() if not has]
            if missing:
                embed.add_field(
                    name="⚠️ Missing Permissions",
                    value="\n".join(f"❌ {p}" for p in missing),
                    inline=False
                )
            else:
                embed.add_field(name="✅ Permissions", value="All required permissions granted", inline=False)
        else:
            embed.add_field(name="❌ Channel Not Found", value="Channel was deleted or is inaccessible", inline=False)
    else:
        embed.add_field(name="⚠️ Not Configured", value="Use `/setup` to configure a channel", inline=False)
    
    # Event count
    event_count = len(guild_state["events"])
    embed.add_field(name="📅 Events", value=f"{event_count} active event(s)", inline=True)
    
    # Status indicators
    embed.add_field(name="Supporter", value=get_supporter_status_text(guild_state), inline=True)
    embed.add_field(name="Pro", value=get_pro_status_text(guild_state), inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="vote", description="Vote for Chromie to unlock supporter features")
@app_commands.guild_only()
async def vote_command(interaction: discord.Interaction):
    """Show vote information."""
    guild_state = get_guild_state(interaction.guild_id)
    
    embed = discord.Embed(
        title="⭐ Support Chromie",
        description="Vote for Chromie on Top.gg to unlock supporter features!",
        color=discord.Color.gold()
    )
    
    if TOPGG_BOT_ID:
        vote_url = f"https://top.gg/bot/{TOPGG_BOT_ID}/vote"
        embed.add_field(
            name="How to Vote",
            value=f"[Click here to vote on Top.gg]({vote_url})\n\nVotes last **{VOTE_DURATION_HOURS} hours**",
            inline=False
        )
    else:
        embed.add_field(
            name="⚠️ Not Configured",
            value="Top.gg integration not configured by bot owner",
            inline=False
        )
    
    embed.add_field(name="Current Status", value=get_supporter_status_text(guild_state), inline=False)
    
    embed.add_field(
        name="Supporter Features",
        value="• Custom themes\n• Event banners\n• And more!",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed, ephemeral=True, view=build_vote_view())

# ============================================================================
# SUPPORTER-GATED COMMANDS
# ============================================================================

@bot.tree.command(name="settheme", description="[Supporter] Change embed theme")
@app_commands.describe(theme="Theme name (classic, dark, light, colorful)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def settheme_command(interaction: discord.Interaction, theme: str):
    """Set embed theme (supporter feature)."""
    await safe_defer(interaction)
    
    guild_state = get_guild_state(interaction.guild_id)
    
    # Check supporter status
    if not has_active_vote(guild_state):
        # Try to update vote status
        voted = await update_vote_status(guild_state, interaction.user.id)
        if not voted:
            await send_vote_required(interaction, "theme customization")
            return
    
    # Valid themes
    valid_themes = ["classic", "dark", "light", "minimal", "colorful"]
    if theme.lower() not in valid_themes:
        await safe_send(
            interaction,
            embed=discord.Embed(
                title="❌ Invalid Theme",
                description=f"Available themes: {', '.join(valid_themes)}",
                color=discord.Color.red()
            )
        )
        return
    
    guild_state["settings"]["theme"] = theme.lower()
    save_state()
    await update_pinned_message(bot, interaction.guild_id)
    
    embed = discord.Embed(
        title="✅ Theme Updated",
        description=f"Theme set to **{theme}**",
        color=discord.Color.green()
    )
    await safe_send(interaction, embed=embed)

@bot.tree.command(name="setbanner", description="[Supporter] Set event banner image")
@app_commands.describe(
    event_identifier="Event ID or name",
    banner_url="Banner image URL",
    attachment="Or upload an image"
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def setbanner_command(
    interaction: discord.Interaction,
    event_identifier: str,
    banner_url: str = None,
    attachment: discord.Attachment = None
):
    """Set event banner (supporter feature)."""
    await safe_defer(interaction)
    
    guild_state = get_guild_state(interaction.guild_id)
    
    # Check supporter status
    if not has_active_vote(guild_state):
        voted = await update_vote_status(guild_state, interaction.user.id)
        if not voted:
            await send_vote_required(interaction, "event banners")
            return
    
    # Find event
    event = find_event(guild_state, event_identifier)
    if not event:
        await safe_send(
            interaction,
            embed=discord.Embed(
                title="❌ Event Not Found",
                color=discord.Color.red()
            )
        )
        return
    
    # Get banner URL
    url = banner_url
    if attachment:
        url = attachment.url
    
    if not url:
        await safe_send(
            interaction,
            embed=discord.Embed(
                title="❌ No Banner Provided",
                description="Provide either a URL or an attachment",
                color=discord.Color.red()
            )
        )
        return
    
    event["banner_url"] = url
    event["updated_at"] = datetime.utcnow().isoformat()
    
    save_state()
    await update_pinned_message(bot, interaction.guild_id)
    
    embed = discord.Embed(
        title="✅ Banner Updated",
        description=f"Banner set for **{event['name']}**",
        color=discord.Color.green()
    )
    embed.set_image(url=url)
    await safe_send(interaction, embed=embed)

# ============================================================================
# OWNER-ONLY COMMANDS
# ============================================================================

@bot.tree.command(name="vote_debug", description="[Owner] Debug vote checking")
async def vote_debug_command(interaction: discord.Interaction):
    """Debug vote checking (owner only)."""
    if interaction.user.id != BOT_OWNER_ID:
        await interaction.response.send_message("❌ Owner only", ephemeral=True)
        return
    
    guild_state = get_guild_state(interaction.guild_id)
    
    embed = discord.Embed(
        title="🔧 Vote Debug",
        color=EMBED_COLOR
    )
    
    embed.add_field(name="TOPGG_TOKEN", value="✅ Set" if TOPGG_TOKEN else "❌ Missing", inline=True)
    embed.add_field(name="TOPGG_BOT_ID", value="✅ Set" if TOPGG_BOT_ID else "❌ Missing", inline=True)
    
    if TOPGG_TOKEN and TOPGG_BOT_ID:
        voted = await topgg_has_voted(interaction.user.id, force=True)
        embed.add_field(name="Your Vote Status", value="✅ Voted" if voted else "❌ Not voted", inline=False)
    
    embed.add_field(name="Guild Status", value=get_supporter_status_text(guild_state), inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="owner_unlock", description="[Owner] Temporarily unlock features")
@app_commands.describe(
    feature="supporter or pro",
    duration_hours="Hours to unlock (default 12)"
)
async def owner_unlock_command(
    interaction: discord.Interaction,
    feature: str,
    duration_hours: int = 12
):
    """Owner-only temporary unlock."""
    if interaction.user.id != BOT_OWNER_ID:
        await interaction.response.send_message("❌ Owner only", ephemeral=True)
        return
    
    guild_state = get_guild_state(interaction.guild_id)
    now = datetime.utcnow()
    until = now + timedelta(hours=duration_hours)
    
    if feature.lower() == "supporter":
        guild_state["supporter"]["last_vote_at"] = now.isoformat()
        guild_state["supporter"]["vote_until"] = until.isoformat()
        msg = f"✅ Supporter unlocked for {duration_hours} hours"
    elif feature.lower() == "pro":
        guild_state["pro"]["pro_active"] = True
        guild_state["pro"]["pro_until"] = until.isoformat()
        msg = f"✅ Pro unlocked for {duration_hours} hours"
    else:
        await interaction.response.send_message("❌ Invalid feature (use: supporter or pro)", ephemeral=True)
        return
    
    save_state()
    await update_pinned_message(bot, interaction.guild_id)
    
    await interaction.response.send_message(msg, ephemeral=True)

# ============================================================================
# MAIN
# ============================================================================

def main():
    """Main entry point."""
    if not TOKEN:
        raise RuntimeError(
            "No bot token found. Set the DISCORD_BOT_TOKEN environment variable."
        )
    
    log_info("Starting Chromie bot...")
    log_info(f"Data path: {DATA_FILE}")
    log_info(f"Top.gg configured: {bool(TOPGG_TOKEN and TOPGG_BOT_ID)}")
    
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        log_info("Shutting down...")
    except Exception as e:
        log_error(f"Fatal error: {e}\n{traceback.format_exc()}")

if __name__ == "__main__":
    main()
