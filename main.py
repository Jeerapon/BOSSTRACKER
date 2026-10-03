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
# 1. SETUP DISCORD BOT & TIMEZONE
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# กำหนด Timezone ประเทศไทย (UTC+7)
THAI_TZ = datetime.timezone(datetime.timedelta(hours=7))

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
        # สำหรับ รันในเครื่อง local (ถ้ามีไฟล์ credentials.json)
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPE)
    client = gspread.authorize(creds)
    return client

gc = init_gspread()
# เปลี่ยนชื่อ Google Sheet ของคุณให้ตรงตรงนี้ (เช่น "ERICA5")
sheet = gc.open("ERICA5").sheet1 

# คิวสำหรับเข้าแถวบันทึกข้อมูล ป้องกัน Rate Limit 429 Error
sheet_queue = asyncio.Queue()

async def sheet_worker():
    """Worker คอยดึงงานจาก Queue ไปเขียนลง Google Sheets ทีละรายการ"""
    while True:
        task_func, args, kwargs, future = await sheet_queue.get()
        success = False
        for attempt in range(3): # สั่ง Retry สูงสุด 3 ครั้งหากมีปัญหา
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
        await asyncio.sleep(1.2) # ชะลอเวลา 1.2 วินาที ป้องกัน Google API Block

async def add_to_sheet_queue(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    await sheet_queue.put((func, args, kwargs, future))
    return await future

# ==========================================
# 3. BOSS ALERT TASK LOOP (แจ้งเตือนล่วงหน้า 1 นาที)
# ==========================================
# ช่องที่จะให้บอทยิงข้อความแจ้งเตือนบอส (จะถูกอัปเดตอัตโนมัติเมื่อรัน !setup)
ALERT_CHANNEL_ID = None 

# บันทึกบอสที่เตือนไปแล้ว ป้องกันการยิงเตือนซ้ำในนาทีเดียวกัน
notified_bosses = set()

@tasks.loop(seconds=30)
async def check_boss_alerts():
    """ตรวจสอบเวลาบอสจาก Google Sheets ทุกๆ 30 วินาที"""
    global ALERT_CHANNEL_ID, notified_bosses
    if not ALERT_CHANNEL_ID:
        return

    try:
        # อ่านข้อมูลจาก Sheet ผ่าน Queue Safe Function
        data = await add_to_sheet_queue(sheet.get_all_records)
        now_thai = datetime.datetime.now(THAI_TZ)
        current_time_str = now_thai.strftime("%Y-%m-%d %H:%M")

        channel = bot.get_channel(ALERT_CHANNEL_ID)
        if not channel:
            return

        for row in data:
            boss_name = row.get("Boss Name") or row.get("Boss")
            date_str = str(row.get("Date", "")).strip()
            time_str = str(row.get("Time", "")).strip()

            if not boss_name or not date_str or not time_str:
                continue

            try:
                # แปลงเวลาบอสเกิดเป็น datetime object
                boss_time_str = f"{date_str} {time_str}"
                boss_dt = datetime.datetime.strptime(boss_time_str, "%Y-%m-%d %H:%M").replace(tzinfo=THAI_TZ)
                
                # คำนวณเวลาเตือนล่วงหน้า 1 นาที
                alert_dt = boss_dt - timedelta(minutes=1)
                
                # key สำหรับเช็กการเตือนซ้ำ
                alert_key = f"{boss_name}_{boss_time_str}"

                # ถ้าเวลาปัจจุบันตรงกับเวลาแจ้งเตือน (หรือห่างไม่เกิน 1 นาที) และยังไม่เคยเตือน
                if alert_dt.strftime("%Y-%m-%d %H:%M") == current_time_str and alert_key not in notified_bosses:
                    embed = discord.Embed(
                        title="⚔️ บอสใกล้จะเกิดแล้ว!",
                        description=f"**{boss_name}** กำลังจะเกิดภายใน **1 นาที!**\n⏰ เวลาเกิด: `{time_str}` น.",
                        color=discord.Color.red()
                    )
                    await channel.send(content="@here", embed=embed)
                    notified_bosses.add(alert_key) # บันทึกว่าเตือนแล้ว

            except ValueError:
                continue # ข้ามกรณี Format เวลาใน Sheet ไม่ถูกต้อง

    except Exception as e:
        print(f"[Alert Loop Error]: {e}")

# ==========================================
# 4. DISCORD EVENT & COMMANDS
# ==========================================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user.name}")
    print("🚀 Boogeyman Boss Bot Premium ONLINE! (Queue & Keep-Alive Ready)")
    
    # รัน Queue Worker
    asyncio.create_task(sheet_worker())
    
    # รัน Background Loop แจ้งเตือนบอส
    if not check_boss_alerts.is_running():
        check_boss_alerts.start()

@bot.command()
async def setup(ctx):
    """คำสั่งสำหรับตั้งค่าเปิดแผงควบคุมบอสและบันทึก Channel แจ้งเตือน"""
    global ALERT_CHANNEL_ID
    ALERT_CHANNEL_ID = ctx.channel.id
    
    embed = discord.Embed(
        title="⚔️ ERICA5 LIVE BOSS SCHEDULE",
        description="กดปุ่มด้านล่างเพื่อทำการเคลมคะแนน / รายงานบอสถูกจัดการ",
        color=discord.Color.blue()
    )
    
    # หมายเหตุ: สามารถเพิ่ม View หรือ Button Components ของคุณต่อตรงนี้ได้
    await ctx.send(embed=embed)

# ==========================================
# 5. MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    keep_alive() # เปิด Flask Server รับ Ping จาก cron-job.org
    TOKEN = os.getenv("DISCORD_TOKEN")
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("❌ Error: DISCORD_TOKEN Environment Variable is missing!")
