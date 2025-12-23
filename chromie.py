import os
import json
from pathlib import Path
from datetime import datetime, date
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from discord import app_commands

# ==========================
# CONFIG
# ==========================

# Default timezone for all events
DEFAULT_TZ = ZoneInfo("America/Chicago")

# How often to update pinned countdowns (seconds)
UPDATE_INTERVAL_SECONDS = 60

# Default milestones (days before event) – messages at these offsets
DEFAULT_MILESTONES = [100, 50, 30, 14, 7, 2, 1, 0]

# Where we store all data (per server)
DATA_FILE = Path(os.getenv("CHROMIE_DATA_PATH", "/var/data/chromie_state.json"))

# Bot token – preferred: set DISCORD_BOT_TOKEN in your hosting env
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
# For local testing only, you *could* paste your token here instead:
# if not TOKEN:
#     TOKEN = "YOUR_BOT_TOKEN_HERE"

EMBED_COLOR = discord.Color.from_rgb(140, 82, 255)  # ChronoBot purple

# ==========================
# STATE HANDLING
# ==========================

"""
State structure:

{
  "guilds": {
    "123456789012345678": {
      "event_channel_id": 987654321098765432,
      "pinned_message_id": 123123123123123123,
      "events": [
        {
          "name": "Couples Retreat 💕",
          "timestamp": 1771000800,          # unix seconds
          "milestones": [100, 50, 30, ...],
          "announced_milestones": [100, 50],
        },
        ...
      ],
      "welcomed": true
    },
    ...
  },
  "user_links": {
    "user_id_str": guild_id_int
  }
}
"""


def load_state():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    else:
        data = {}

    data.setdefault("guilds", {})
    data.setdefault("user_links", {})
    return data


def save_state():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)



def get_guild_state(guild_id: int):
    gid = str(guild_id)
    guilds = state.setdefault("guilds", {})
    if gid not in guilds:
        guilds[gid] = {
            "event_channel_id": None,
            "pinned_message_id": None,
            "events": [],
            "welcomed": False,
        }
    else:
        guilds[gid].setdefault("event_channel_id", None)
        guilds[gid].setdefault("pinned_message_id", None)
        guilds[gid].setdefault("events", [])
        guilds[gid].setdefault("welcomed", False)
    return guilds[gid]


def get_user_links():
    return state.setdefault("user_links", {})


def sort_events(guild_state: dict):
    """Sort events soonest → farthest based on timestamp."""
    events = guild_state.get("events", [])
    events.sort(key=lambda ev: ev.get("timestamp", 0))
    guild_state["events"] = events


state = load_state()
for _, g_state in state.get("guilds", {}).items():
    sort_events(g_state)

    # Backfill new optional fields for older saved events
    for ev in g_state.get("events", []):
        ev.setdefault("milestones", DEFAULT_MILESTONES.copy())
        ev.setdefault("announced_milestones", [])
        ev.setdefault("repeat_every_days", None)
        ev.setdefault("repeat_anchor_date", None)
        ev.setdefault("announced_repeat_dates", [])

save_state()


# ==========================
# DISCORD SETUP
# ==========================

intents = discord.Intents.default()
# Slash commands do NOT require message_content, but having it on is fine.
bot = commands.Bot(command_prefix="None", intents=intents)


