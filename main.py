import os
import json
import asyncio
from datetime import datetime, timedelta
from threading import Thread
from flask import Flask

import discord
from discord.ext import commands, tasks
from discord import ui
import gspread
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------
# 🌐 0. Flask Server for Render Keep-Alive
# ---------------------------------------------------------
app = Flask('')

@app.route('/')
def home():
    return "Boogeyman Boss Bot is Alive and Running on Render!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# ---------------------------------------------------------
# 🔑 1. Google Sheets Setup & Async Wrapper
# ---------------------------------------------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

SHEET_NAME = "Erica-05 Update 15-04-2026"
SCORE_SHEET_NAME = "SCORE_DATA"
EVENT_SHEET_NAME = "EVENT_BOSS"

def _get_worksheets_sync():
    if os.path.exists("credentials.json"):
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    else:
        google_creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if google_creds_json:
            creds_dict = json.loads(google_creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        else:
            raise ValueError("❌ Google Credentials not found in Environment Variables!")
            
    client = gspread.authorize(creds)
    spreadsheet = client.open(SHEET_NAME)
    
    sheet_main = spreadsheet.worksheet("SHEET BOT ERICA")
        
    try:
        sheet_score = spreadsheet.worksheet(SCORE_SHEET_NAME)
    except Exception:
        sheet_score = spreadsheet.add_worksheet(title=SCORE_SHEET_NAME, rows="500", cols="6")
        sheet_score.append_row(["USER ID", "Display Name", "Score", "BOSS NAME", "DATE", "TIME"])

    try:
        sheet_event = spreadsheet.worksheet(EVENT_SHEET_NAME)
    except Exception:
        sheet_event = spreadsheet.add_worksheet(title=EVENT_SHEET_NAME, rows="100", cols="3")
        sheet_event.append_row(["EVENT_BOSS", "Point", "Penalty"])
        
    return sheet_main, sheet_score, sheet_event

async def get_worksheets():
    return await asyncio.to_thread(_get_worksheets_sync)

TOKEN = os.getenv("DISCORD_TOKEN")
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

notified_bosses = {}
main_setup_message = None  
claim_status = {}  # {boss_name: True/False}

# Queue System for High Concurrency Google Sheet Writes
sheet_queue = asyncio.Queue()

# ---------------------------------------------------------
# 🔄 Retry & Queue Processing
# ---------------------------------------------------------
async def execute_sheet_write_with_retry(func, *args, max_retries=3):
    """Executes a blocking gspread write operation in a thread with backoff retries."""
    for attempt in range(1, max_retries + 1):
        try:
            return await asyncio.to_thread(func, *args)
        except Exception as e:
            print(f"⚠️️ Google Sheet write error (Attempt {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                raise e
            await asyncio.sleep(1.5 * attempt)

async def process_sheet_queue():
    """Background task processing queued Google Sheets write operations sequentially."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        item = await sheet_queue.get()
        func, args, interaction, action_name = item
        try:
            await execute_sheet_write_with_retry(func, *args)
            print(f"✅ Queue Processed: {action_name} for {interaction.user.display_name}")
        except Exception as e:
            print(f"❌ Queue Failed: {action_name} - {e}")
            try:
                retry_view = RetryActionView(func=func, args=args, action_name=action_name)
                await interaction.followup.send(
                    f"❌ **{action_name} Failed!** Google Sheets is currently busy.\n"
                    "Click **Retry Claim** below to resubmit your request.",
                    view=retry_view,
                    ephemeral=True
                )
            except Exception:
                pass
        finally:
            sheet_queue.task_done()
            await asyncio.sleep(0.5)

class RetryActionView(ui.View):
    def __init__(self, func, args, action_name: str):
        super().__init__(timeout=180)
        self.func = func
        self.args = args
        self.action_name = action_name

    @ui.button(label="🔄 Retry Claim", style=discord.ButtonStyle.primary)
    async def retry_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            await execute_sheet_write_with_retry(self.func, *self.args)
            await interaction.followup.send(f"✅ **{self.action_name} Successful on Retry!**", ephemeral=True)
            self.stop()
        except Exception as e:
            await interaction.followup.send(
                f"❌ Retry failed again: {str(e)}. Please try clicking Retry again in a few seconds.",
                ephemeral=True
            )

# ---------------------------------------------------------
# 📊 2. Embed Schedule Generator
# ---------------------------------------------------------
def _get_boss_embeds_sync():
    sheet_main, _, _ = _get_worksheets_sync()
    all_records = sheet_main.get_all_records()
    
    boss_list = []
    now = datetime.now()

    for row in all_records:
        name = str(row.get("BOSS NAME", "")).strip()
        rate = str(row.get("RATE", "")).strip()
        boss_up = str(row.get("BOSS UP", "") or row.get("เวลาที่จะเกิด", "") or row.get("SPAWN TIME", "")).strip()
        point_val = str(row.get("POINT", row.get("point", "0"))).strip()
        
        if not name:
            continue
            
        sort_datetime = datetime(2099, 12, 31, 23, 59)
        display_date = "???"
        display_time = "Unknown"
        
        if boss_up and "ไม่ทราบเวลา" not in boss_up and "Unknown" not in boss_up and ":" in boss_up:
            try:
                if " " in boss_up: 
                    parsed_dt = datetime.strptime(boss_up[:16], "%Y-%m-%d %H:%M")
                    sort_datetime = parsed_dt
                    display_date = parsed_dt.strftime("%Y-%m-%d")
                    display_time = parsed_dt.strftime("%H:%M")
                else: 
                    t_parsed = datetime.strptime(boss_up[:5], "%H:%M")
                    parsed_dt = now.replace(hour=t_parsed.hour, minute=t_parsed.minute, second=0, microsecond=0)
                    if parsed_dt < now - timedelta(minutes=10): 
                        parsed_dt = parsed_dt + timedelta(days=1)
                    sort_datetime = parsed_dt
                    display_date = parsed_dt.strftime("%Y-%m-%d")
                    display_time = parsed_dt.strftime("%H:%M")
            except Exception:
                pass
                
        boss_list.append({
            "name": name, 
            "rate": rate, 
            "display_date": display_date,
            "display_time": display_time,
            "point": point_val,
            "sort_key": sort_datetime
        })
        
    boss_list.sort(key=lambda x: x["sort_key"])
    
    half = (len(boss_list) + 1) // 2
    part1_list = boss_list[:half]
    part2_list = boss_list[half:]

    embed1 = discord.Embed(title="📢 LIVE BOSS SCHEDULE (PART 1)", color=discord.Color.from_rgb(47, 49, 54))
    table_text1 = "```\nBoss Name     | S.Rate | Date       | Time  | Pts\n---------------------------------------------------\n"
    for b in part1_list:
        table_text1 += f"{b['name'][:13]:<13} | {b['rate']:<6} | {b['display_date']:<10} | {b['display_time']} | {b['point']:>3}\n"
    table_text1 += "```"
    embed1.description = table_text1

    embed2 = discord.Embed(title="📢 LIVE BOSS SCHEDULE (PART 2)", color=discord.Color.from_rgb(47, 49, 54))
    table_text2 = "```\nBoss Name     | S.Rate | Date       | Time  | Pts\n---------------------------------------------------\n"
    for b in part2_list:
        table_text2 += f"{b['name'][:13]:<13} | {b['rate']:<6} | {b['display_date']:<10} | {b['display_time']} | {b['point']:>3}\n"
    table_text2 += "```"
    embed2.description = table_text2

    return [embed1, embed2]

async def refresh_main_table(interaction_or_channel=None):
    global main_setup_message
    try:
        new_embeds = await asyncio.to_thread(_get_boss_embeds_sync)
        if hasattr(interaction_or_channel, "message") and main_setup_message and interaction_or_channel.message.id == main_setup_message.id:
            await interaction_or_channel.message.edit(embeds=new_embeds)
        elif main_setup_message: 
            await main_setup_message.edit(embeds=new_embeds)
    except Exception as e:
        print(f"Failed to refresh main table: {e}")

# ---------------------------------------------------------
# 📈 3. Score Summary Generator
# ---------------------------------------------------------
def _generate_score_report_sync():
    _, sheet_score, _ = _get_worksheets_sync()
    records = sheet_score.get_all_records()

    if not records:
        embed = discord.Embed(
            title="🏆 Clan Boss Point Leaderboard",
            description="No score records found in database.",
            color=discord.Color.blue()
        )
        return embed

    user_scores = {}
    total_clan_points = 0.0

    for r in records:
        user_name = str(r.get("Display Name", "Unknown")).strip()
        try:
            score = float(r.get("Score", 0))
        except ValueError:
            score = 0.0

        user_scores[user_name] = user_scores.get(user_name, 0.0) + score
        total_clan_points += score

    sorted_scores = sorted(user_scores.items(), key=lambda x: x[1], reverse=True)

    embed = discord.Embed(
        title="🏆 Clan Boss Point Leaderboard",
        color=discord.Color.gold(),
        timestamp=datetime.now()
    )

    leaderboard_text = ""
    medals = ["🥇", "🥈", "🥉"]

    for idx, (u_name, u_score) in enumerate(sorted_scores[:10], start=1):
        prefix = medals[idx-1] if idx <= 3 else f"`#{idx:02d}`"
        leaderboard_text += f"{prefix} **{u_name}** — `{u_score:g} pts`\n"

    embed.add_field(name="⭐ Top Contributors", value=leaderboard_text or "No data", inline=False)
    embed.add_field(name="👥 Total Members", value=f"`{len(user_scores)} Members`", inline=True)
    embed.add_field(name="📊 Total Clan Points", value=f"`{total_clan_points:g} pts`", inline=True)
    embed.add_field(name="📝 Total Claims", value=f"`{len(records)} Claims`", inline=True)
    embed.set_footer(text="Boogeyman Boss Bot • Live Score System")

    return embed

async def generate_score_report_embed():
    return await asyncio.to_thread(_generate_score_report_sync)

# ---------------------------------------------------------
# ⏰ 4. Timer Loop & Normal Boss Alert
# ---------------------------------------------------------
@tasks.loop(minutes=1)
async def check_boss_timers():
    await bot.wait_until_ready()
    await refresh_main_table()

    NOTIFICATION_CHANNEL_ID = 1505457051143241849 
    channel = bot.get_channel(NOTIFICATION_CHANNEL_ID)
    if not channel:
        return

    try:
        sheet_main, _, _ = await get_worksheets()
        all_records = await asyncio.to_thread(sheet_main.get_all_records)
        now = datetime.now()
        
        for row in all_records:
            name = str(row.get("BOSS NAME", "")).strip()
            rate = str(row.get("RATE", "")).strip().upper()
            boss_up = str(row.get("BOSS UP", "") or row.get("เวลาที่จะเกิด", "") or row.get("SPAWN TIME", "")).strip()
            point_val = str(row.get("POINT", row.get("point", "0"))).strip()
            
            if not name or not boss_up or "ไม่ทราบเวลา" in boss_up or "UNKNOWN" in boss_up or ":" not in boss_up:
                continue
            
            try:
                if " " in boss_up:
                    boss_time = datetime.strptime(boss_up[:16], "%Y-%m-%d %H:%M")
                else:
                    t_parsed = datetime.strptime(boss_up[:5], "%H:%M")
                    boss_time = now.replace(hour=t_parsed.hour, minute=t_parsed.minute, second=0, microsecond=0)
                
                time_diff_minutes = (boss_time - now).total_seconds() / 60
                display_time = boss_time.strftime("%H:%M")
                display_date = boss_time.strftime("%Y-%m-%d")

                if 0.0 <= time_diff_minutes <= 1.1:
                    stage_key = f"{boss_up}_1m"
                    if notified_bosses.get(name) != stage_key:
                        notified_bosses[name] = stage_key
                        claim_status[name] = True
                        
                        alert_embed = discord.Embed(
                            title=f"⚔️ {name} ({rate}) will spawn in 1 minute!", 
                            color=discord.Color.red()
                        )
                        alert_embed.add_field(name="📅 Date", value=f"`{display_date}`", inline=True)
                        alert_embed.add_field(name="⏱️ Start Time", value=f"`{display_time}`", inline=True)
                        alert_embed.add_field(name="💎 Spawn Rate", value=f"`{rate}`", inline=True)
                        alert_embed.add_field(name="🏆 Boss Point", value=f"`{point_val} pts`", inline=True)
                        alert_embed.set_footer(text="🎯 Position at spawn point and click 'Claim Point' upon defeating the boss!")
                        
                        view = BossControlView(boss_name=name)
                        await channel.send(content="@everyone", embed=alert_embed, view=view)

            except Exception:
                continue
    except Exception as e:
        print(f"Error in timer loop: {e}")

# ---------------------------------------------------------
# 🎁 5. Claim Normal Point Modal
# ---------------------------------------------------------
class ConfirmClaimModal(ui.Modal, title="Confirm Boss Point Claim"):
    def __init__(self, user_name: str, boss_name: str, spawn_info: str, score_points: str):
        super().__init__()
        self.boss_name = boss_name
        self.score_points = score_points

        self.info_user = ui.TextInput(label="Discord Username", default=user_name, required=False)
        self.info_boss = ui.TextInput(label="Boss Name", default=boss_name, required=False)
        self.info_spawn = ui.TextInput(label="Spawn Time Window", default=spawn_info, required=False)
        self.info_score = ui.TextInput(label="Boss Points Earned", default=f"{score_points} pts", required=False)
        self.confirm_input = ui.TextInput(label="Type 'CONFIRM' to claim points", placeholder="Type CONFIRM here", required=True)

        self.add_item(self.info_user)
        self.add_item(self.info_boss)
        self.add_item(self.info_spawn)
        self.add_item(self.info_score)
        self.add_item(self.confirm_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        if self.confirm_input.value.strip().lower() not in ["confirm", "ok"]:
            await interaction.followup.send("❌ Invalid confirmation! Please type 'CONFIRM'.", ephemeral=True)
            return

        _, sheet_score, _ = await get_worksheets()
        score_records = await asyncio.to_thread(sheet_score.get_all_records)
        user_id = str(interaction.user.id)
        
        for rec in score_records:
            if str(rec.get("USER ID")) == user_id and str(rec.get("BOSS NAME")).lower() == self.boss_name.lower():
                today_str = datetime.now().strftime("%Y-%m-%d")
                if str(rec.get("DATE")) == today_str:
                    await interaction.followup.send("⚠️ You have already claimed points for this boss spawn cycle!", ephemeral=True)
                    return

        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")
        display_name = interaction.user.display_name

        try:
            score_num = float(self.score_points)
        except Exception:
            score_num = 0.0

        new_row = [user_id, display_name, score_num, self.boss_name, date_str, time_str]
        await sheet_queue.put((sheet_score.append_row, (new_row,), interaction, f"Claim Point ({self.boss_name})"))

        confirm_embed = discord.Embed(title="⏳ Boss Point Claim Queued!", color=discord.Color.blue())
        confirm_embed.add_field(name="👤 User", value=f"`{display_name}`", inline=True)
        confirm_embed.add_field(name="⚔️ Boss Name", value=f"`{self.boss_name}`", inline=True)
        confirm_embed.add_field(name="💎 Points Claimed", value=f"`+{score_num} pts`", inline=True)
        confirm_embed.set_footer(text="Your claim is queued and will be saved to Google Sheets shortly.")
        
        await interaction.followup.send(embed=confirm_embed, ephemeral=True)

# ---------------------------------------------------------
# 🎉 6. Event Boss Dropdown System
# ---------------------------------------------------------
class EventBossSelect(ui.Select):
    def __init__(self, event_records):
        options = []
        for r in event_records[:25]:
            boss_name = str(r.get("EVENT_BOSS", "")).strip()
            pts = str(r.get("Point", "0")).strip()
            pen = str(r.get("Penalty", "0")).strip()
            if boss_name:
                desc = f"Reward: +{pts} pts | Penalty: {pen} pts"
                options.append(discord.SelectOption(
                    label=boss_name[:100], 
                    description=desc[:100], 
                    value=boss_name
                ))

        super().__init__(
            placeholder="Select Event Boss...", 
            min_values=1, 
            max_values=1, 
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected_boss = self.values[0]

        _, _, sheet_event = await get_worksheets()
        event_records = await asyncio.to_thread(sheet_event.get_all_records)

        event_data = None
        for r in event_records:
            if str(r.get("EVENT_BOSS", "")).strip().lower() == selected_boss.lower():
                event_data = r
                break

        if not event_data:
            await interaction.followup.send("❌ Event Boss data not found!", ephemeral=True)
            return

        boss_name = str(event_data.get("EVENT_BOSS", selected_boss))
        pts_val = str(event_data.get("Point", "0"))
        penalty_val = str(event_data.get("Penalty", "0"))

        claim_status[boss_name] = True

        now = datetime.now()
        event_embed = discord.Embed(
            title=f"🎉 EVENT BOSS ACTIVATED: {boss_name}",
            color=discord.Color.purple(),
            timestamp=now
        )
        event_embed.add_field(name="⚔️ Event Boss", value=f"`{boss_name}`", inline=True)
        event_embed.add_field(name="🎁 Reward Point", value=f"`+{pts_val} pts`", inline=True)
        event_embed.add_field(name="⚠️ Absence Penalty", value=f"`{penalty_val} pts`", inline=True)
        event_embed.set_footer(text=f"Activated by: {interaction.user.display_name} • Click 'Claim Event Point' before event closes!")

        view = EventBossControlView(boss_name=boss_name, reward_pts=pts_val, penalty_pts=penalty_val)
        
        await interaction.channel.send(content="@everyone", embed=event_embed, view=view)
        await interaction.followup.send(f"✅ Event Boss **{boss_name}** activated successfully!", ephemeral=True)

class EventBossSelectView(ui.View):
    def __init__(self, event_records):
        super().__init__(timeout=60)
        self.add_item(EventBossSelect(event_records))

class EventBossControlView(ui.View):
    def __init__(self, boss_name: str, reward_pts: str, penalty_pts: str):
        super().__init__(timeout=None)
        self.boss_name = boss_name
        self.reward_pts = reward_pts
        self.penalty_pts = penalty_pts

    @ui.button(label="Claim Event Point", style=discord.ButtonStyle.success, emoji="🎁", custom_id="persistent_claim_event")
    async def claim_event_click(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)

        if not claim_status.get(self.boss_name, True):
            await interaction.followup.send("⛔ Event Point Claiming has been closed by Admin!", ephemeral=True)
            return

        _, sheet_score, _ = await get_worksheets()
        score_records = await asyncio.to_thread(sheet_score.get_all_records)
        user_id = str(interaction.user.id)
        today_str = datetime.now().strftime("%Y-%m-%d")

        for rec in score_records:
            if str(rec.get("USER ID")) == user_id and str(rec.get("BOSS NAME")).lower() == self.boss_name.lower():
                if str(rec.get("DATE")) == today_str:
                    await interaction.followup.send("⚠️ You have already claimed points for this Event Boss!", ephemeral=True)
                    return

        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")
        display_name = interaction.user.display_name

        try:
            score_num = float(self.reward_pts)
        except Exception:
            score_num = 0.0

        new_row = [user_id, display_name, score_num, self.boss_name, date_str, time_str]
        await sheet_queue.put((sheet_score.append_row, (new_row,), interaction, f"Claim Event ({self.boss_name})"))

        confirm_embed = discord.Embed(title="⏳ Event Point Claim Queued!", color=discord.Color.green())
        confirm_embed.add_field(name="👤 User", value=f"`{display_name}`", inline=True)
        confirm_embed.add_field(name="🎉 Event Boss", value=f"`{self.boss_name}`", inline=True)
        confirm_embed.add_field(name="💎 Points Earned", value=f"`+{score_num} pts`", inline=True)
        await interaction.followup.send(embed=confirm_embed, ephemeral=True)

    @ui.button(label="Close & Apply Penalty (Admin)", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="persistent_close_event")
    async def close_event_click(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()

        if not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("❌ Admin privileges required!", ephemeral=True)
            return

        claim_status[self.boss_name] = False

        _, sheet_score, _ = await get_worksheets()
        score_records = await asyncio.to_thread(sheet_score.get_all_records)
        today_str = datetime.now().strftime("%Y-%m-%d")

        all_known_users = {}
        claimed_users = set()

        for rec in score_records:
            u_id = str(rec.get("USER ID"))
            d_name = str(rec.get("Display Name"))
            if u_id:
                all_known_users[u_id] = d_name

            if str(rec.get("BOSS NAME")).lower() == self.boss_name.lower() and str(rec.get("DATE")) == today_str:
                claimed_users.add(u_id)

        try:
            penalty_num = float(self.penalty_pts)
        except Exception:
            penalty_num = 0.0

        absent_users = []
        if penalty_num != 0:
            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            time_str = now.strftime("%H:%M")

            for u_id, d_name in all_known_users.items():
                if u_id not in claimed_users:
                    pen_row = [u_id, d_name, penalty_num, f"{self.boss_name} (Penalty)", date_str, time_str]
                    await sheet_queue.put((sheet_score.append_row, (pen_row,), interaction, f"Penalty ({d_name})"))
                    absent_users.append(f"• **{d_name}** (`{penalty_num} pts`)")

        summary_embed = discord.Embed(
            title=f"📋 Event Closed Summary: {self.boss_name}",
            color=discord.Color.gold(),
            timestamp=datetime.now()
        )
        summary_embed.add_field(name="👥 Attendees", value=f"`{len(claimed_users)} Members Claimed`", inline=True)
        summary_embed.add_field(name="⚠️ Penalized Members", value=f"`{len(absent_users)} Members Penalized`", inline=True)
        if absent_users:
            summary_embed.add_field(name="🔻 Applied Penalty List", value="\n".join(absent_users[:15]), inline=False)
        summary_embed.set_footer(text=f"Closed by Admin: {interaction.user.display_name}")

        await interaction.channel.send(embed=summary_embed)

# ---------------------------------------------------------
# ☠️ 7. Report Slain & Skip Modals
# ---------------------------------------------------------
class KillModal(ui.Modal, title="Report Boss Slain"):
    def __init__(self, default_boss_name: str = ""):
        super().__init__()
        self.boss_input = ui.TextInput(label="Boss Name", default=default_boss_name, placeholder="e.g. Felis, Selu")
        self.time_input = ui.TextInput(label="Time of Death (HH:MM)", placeholder="e.g. 18:41")
        self.add_item(self.boss_input)
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_input = self.boss_input.value.strip().lower()
        time_val = self.time_input.value.strip()
        
        sheet_main, _, _ = await get_worksheets()
        all_records = await asyncio.to_thread(sheet_main.get_all_records)
        row_index = -1
        boss_row_data = None
        boss_name = ""
        
        for idx, row in enumerate(all_records, start=2):
            sheet_boss_name = str(row.get("BOSS NAME", "")).strip()
            if user_input in sheet_boss_name.lower():
                row_index = idx
                boss_row_data = row
                boss_name = sheet_boss_name
                break
                
        if row_index == -1:
            await interaction.followup.send(f"❌ Boss '{self.boss_input.value}' not found!", ephemeral=True)
            return

        try:
            today = datetime.now()
            parsed_time = datetime.strptime(time_val, "%H:%M")
            kill_time = today.replace(hour=parsed_time.hour, minute=parsed_time.minute, second=0, microsecond=0)
            if kill_time > today + timedelta(hours=1):
                kill_time = kill_time - timedelta(days=1)
        except ValueError:
            await interaction.followup.send("❌ Invalid time format! Must be HH:MM (e.g. 18:41)", ephemeral=True)
            return

        try:
            respawn_val = str(boss_row_data.get("TIME RESPAWN", "0")).strip()
            cooldown_hours = int(float(respawn_val)) if respawn_val else 0
        except Exception:
            cooldown_hours = 0
            
        next_spawn = kill_time + timedelta(hours=cooldown_hours)
        death_time_str = kill_time.strftime("%H:%M")
        next_spawn_sheet_str = next_spawn.strftime("%Y-%m-%d %H:%M")
        
        try:
            await execute_sheet_write_with_retry(sheet_main.update_cell, row_index, 5, death_time_str)
            await execute_sheet_write_with_retry(sheet_main.update_cell, row_index, 6, next_spawn_sheet_str)
            await refresh_main_table(interaction)
            
            announcement = discord.Embed(title=f"☠️ {boss_name} Has Been Defeated!", color=discord.Color.red())
            announcement.add_field(name="🕒 Time of Death", value=f"`{death_time_str}`", inline=True)
            announcement.add_field(name="⏳ Next Spawn Time", value=f"`{next_spawn.strftime('%Y-%m-%d %H:%M')}`", inline=True)
            announcement.set_footer(text=f"Reported by: {interaction.user.display_name}")
            
            await interaction.channel.send(embed=announcement)
            await interaction.followup.send("✅ Boss death time recorded successfully!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Database error: {str(e)}", ephemeral=True)

class SkipBossModal(ui.Modal, title="Skip Boss"):
    def __init__(self, default_boss_name: str = ""):
        super().__init__()
        self.boss_input = ui.TextInput(
            label="Boss Name", 
            default=default_boss_name, 
            placeholder="e.g. Flynt, Selu",
            required=True
        )
        self.add_item(self.boss_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_input = self.boss_input.value.strip().lower()

        sheet_main, _, _ = await get_worksheets()
        all_records = await asyncio.to_thread(sheet_main.get_all_records)
        row_index = -1
        boss_row_data = None
        boss_name = ""

        for idx, row in enumerate(all_records, start=2):
            sheet_boss_name = str(row.get("BOSS NAME", "")).strip()
            if user_input in sheet_boss_name.lower():
                row_index = idx
                boss_row_data = row
                boss_name = sheet_boss_name
                break

        if row_index == -1:
            await interaction.followup.send(f"❌ Boss '{self.boss_input.value}' not found in database!", ephemeral=True)
            return

        now = datetime.now()
        try:
            respawn_val = str(boss_row_data.get("TIME RESPAWN", "0")).strip()
            cooldown_hours = int(float(respawn_val)) if respawn_val else 0
        except Exception:
            cooldown_hours = 0

        next_spawn = now + timedelta(hours=cooldown_hours)
        skip_time_str = now.strftime("%H:%M")
        next_spawn_sheet_str = next_spawn.strftime("%Y-%m-%d %H:%M")

        try:
            await execute_sheet_write_with_retry(sheet_main.update_cell, row_index, 5, f"SKIP ({skip_time_str})")
            await execute_sheet_write_with_retry(sheet_main.update_cell, row_index, 6, next_spawn_sheet_str)
            
            await refresh_main_table(interaction)

            skip_embed = discord.Embed(
                title=f"⏭️ {boss_name} Has Been Skipped!", 
                color=discord.Color.orange()
            )
            skip_embed.add_field(name="🕒 Status", value=f"`Skipped at {skip_time_str}`", inline=True)
            skip_embed.add_field(name="⏳ Next Estimated Spawn", value=f"`{next_spawn_sheet_str}`", inline=True)
            skip_embed.set_footer(text=f"Skipped by: {interaction.user.display_name}")

            await interaction.channel.send(embed=skip_embed)
            await interaction.followup.send(f"✅ Boss **{boss_name}** skipped and next spawn time updated successfully!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Database error: {str(e)}", ephemeral=True)

# ---------------------------------------------------------
# 🔘 8. Control Panel View
# ---------------------------------------------------------
class BossControlView(ui.View):
    def __init__(self, boss_name: str = ""):
        super().__init__(timeout=None)
        self.boss_name = boss_name

    # --- ROW 1 ---
    @ui.button(label="Claim Point", style=discord.ButtonStyle.success, emoji="🎁", custom_id="persistent_claim_button", row=0)
    async def claim_click(self, interaction: discord.Interaction, button: ui.Button):
        target_boss = self.boss_name
        
        if not target_boss:
            await interaction.response.send_message("❌ Boss name not specified for claiming!", ephemeral=True)
            return

        if not claim_status.get(target_boss, True):
            await interaction.response.send_message("⛔ Point claiming for this boss has been closed by Admin!", ephemeral=True)
            return

        sheet_main, _, _ = await get_worksheets()
        all_records = await asyncio.to_thread(sheet_main.get_all_records)

        boss_row = None
        for r in all_records:
            if str(r.get("BOSS NAME", "")).strip().lower() == target_boss.lower():
                boss_row = r
                break

        if not boss_row:
            await interaction.response.send_message(f"❌ Boss '{target_boss}' not found in database!", ephemeral=True)
            return

        spawn_time_str = str(boss_row.get("BOSS UP", "") or boss_row.get("SPAWN TIME", "Unknown")).strip()
        score_val = str(boss_row.get("POINT", boss_row.get("point", "0"))).strip()

        modal = ConfirmClaimModal(
            user_name=interaction.user.display_name,
            boss_name=target_boss,
            spawn_info=spawn_time_str,
            score_points=score_val
        )
        await interaction.response.send_modal(modal)

    @ui.button(label="Cancel Claim", style=discord.ButtonStyle.primary, emoji="↩", custom_id="persistent_unclaim_button", row=0)
    async def unclaim_click(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        target_boss = self.boss_name

        _, sheet_score, _ = await get_worksheets()
        score_records = await asyncio.to_thread(sheet_score.get_all_records)
        
        row_to_delete = -1
        for idx, rec in enumerate(score_records, start=2):
            if str(rec.get("USER ID")) == user_id:
                if not target_boss or str(rec.get("BOSS NAME")).lower() == target_boss.lower():
                    row_to_delete = idx

        if row_to_delete != -1:
            await sheet_queue.put((sheet_score.delete_rows, (row_to_delete,), interaction, f"Cancel Claim ({target_boss})"))
            await interaction.followup.send("⏳ Cancel claim request queued!", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ No point claim records found to cancel!", ephemeral=True)

    @ui.button(label="Close Claim (Admin)", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="persistent_toggle_claim", row=0)
    async def toggle_claim_click(self, interaction: discord.Interaction, button: ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ This action requires Administrator privileges!", ephemeral=True)
            return

        await interaction.response.defer()
        target_boss = self.boss_name
        claim_status[target_boss] = False

        _, sheet_score, _ = await get_worksheets()
        score_records = await asyncio.to_thread(sheet_score.get_all_records)
        
        participants = []
        total_score = 0
        today_str = datetime.now().strftime("%Y-%m-%d")

        for rec in score_records:
            if str(rec.get("BOSS NAME")).lower() == target_boss.lower() and str(rec.get("DATE")) == today_str:
                p_name = rec.get("Display Name")
                p_score = rec.get("Score", 0)
                participants.append(f"• **{p_name}** (`+{p_score} pts`)")
                try:
                    total_score += float(p_score)
                except Exception:
                    pass

        report_embed = discord.Embed(
            title=f"📋 Participant Summary: {target_boss}", 
            color=discord.Color.gold(),
            timestamp=datetime.now()
        )
        report_embed.add_field(name="🔒 Status", value="`Point Claiming Closed`", inline=False)
        report_embed.add_field(name=f"👥 Total Participants ({len(participants)})", value="\n".join(participants) if participants else "No participants claimed points.", inline=False)
        report_embed.add_field(name="📊 Total Score", value=f"`{total_score} pts`", inline=False)
        report_embed.set_footer(text=f"Closed by Admin: {interaction.user.display_name}")

        await interaction.channel.send(embed=report_embed)

    @ui.button(label="Event Boss", style=discord.ButtonStyle.primary, emoji="🎉", custom_id="persistent_create_event", row=0)
    async def create_event_click(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        _, _, sheet_event = await get_worksheets()
        event_records = await asyncio.to_thread(sheet_event.get_all_records)

        if not event_records:
            await interaction.followup.send("❌ No Event Boss found in EVENT_BOSS sheet!", ephemeral=True)
            return

        view = EventBossSelectView(event_records)
        await interaction.followup.send("🎉 **Select Event Boss to Activate:**", view=view, ephemeral=True)

    # --- ROW 2 ---
    @ui.button(label="Report Slain", style=discord.ButtonStyle.danger, emoji="☠️", custom_id="persistent_kill_button", row=1)
    async def kill_click(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(KillModal(default_boss_name=self.boss_name))

    @ui.button(label="Skip Boss", style=discord.ButtonStyle.secondary, emoji="⏭", custom_id="persistent_skip_button", row=1)
    async def skip_click(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(SkipBossModal(default_boss_name=self.boss_name))

    @ui.button(label="Score Summary", style=discord.ButtonStyle.secondary, emoji="📊", custom_id="persistent_score_button", row=1)
    async def score_click(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        report_embed = await generate_score_report_embed()
        await interaction.followup.send(embed=report_embed, ephemeral=True)

# ---------------------------------------------------------
# 🚀 9. Commands & Events
# ---------------------------------------------------------
@bot.command()
async def setup(ctx):
    global main_setup_message
    try:
        await ctx.message.delete()
    except Exception:
        pass
    embeds = await asyncio.to_thread(_get_boss_embeds_sync)
    view = BossControlView()
    main_setup_message = await ctx.send(embeds=embeds, view=view)

@bot.command(name="score", aliases=["scores", "leaderboard"])
async def score_cmd(ctx):
    embed = await generate_score_report_embed()
    await ctx.send(embed=embed)

@bot.event
async def on_ready():
    bot.add_view(BossControlView())
    
    if not check_boss_timers.is_running():
        check_boss_timers.start()
        
    bot.loop.create_task(process_sheet_queue())
    print("Boogeyman Boss Bot Premium ONLINE! (Queue & Keep-Alive Ready)")

if __name__ == "__main__":
    # Start Keep-Alive Web Server
    keep_alive()
    
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("❌ ERROR: DISCORD_TOKEN Environment Variable not set!")
