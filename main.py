import os
import json
import asyncio
import datetime
from datetime import timedelta
import discord
from discord.ext import commands, tasks
import gspread
from google.oauth2.service_account import Credentials
from keep_alive import keep_alive

# ==========================================
# 1. SETUP DISCORD BOT & CONFIGURATION
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ⚠️ สำคัญมาก: นำ Channel ID ของช่องที่ต้องการให้ยิงเตือนมาใส่ตรงนี้
# (คลิกขวาที่ชื่อช่องใน Discord -> Copy Channel ID)
ALERT_CHANNEL_ID = 1505457051143241849  # <--- เปลี่ยนเป็น ID ช่องของคุณ (เป็นตัวเลข ไม่มีเครื่องหมายอัญประกาศ)

# กำหนด Timezone ประเทศไทย (UTC+7)
THAI_TZ = datetime.timezone(datetime.timedelta(hours=7))

# บันทึกประวัติการเตือนเพื่อป้องกันการยิงข้อความซ้ำในนาทีเดียวกัน
notified_bosses = set()

# ==========================================
# 2. GOOGLE SHEETS SETUP & ASYNC QUEUE
# ==========================================
SCOPE = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def init_gspread():
    creds_json = os.getenv("GOOGLE_CREDENTIALS")
    if creds_json:
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPE)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPE)
    client = gspread.authorize(creds)
    return client

gc = init_gspread()
sheet = gc.open("ERICA5").sheet1 # ตรวจสอบชื่อ Google Sheet ให้ถูกต้อง

sheet_queue = asyncio.Queue()

async def sheet_worker():
    """Worker คอยดึงงานไปเขียน/อ่าน Google Sheets ทีละคิว ป้องกัน Rate Limit 429 Error"""
    while True:
        task_func, args, kwargs, future = await sheet_queue.get()
        success = False
        for attempt in range(3):
            try:
                result = await asyncio.to_thread(task_func, *args, **kwargs)
                if not future.done():
                    future.set_result(result)
                success = True
                break
            except Exception as e:
                print(f"[Queue Worker Error] Attempt {attempt+1}: {e}")
                await asyncio.sleep(2)
        
        if not success and not future.done():
            future.set_exception(Exception("Failed to update Google Sheets after 3 retries."))
        
        sheet_queue.task_done()
        await asyncio.sleep(1.2)

async def add_to_sheet_queue(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    await sheet_queue.put((func, args, kwargs, future))
    return await future

# ==========================================
# 3. BOSS ALERT TASK LOOP (แจ้งเตือนล่วงหน้า 1 นาที)
# ==========================================
@tasks.loop(seconds=30)
async def check_boss_alerts():
    global ALERT_CHANNEL_ID, notified_bosses
    if not ALERT_CHANNEL_ID or ALERT_CHANNEL_ID == 1234567890123456789:
        print("[Alert Loop Warning] ยังไม่ได้ระบุ ALERT_CHANNEL_ID ที่ถูกต้อง")
        return

    try:
        data = await add_to_sheet_queue(sheet.get_all_records)
        now_thai = datetime.datetime.now(THAI_TZ)
        current_time_str = now_thai.strftime("%H:%M") # เวลาปัจจุบัน (ชั่วโมง:นาที)
        current_date_str = now_thai.strftime("%Y-%m-%d")

        channel = bot.get_channel(int(ALERT_CHANNEL_ID))
        if not channel:
            print(f"[Alert Loop Error] หา Channel ID {ALERT_CHANNEL_ID} ไม่เจอ")
            return

        for row in data:
            boss_name = str(row.get("Boss Name") or row.get("Boss") or "").strip()
            time_str = str(row.get("Time") or "").strip()

            if not boss_name or not time_str:
                continue

            # แปลง Format เวลาใน Sheet ให้เป็น HH:MM เสมอ
            time_parts = time_str.split(":")
            if len(time_parts) >= 2:
                formatted_boss_time = f"{int(time_parts[0]):02d}:{int(time_parts[1]):02d}"
            else:
                continue

            # คำนวณเวลาเตือนล่วงหน้า 1 นาที
            try:
                boss_dt = datetime.datetime.strptime(formatted_boss_time, "%H:%M")
                alert_dt = boss_dt - timedelta(minutes=1)
                alert_time_str = alert_dt.strftime("%H:%M")

                alert_key = f"{boss_name}_{current_date_str}_{formatted_boss_time}"

                # เช็กว่าเวลาปัจจุบัน ตรงกับ เวลาเตือนล่วงหน้า 1 นาที หรือไม่
                if current_time_str == alert_time_str and alert_key not in notified_bosses:
                    embed = discord.Embed(
                        title="⚔️ บอสกำลังจะเกิดแล้ว!",
                        description=f"**{boss_name}** กำลังจะเกิดภายใน **1 นาที!**\n⏰ เวลาเกิด: `{formatted_boss_time}` น.",
                        color=discord.Color.red()
                    )
                    await channel.send(content="@here", embed=embed)
                    notified_bosses.add(alert_key)
                    print(f"✅ [SUCCESS] ยิงเตือนบอส {boss_name} (เกิด {formatted_boss_time}) เรียบร้อยแล้วที่เวลา {current_time_str}")

            except Exception as parse_err:
                continue

    except Exception as e:
        print(f"[Alert Loop Error]: {e}")

# ==========================================
# 4. DISCORD EVENT & COMMANDS
# ==========================================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user.name}")
    print("🚀 Boogeyman Boss Bot Premium ONLINE! (Queue & Alert Loop Active)")
    
    # รัน Queue Worker สำหรับ Google Sheets
    asyncio.create_task(sheet_worker())
    
    # รัน Background Loop เช็กเวลาเตือนบอส
    if not check_boss_alerts.is_running():
        check_boss_alerts.start()

@bot.command()
async def setup(ctx):
    """คำสั่งสร้างแผงตารางควบคุมบอส"""
    global ALERT_CHANNEL_ID
    ALERT_CHANNEL_ID = ctx.channel.id # อัปเดต ID ช่องอัตโนมัติเมื่อพิมพ์ !setup
    
    embed = discord.Embed(
        title="⚔️ ERICA5 LIVE BOSS SCHEDULE",
        description="กดปุ่มด้านล่างเพื่อทำการเคลมคะแนน / รายงานบอสถูกจัดการ",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed)
    print(f"📌 [Setup] บันทึก Channel ID แจ้งเตือนสำเร็จ: {ALERT_CHANNEL_ID}")

# ==========================================
# 5. MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    keep_alive() # เปิด Web Server ให้ Cron-job ยิงสะกิดกันบอทหลับ
    TOKEN = os.getenv("DISCORD_TOKEN")
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("❌ Error: ไม่พบ DISCORD_TOKEN ใน Environment Variables")