async def send_onboarding_for_guild(guild: discord.Guild):
    """Send the onboarding/setup message for a guild, and mark it welcomed once."""
    guild_state = get_guild_state(guild.id)

    # If we've already tried to welcome this guild, don't do it again automatically
    if guild_state.get("welcomed"):
        return

    contact_user = guild.owner or (await bot.fetch_user(guild.owner_id))
    mention = contact_user.mention if contact_user else ""
    milestone_str = ", ".join(str(x) for x in DEFAULT_MILESTONES)

    setup_message = (
        f"Hey {mention}! Thanks for inviting **ChronoBot** to **{guild.name}** 🕒✨\n\n"
        "I’m **Chromie** — your server’s confident little timekeeper. I pin a clean countdown list and I nudge people at the right moments. "
        "It’s like a calendar… but with better vibes.\n\n"
        f"⏳ **Default milestones:** {milestone_str} days before the event (including **0** for day-of).\n\n"
        "**⚡ Quick start (two commands, instant order):**\n"
        "1) In your events channel: `/seteventchannel`\n"
        "2) Add your first event: `/addevent date: 04/12/2026 time: 09:00 name: Game Night 🎲`\n\n"
        "**🧰 The power tools you just unlocked:**\n"
        "• `/editevent` – tweak name/date/time without re-adding\n"
        "• `/remindall` – send a reminder to everyone\n"
        "• `/dupeevent` – clone an event (perfect for yearly stuff)\n"
        "• `/reorder` – move an event up/down in the list\n"
        "• `/seteventowner` – assign an owner and I’ll DM them at milestones\n"
        "• `/setrepeat index: <number> every_days: <days>` – repeating reminders every X days\n"
        "   - Daily example: `/setrepeat index: 1 every_days: 1`\n"
        "   - Weekly example: `/setrepeat index: 1 every_days: 7`\n"
        "• `/clearrepeat index: <number>` – turn repeating reminders off\n\n"
        "Need the full menu? Type `/chronohelp` and I’ll hand you the whole spellbook.\n\n"
        "Alright — I’ll be over here, politely bullying time into behaving. 💜"
    )

    sent = False
    if contact_user:
        try:
            await contact_user.send(setup_message)
            sent = True
        except discord.Forbidden:
            sent = False

    if not sent:
        # Fallback: try system channel, then first text channel where I can speak
        fallback_channel = guild.system_channel
        if fallback_channel is None:
            for channel in guild.text_channels:
                perms = channel.permissions_for(guild.me)
                if perms.send_messages:
                    fallback_channel = channel
                    break

        if fallback_channel is not None:
            try:
                await fallback_channel.send(setup_message)
                sent = True
            except discord.Forbidden:
                sent = False

    # Mark as welcomed after the first attempt
    guild_state["welcomed"] = True
    save_state()
    


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Sync slash commands
    try:
        await bot.tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print(f"Error syncing commands: {e}")

    if not update_countdowns.is_running():
        update_countdowns.start()


@bot.event
async def on_guild_join(guild: discord.Guild):
    """When the bot is added to a new server, send the setup guide."""
    g_state = get_guild_state(guild.id)
    sort_events(g_state)
    save_state()
    await send_onboarding_for_guild(guild)


# ==========================
# TIME & EMBED HELPERS
# ==========================


def _today_local_date() -> date:
    """Return today's date in the bot's default timezone."""
    return datetime.now(DEFAULT_TZ).date()

def compute_time_left(dt: datetime):
    """Return (description, days_left, event_passed)."""
    now = datetime.now(DEFAULT_TZ)
    delta = dt - now
    total_seconds = int(delta.total_seconds())

    if total_seconds <= 0:
        return "The event is happening now or has already started! 💕", 0, True

    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if not parts:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")

    return " • ".join(parts), days, False


def build_embed_for_guild(guild_state: dict):
    sort_events(guild_state)
    events = guild_state.get("events", [])
    embed = discord.Embed(
        title="Upcoming Event Countdowns",
        description="Live countdowns for this server’s events.",
        color=EMBED_COLOR,
    )

    if not events:
        embed.add_field(
            name="No events yet",
            value="Use `/addevent` to add one.",
            inline=False,
        )
        return embed

    any_upcoming = False

    for ev in events:
        dt = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
        desc, days_left, passed = compute_time_left(dt)
        date_str = dt.strftime("%B %d, %Y at %I:%M %p %Z")

        if passed:
            value = f"**{date_str}**\n➡️ Event has started or passed. 🎉"
        else:
            any_upcoming = True
            value = f"**{date_str}**\n⏱ **{desc}** remaining"

        embed.add_field(
            name=ev["name"],
            value=value,
            inline=False,
        )

    if not any_upcoming:
        embed.set_footer(text="All listed events have already started or passed.")

    return embed


async def rebuild_pinned_message(guild_id: int, channel: discord.TextChannel, guild_state: dict):
    """Unpin the old pinned countdown (if any), send a new one, and pin it."""
    sort_events(guild_state)
    old_id = guild_state.get("pinned_message_id")
    if old_id:
        try:
            old_msg = await channel.fetch_message(old_id)
            await old_msg.unpin()
        except (discord.NotFound, discord.Forbidden):
            pass

    embed = build_embed_for_guild(guild_state)
    msg = await channel.send(embed=embed)

    try:
        await msg.pin()
    except discord.Forbidden:
        print(f"[Guild {guild_id}] Missing permission to pin messages.")
    except discord.HTTPException as e:
        print(f"[Guild {guild_id}] Failed to pin message: {e}")

    guild_state["pinned_message_id"] = msg.id
    save_state()
    return msg


async def get_or_create_pinned_message(guild_id: int, channel: discord.TextChannel):
    guild_state = get_guild_state(guild_id)
    sort_events(guild_state)
    pinned_id = guild_state.get("pinned_message_id")

    if pinned_id:
        try:
            msg = await channel.fetch_message(pinned_id)
            return msg
        except discord.NotFound:
            pass

    embed = build_embed_for_guild(guild_state)
    msg = await channel.send(embed=embed)

    try:
        await msg.pin()
    except discord.Forbidden:
        print(f"[Guild {guild_id}] Missing permission to pin messages.")
    except discord.HTTPException as e:
        print(f"[Guild {guild_id}] Failed to pin message: {e}")

    guild_state["pinned_message_id"] = msg.id
    save_state()
    return msg

async def get_text_channel(channel_id: int) -> discord.TextChannel | None:
    ch = bot.get_channel(channel_id)
    if isinstance(ch, discord.TextChannel):
        return ch
    try:
        ch = await bot.fetch_channel(channel_id)
        return ch if isinstance(ch, discord.TextChannel) else None
    except Exception:
        return None


# ==========================
# BACKGROUND LOOP
# ==========================

@tasks.loop(seconds=UPDATE_INTERVAL_SECONDS)
async def update_countdowns():
    guilds = state.get("guilds", {})
    for gid_str, guild_state in list(guilds.items()):
        guild_id = int(gid_str)
        sort_events(guild_state)
        channel_id = guild_state.get("event_channel_id")
        if not channel_id:
            continue

        channel = await get_text_channel(channel_id)
        if channel is None:
            continue


        # Update pinned embed
        pinned = await get_or_create_pinned_message(guild_id, channel)
        embed = build_embed_for_guild(guild_state)
        try:
            await pinned.edit(embed=embed)
        except discord.HTTPException as e:
            print(f"[Guild {guild_id}] Failed to edit pinned message: {e}")

        # Milestone + repeating reminder checks
        today = _today_local_date()

        for ev in guild_state.get("events", []):
            dt = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
            desc, days_left, passed = compute_time_left(dt)
            if passed or days_left < 0:
                continue

            milestone_sent_today = False

            # ---------------------------
            # Milestones (fixed day offsets)
            # ---------------------------
            milestones = ev.get("milestones", DEFAULT_MILESTONES)
            announced = ev.get("announced_milestones", [])

            if days_left in milestones and days_left not in announced:
                if days_left == 1:
                    text = f"✨ **{ev['name']}** is **tomorrow**! ✨"
                else:
                    text = (
                        f"💌 **{ev['name']}** is **{days_left} day"
                        f"{'s' if days_left != 1 else ''}** away!"
                    )

                await channel.send(text)

                announced.append(days_left)
                ev["announced_milestones"] = announced
                milestone_sent_today = True
                save_state()

            # ---------------------------
            # Repeating reminders (every X days)
            # ---------------------------
            repeat_every = ev.get("repeat_every_days")
            if isinstance(repeat_every, int) and repeat_every > 0:
                anchor_str = ev.get("repeat_anchor_date") or today.isoformat()
                try:
                    anchor = date.fromisoformat(anchor_str)
                except ValueError:
                    anchor = today
                    ev["repeat_anchor_date"] = anchor.isoformat()

                days_since_anchor = (today - anchor).days

                # Start repeating reminders the day *after* setup, then every N days
                if days_since_anchor > 0 and (days_since_anchor % repeat_every == 0):
                    sent_dates = ev.get("announced_repeat_dates", [])
                    if today.isoformat() not in sent_dates:
                        # Avoid double-posting on the same day a milestone is announced
                        if not milestone_sent_today:
                            date_str = dt.strftime("%B %d, %Y")
                            await channel.send(
                                f"🔁 Reminder: **{ev['name']}** is in **{desc}** "
                                f"(on **{date_str}**)."
                            )

                        sent_dates.append(today.isoformat())
                        # Keep this list from growing forever
                        ev["announced_repeat_dates"] = sent_dates[-180:]
                        save_state()


# ==========================
# SLASH COMMANDS
# ==========================

def format_events_list(guild_state: dict) -> str:
    sort_events(guild_state)
    events = guild_state.get("events", [])
    if not events:
        return (
            "There are no events set for this server yet.\n"
            "Add one with `/addevent`."
        )

    lines = []
    for idx, ev in enumerate(events, start=1):
        dt = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
        desc, days_left, passed = compute_time_left(dt)
        status = "✅ done" if passed else "⏳ active"
        repeat_every = ev.get("repeat_every_days")
        repeat_note = ""
        if isinstance(repeat_every, int) and repeat_every > 0:
            repeat_note = f" 🔁 every {repeat_every} day{'s' if repeat_every != 1 else ''}"

        lines.append(
            f"**{idx}. {ev['name']}** — {dt.strftime('%m/%d/%Y %H:%M')} "
            f"({desc}) [{status}]{repeat_note}"
        )
    return "\n".join(lines)


@bot.tree.command(name="seteventchannel", description="Set this channel as the event countdown channel.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def seteventchannel(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    guild_state["event_channel_id"] = interaction.channel.id
    guild_state["pinned_message_id"] = None
    sort_events(guild_state)
    save_state()

    await interaction.response.send_message(
        "✅ This channel is now the event countdown channel for this server.\n"
        "Use `/addevent` to add events.",
        ephemeral=True,
    )


@bot.tree.command(name="linkserver", description="Link yourself to this server for DM control.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def linkserver(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    user_links = get_user_links()
    user_links[str(interaction.user.id)] = guild.id
    save_state()

    await interaction.response.send_message(
        "🔗 Linked your user to this server.\n"
        "You can now DM me `/addevent` and I’ll add events to this server (as long as you still have Manage Server).",
        ephemeral=True,
    )

@bot.tree.command(name="addevent", description="Add a new event to the countdown.")
@app_commands.describe(
    date="Date in MM/DD/YYYY format",
    time="Time in 24-hour HH:MM format (America/Chicago)",
    name="Name of the event",
)
async def addevent(interaction: discord.Interaction, date: str, time: str, name: str):
    user = interaction.user

    # ---------------------------
    # Decide which guild to target
    # ---------------------------
    if interaction.guild is not None:
        guild = interaction.guild

        member = interaction.user
        if not isinstance(member, discord.Member):
            member = guild.get_member(user.id)

        if member is None:
            try:
                member = await guild.fetch_member(user.id)
            except Exception:
                member = None

        perms = getattr(member, "guild_permissions", None)
        if not perms or not (perms.manage_guild or perms.administrator):
            await interaction.response.send_message(
                "You need the **Manage Server** or **Administrator** permission "
                "to add events in this server.",
                ephemeral=True,
            )
            return

        guild_state = get_guild_state(guild.id)
        is_dm = False

    else:
        user_links = get_user_links()
        linked_guild_id = user_links.get(str(user.id))
        if not linked_guild_id:
            await interaction.response.send_message(
                "I don't know which server to use for your DMs yet.\n"
                "In the server you want to control, run `/linkserver`, then use `/addevent` here again.",
                ephemeral=True,
            )
            return

        guild = bot.get_guild(linked_guild_id)
        if not guild:
            await interaction.response.send_message(
                "I can't find the linked server anymore. Maybe I was removed from it?\n"
                "Re-add me and run `/linkserver` again.",
                ephemeral=True,
            )
            return

        member = guild.get_member(user.id)
        if member is None:
            try:
                member = await guild.fetch_member(user.id)
            except Exception:
                member = None

        perms = getattr(member, "guild_permissions", None)
        if not perms or not (perms.manage_guild or perms.administrator):
            await interaction.response.send_message(
                "You no longer have **Manage Server** (or **Administrator**) in the linked server, "
                "so I can’t add events via DM.\n"
                "Ask an admin to run `/linkserver` again if needed.",
                ephemeral=True,
            )
            return

        guild_state = get_guild_state(guild.id)
        is_dm = True

    # ---------------------------
    # Make sure we have an events channel
    # ---------------------------
    if not guild_state.get("event_channel_id"):
        msg = (
            "I don't know which channel to use yet.\n"
            "Run `/seteventchannel` in the channel where you want the countdown pinned."
        )
        if is_dm:
            msg += "\n(Do this in the linked server.)"
        await interaction.response.send_message(msg, ephemeral=True)
        return

    # ---------------------------
    # Parse date/time (MM/DD/YYYY HH:MM)
    # ---------------------------
    try:
        dt = datetime.strptime(f"{date} {time}", "%m/%d/%Y %H:%M")
    except ValueError:
        await interaction.response.send_message(
            "I couldn't understand that date/time.\n"
            "Use something like: `date: 04/12/2026` `time: 09:00` (MM/DD/YYYY and 24-hour time).",
            ephemeral=True,
        )
        return

    dt = dt.replace(tzinfo=DEFAULT_TZ)

    # Optional: prevent past events
    if dt <= datetime.now(DEFAULT_TZ):
        await interaction.response.send_message(
            "That date/time is in the past. Please choose a future time.",
            ephemeral=True,
        )
        return

    event = {
        "name": name,
        "timestamp": int(dt.timestamp()),
        "milestones": DEFAULT_MILESTONES.copy(),
        "announced_milestones": [],
        "repeat_every_days": None,
        "repeat_anchor_date": None,
        "announced_repeat_dates": [],
    }

    guild_state["events"].append(event)
    sort_events(guild_state)
    save_state()

    channel_id = guild_state.get("event_channel_id")
    if channel_id:
        channel = await get_text_channel(channel_id)
        if channel is not None:
            await rebuild_pinned_message(guild.id, channel, guild_state)

    await interaction.response.send_message(
        f"✅ Added event **{name}** on {dt.strftime('%B %d, %Y at %I:%M %p %Z')} "
        f"in server **{guild.name}**.",
        ephemeral=True,
    )

    
@bot.tree.command(name="listevents", description="List all events for this server.")
@app_commands.guild_only()
async def listevents(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    text = format_events_list(guild_state)
    await interaction.response.send_message(text, ephemeral=True)

@bot.tree.command(
    name="removeevent",
    description="Remove an event by its list number (from /listevents)."
)
@app_commands.describe(
    index="The number shown in /listevents (1, 2, 3, ...)"
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def removeevent(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None  # guaranteed by @guild_only

    guild_state = get_guild_state(guild.id)
    # Make sure events are in soonest → farthest order
    sort_events(guild_state)
    events = guild_state.get("events", [])

    if not events:
        await interaction.response.send_message(
            "There are no events to remove.",
            ephemeral=True,
        )
        return

    if index < 1 or index > len(events):
        await interaction.response.send_message(
            f"Index must be between 1 and {len(events)}.",
            ephemeral=True,
        )
        return

    # Remove the chosen event
    ev = events.pop(index - 1)
    save_state()

    # Rebuild the pinned countdown so the list is accurate
    channel_id = guild_state.get("event_channel_id")
    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        await rebuild_pinned_message(guild.id, channel, guild_state)

    await interaction.response.send_message(
        f"🗑 Removed event **{ev['name']}**.",
        ephemeral=True,
    )



@bot.tree.command(
    name="setrepeat",
    description="Set a repeating reminder for an event (every X days)."
)
@app_commands.describe(
    index="The number shown in /listevents (1, 2, 3, ...)",
    every_days="Repeat interval in days (1 = daily, 7 = weekly, etc.)",
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def setrepeat(interaction: discord.Interaction, index: int, every_days: int):
    guild = interaction.guild
    assert guild is not None  # guaranteed by @guild_only

    if every_days < 1 or every_days > 365:
        await interaction.response.send_message(
            "Repeat interval must be between **1** and **365** days.",
            ephemeral=True,
        )
        return

    guild_state = get_guild_state(guild.id)
    sort_events(guild_state)
    events = guild_state.get("events", [])

    if not events:
        await interaction.response.send_message(
            "There are no events yet. Add one with `/addevent` first.",
            ephemeral=True,
        )
        return

    if index < 1 or index > len(events):
        await interaction.response.send_message(
            f"Index must be between 1 and {len(events)}.",
            ephemeral=True,
        )
        return

    ev = events[index - 1]
    today = _today_local_date().isoformat()

    ev["repeat_every_days"] = int(every_days)
    ev["repeat_anchor_date"] = today
    ev["announced_repeat_dates"] = []
    save_state()
    plural = "s" if every_days != 1 else ""
    await interaction.response.send_message(
        f"✅ Repeating reminders enabled for **{ev['name']}** — every **{every_days}** day{plural} (starting tomorrow). "
        f"Use `/clearrepeat index: {index}` to turn it off.",
        ephemeral=True,
    )


@bot.tree.command(
    name="clearrepeat",
    description="Turn off repeating reminders for an event."
)
@app_commands.describe(
    index="The number shown in /listevents (1, 2, 3, ...)"
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def clearrepeat(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None  # guaranteed by @guild_only

    guild_state = get_guild_state(guild.id)
    sort_events(guild_state)
    events = guild_state.get("events", [])

    if not events:
        await interaction.response.send_message(
            "There are no events to update.",
            ephemeral=True,
        )
        return

    if index < 1 or index > len(events):
        await interaction.response.send_message(
            f"Index must be between 1 and {len(events)}.",
            ephemeral=True,
        )
        return

    ev = events[index - 1]
    ev["repeat_every_days"] = None
    ev["repeat_anchor_date"] = None
    ev["announced_repeat_dates"] = []
    save_state()

    await interaction.response.send_message(
        f"🧹 Repeating reminders disabled for **{ev['name']}**.",
        ephemeral=True,
    )

@bot.tree.command(name="update_countdown", description="Force-refresh the pinned countdown.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.guild_only()
async def update_countdown_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    sort_events(guild_state)
    channel_id = guild_state.get("event_channel_id")

    if not channel_id:
        await interaction.response.send_message(
            "No events channel set yet. Run `/seteventchannel` in your events channel.",
            ephemeral=True,
        )
        return

    if interaction.channel_id != channel_id:
        await interaction.response.send_message(
            "Please run this command in the configured events channel.",
            ephemeral=True,
        )
        return

    channel = interaction.channel
    assert isinstance(channel, discord.TextChannel)

    pinned = await get_or_create_pinned_message(guild.id, channel)
    embed = build_embed_for_guild(guild_state)
    await pinned.edit(embed=embed)

    await interaction.response.send_message(
        "⏱ Countdown updated.",
        ephemeral=True,
    )


@bot.tree.command(name="resendsetup", description="Resend the onboarding/setup message.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def resendsetup(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    g_state = get_guild_state(guild.id)
    g_state["welcomed"] = False
    save_state()

    await send_onboarding_for_guild(guild)
    await interaction.response.send_message(
        "📨 Setup instructions have been resent to the server owner (or a fallback channel).",
        ephemeral=True,
    )


@bot.tree.command(name="chronohelp", description="Show ChronoBot setup & command help.")
async def chronohelp(interaction: discord.Interaction):
    text = (
        "**ChronoBot – Setup & Commands**\n\n"
        "All slash command responses are ephemeral (only you see them).\n\n"
        "**Tip:** Use `/listevents` to see event numbers for any command that needs `index:`\n\n"
        "**Setup**\n"
        "• `/seteventchannel` – pick the channel where the pinned countdown lives\n"
        "• `/addevent` – add an event (MM/DD/YYYY + 24-hour HH:MM)\n\n"
        "**Browse**\n"
        "• `/listevents` – list events\n"
        "• `/nextevent` – show the next upcoming event\n"
        "• `/eventinfo index:` – details for one event\n\n"
        "**Edit & organize**\n"
        "• `/editevent index:` – edit name/date/time\n"
        "• `/dupeevent index: date:` – duplicate an event (optional time/name)\n"
        "• `/reorder index: position:` – move an event in the list\n"
        "• `/removeevent index:` – delete an event\n\n"
        "**Repeating reminders (every X days)**\n"
        "• `/setrepeat index: every_days:` – turn on repeating reminders (1 = daily, 7 = weekly)\n"
        "   - Daily example: `/setrepeat index: 1 every_days: 1`\n"
        "   - Weekly example: `/setrepeat index: 1 every_days: 7`\n"
        "• `/clearrepeat index:` – turn repeating reminders off\n\n"
        "**Milestones & notifications**\n"
        "• `/remindall` – send a notification to the channel about the event\n"
        "• `/setmilestones index:` – set custom milestone days\n"
        "• `/resetmilestones index:` – restore default milestones\n"
        "• `/silence index:` – stop reminders for an event (keeps it listed)\n"
        "• `/seteventowner index: user:` – assign an owner (they get milestone DMs)\n"
        "• `/cleareventowner index:` – remove the owner\n"
        "• `/setmentionrole role:` – @mention a role on milestone posts\n"
        "• `/clearmentionrole` – stop role mentions\n\n"
        "**Maintenance**\n"
        "• `/archivepast` – remove past events\n"
        "• `/resetchannel` – clear the configured channel\n"
        "• `/healthcheck` – show config + permission diagnostics\n"
        "• `/purgeevents confirm: YES` – delete all events for this server\n\n"
        "**Optional: DM control**\n"
        "• `/linkserver` – link your DMs to this server (Manage Server required)\n"
        "• Then DM me `/addevent` to add events remotely\n"
    )

    await interaction.response.send_message(text, ephemeral=True)



# ==========================
# RUN
# ==========================

def main():
    if not TOKEN:
        raise RuntimeError(
            "No bot token found. Set the DISCORD_BOT_TOKEN environment variable "
            "or edit the TOKEN section near the top of the file."
        )
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
