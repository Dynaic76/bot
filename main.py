# main.py
# ==============================================================================
#                      IMPORTS & BASIC CONFIGURATION
# ==============================================================================
import os
import random
import subprocess
import json
import time
import threading
import shutil
from datetime import datetime
from instagrapi import Client
from instagrapi.exceptions import LoginRequired, ChallengeRequired, UserNotFound
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import requests
import logging

# ==============================================================================
#                      HOSTING-SPECIFIC CONFIGURATION
# ==============================================================================
# For Railway.app, we use a persistent volume for session data
# to avoid re-logins on every restart.
DATA_PATH = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", os.getcwd())
# On Railway, this will be /data. Locally, it will be the current directory.
# The `load_dotenv()` will load from a .env file locally. On Railway,
# it will use the environment variables you set in the dashboard.
load_dotenv()

# ==============================================================================
#                       CRITICAL PREREQUISITES CHECK
# ==============================================================================
def check_dependencies():
    """Checks if essential command-line tools are available."""
    if not shutil.which("ffmpeg"):
        logger.critical("FATAL ERROR: `ffmpeg` is not installed or not in the system's PATH.")
        logger.critical("This is required to merge video and audio. The bot will not run without it.")
        # On Railway, this check ensures your Dockerfile is working correctly.
        return False
    logger.info("✅ Dependency check passed: `ffmpeg` is available.")
    return True

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load Environment & Global Variables
USERNAME = os.getenv("IG_USERNAME")
PASSWORD = os.getenv("IG_PASSWORD")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not all([USERNAME, PASSWORD, ADMIN_USER_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    logger.critical("FATAL: Missing one or more environment variables.")
    exit(1)
if not ADMIN_USER_ID.isdigit():
    logger.critical("FATAL: ADMIN_USER_ID must be a numeric ID.")
    exit(1)
ADMIN_USER_ID = int(ADMIN_USER_ID)

REELS_FOLDER = os.path.join(DATA_PATH, "reels")
SESSION_FILE = os.path.join(DATA_PATH, f"{USERNAME}_session.json")
APPROVAL_FILE = os.path.join(DATA_PATH, "approval_flags.json")
DEMO_FILE = os.path.join(DATA_PATH, "demo_flags.json")

SOURCE_ACCOUNTS = ["terabox_links.hub", "divya_links", "duniyaa_links_ki", "mx_links"]
cl, last_update_id = Client(), 0

# ==============================================================================
#          ALL BOT FUNCTIONS (login, download, upload, etc.)
#    No changes are needed to the internal logic of these functions.
# They are included here for a complete, copy-paste ready file.
# ==============================================================================

def robust_login(max_retries=3):
    """Attempt to login with session or credentials, with retries."""
    for attempt in range(max_retries):
        try:
            if os.path.exists(SESSION_FILE):
                logger.info(f"Attempting to load session from persistent storage: {SESSION_FILE}...")
                cl.load_settings(SESSION_FILE)
                cl.login(USERNAME, PASSWORD)
                cl.get_timeline_feed()
                logger.info(f"Logged in successfully as {cl.username} using existing session.")
                return True
        except Exception as e:
            logger.warning(f"Session load failed: {e}. Attempting fresh login.")

        try:
            logger.info(f"Attempting fresh login (attempt {attempt + 1}/{max_retries})...")
            cl.login(USERNAME, PASSWORD)
            cl.dump_settings(SESSION_FILE)
            logger.info(f"Fresh login successful for {cl.username}. Session saved to {SESSION_FILE}")
            return True
        except Exception as e:
            logger.error(f"Login attempt {attempt + 1} failed: {e}")
            if "checkpoint_required" in str(e).lower():
                logger.critical("CRITICAL: Challenge required.")
                send_telegram_message("⚠️ <b>Login Failed:</b> Checkpoint required.")
                return False
            if attempt < max_retries - 1:
                time.sleep((attempt + 1) * 20)

    logger.error("All login attempts have failed.")
    return False


def send_telegram_message(text, reply_markup=None):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
        if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
        requests.post(url, json=payload, timeout=10)
    except Exception as e: logger.error(f"Failed to send Telegram message: {e}")


def send_telegram_video(video_path, caption="", reply_markup=None):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
        with open(video_path, "rb") as video_file:
            files, data = {"video": video_file}, {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
            if reply_markup: data["reply_markup"] = json.dumps(reply_markup)
            response = requests.post(url, data=data, files=files, timeout=60)
            response.raise_for_status()
            return response.json()
    except Exception as e:
        logger.error(f"Failed to send Telegram video: {e}")
        send_telegram_message(f"⚠️ <b>Error:</b> Failed to send video preview. <code>{e}</code>")
        return None


def wait_for_decision(flag_file, timeout=300):
    start_time = time.time()
    while time.time() - start_time < timeout:
        if os.path.exists(flag_file):
            try:
                with open(flag_file, 'r') as f:
                    flags = json.load(f)
                if "decision" in flags:
                    os.remove(flag_file)
                    return flags["decision"]
            except (json.JSONDecodeError, IOError):
                time.sleep(1)
                continue
        time.sleep(5)
    if os.path.exists(flag_file): os.remove(flag_file)
    return None


def download_reels(num_reels=3, is_demo=False):
    if not cl.user_id:
        send_telegram_message("⚠️ Download failed: Not logged into Instagram.")
        return False

    os.makedirs(REELS_FOLDER, exist_ok=True)
    for f in os.listdir(REELS_FOLDER): os.remove(os.path.join(REELS_FOLDER, f))
    account = random.choice(SOURCE_ACCOUNTS)
    send_telegram_message(f"🔍 <b>{'DEMO: ' if is_demo else ''}Download Started</b>\nFinding reels from <code>@{account}</code>...")

    try:
        user_id, medias = cl.user_id_from_username(account), cl.user_medias(cl.user_id_from_username(account), amount=20)
        reels = [m for m in medias if m.media_type == 2]
        if not reels: raise Exception(f"No recent reels found for @{account}.")
        random.shuffle(reels)
        reels_to_download, download_count = reels[:num_reels], 0

        for reel in reels_to_download:
            try:
                reel_url, filename = f"https://www.instagram.com/reel/{reel.code}/", os.path.join(REELS_FOLDER, f"{reel.user.username}_{reel.pk}.mp4")
                logger.info(f"Downloading reel from: {reel_url}")
                command = ["yt-dlp", "-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4", "--no-warnings", "--quiet", "-o", filename, reel_url]
                result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=True)
                download_count += 1
            except subprocess.CalledProcessError as e: logger.error(f"yt-dlp failed for reel {reel.code}. Stderr: {e.stderr.strip()}")
            except Exception as e: logger.error(f"Failed to download a specific reel ({reel.code}): {e}")

        if download_count == 0: raise Exception(f"All download attempts failed for @{account}.")
        send_telegram_message(f"✅ <b>Download Complete:</b> {download_count} reel(s) downloaded.")
        return True
    except UserNotFound:
        send_telegram_message(f"⚠️ <b>Download Error:</b> User @{account} not found.")
        return False
    except Exception as e:
        logger.error(f"Download process failed: {e}", exc_info=True)
        send_telegram_message(f"⚠️ <b>Download Error:</b>\n<code>{e}</code>")
        return False


def comment_on_sources(source_account, media_pk):
    try:
        cl.media_comment(media_pk, text=f"Great reel! Reposted with credit on @{cl.username} 🔥")
        logger.info(f"Successfully commented on source reel from @{source_account}")
        send_telegram_message(f"✅ Left a credit comment on original reel from <b>@{source_account}</b>.")
    except Exception as e: logger.error(f"Could not comment on source @{source_account}: {e}")


def upload_reel(path, source_account, media_pk):
    try:
        cl.clip_upload(path, caption=f"Credits to @{source_account} 🔥\nFollow for more!")
        logger.info(f"Successfully uploaded reel from {path}")
        send_telegram_message(f"✅ <b>Reel Posted!</b>\nSource: <code>@{source_account}</code>")
        comment_on_sources(source_account, media_pk)
        return True
    except Exception as e:
        logger.error(f"Upload failed: {e}", exc_info=True)
        send_telegram_message(f"⚠️ <b>Upload Error:</b>\n<code>{e}</code>")
        return False
    finally:
        if os.path.exists(path): os.remove(path)


def perform_demo():
    try:
        send_telegram_message("⚙️ <b>Demo Mode</b>\nDownloading test reel...")
        if not download_reels(num_reels=1, is_demo=True):
            send_telegram_message("⚠️ <b>Demo Halted:</b> Download failed.")
            return

        reels = [f for f in os.listdir(REELS_FOLDER) if f.endswith(".mp4")]
        if not reels: return
        reel_path = os.path.join(REELS_FOLDER, reels[0])
        source_account, media_pk, _ = reels[0].replace(".mp4", "").partition('_')
        send_telegram_video(
            reel_path, "📹 <b>DEMO: APPROVE POST?</b>",
            reply_markup={"inline_keyboard": [[{"text": "✅ Approve", "callback_data": "approve_demo"}, {"text": "❌ Reject", "callback_data": "reject_demo"}]]}
        )
        with open(APPROVAL_FILE, 'w') as f: json.dump({}, f)
        decision = wait_for_decision(APPROVAL_FILE, timeout=600)

        if decision is True:
            upload_reel(reel_path, source_account, media_pk)
            send_telegram_message("✅ <b>Demo Completed Successfully!</b>")
        elif decision is False: send_telegram_message("👎 <b>Demo Rejected.</b>")
        else: send_telegram_message("⏳ <b>Demo Timed Out.</b>")
        if os.path.exists(reel_path): os.remove(reel_path)
    except Exception as e: logger.error(f"Demo error: {e}", exc_info=True)


def scheduled_job():
    logger.info("--- Running Scheduled Job ---")
    reels = [f for f in os.listdir(REELS_FOLDER) if f.endswith(".mp4")]
    if not reels:
        daily_download_job()
        reels = [f for f in os.listdir(REELS_FOLDER) if f.endswith(".mp4")]
        if not reels: return

    reel_path = os.path.join(REELS_FOLDER, random.choice(reels))
    source_account, media_pk, _ = os.path.basename(reel_path).replace(".mp4", "").partition('_')
    send_telegram_video(
        reel_path, f"📹 <b>APPROVE POST?</b>\nSource: @{source_account}",
        reply_markup={"inline_keyboard": [[{"text": "✅ Approve", "callback_data": "approve_upload"}, {"text": "❌ Reject", "callback_data": "reject_upload"}]]}
    )
    with open(APPROVAL_FILE, 'w') as f: json.dump({}, f)
    decision = wait_for_decision(APPROVAL_FILE, timeout=1800)
    if decision is True: upload_reel(reel_path, source_account, media_pk)
    elif decision is False: send_telegram_message("👎 <b>Upload Rejected.</b>")
    else: send_telegram_message("⏳ <b>Approval Timed Out.</b>")
    if os.path.exists(reel_path): os.remove(reel_path)


def daily_download_job():
    logger.info("--- Running Daily Download Job ---")
    if robust_login(): download_reels(num_reels=5)
    else: send_telegram_message("⚠️ Daily download job skipped: Login failed.")


def process_telegram_update(update):
    if "callback_query" not in update: return
    cb = update["callback_query"]
    if cb["from"]["id"] != ADMIN_USER_ID: return
    flag_file, decision = None, None
    if cb["data"] in ["run_demo", "skip_demo"]:
        flag_file, decision = DEMO_FILE, (cb["data"] == "run_demo")
    elif cb["data"] in ["approve_demo", "reject_demo", "approve_upload", "reject_upload"]:
        flag_file, decision = APPROVAL_FILE, cb["data"].startswith("approve")
    if flag_file and os.path.exists(flag_file):
        with open(flag_file, "r+") as f: data = json.load(f); data["decision"] = decision; f.seek(0); json.dump(data, f); f.truncate()
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb["id"]})


def poll_telegram_updates():
    global last_update_id
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params, response = {"offset": last_update_id + 1, "timeout": 30}, requests.get(url, params=params, timeout=35)
            for update in response.json().get("result", []): process_telegram_update(update); last_update_id = update["update_id"]
        except Exception as e: logger.error(f"Telegram polling error: {e}"); time.sleep(15)

# ==============================================================================
#                             MAIN EXECUTION BLOCK
# ==============================================================================
if __name__ == "__main__":
    if not check_dependencies(): exit(1)

    scheduler = None
    try:
        os.makedirs(REELS_FOLDER, exist_ok=True)
        if not robust_login():
            send_telegram_message("❌ <b>Fatal Error:</b> Bot could not log in and is shutting down.")
            exit(1)
        
        send_telegram_message("✅ <b>Bot Online & Logged In!</b>")
        polling_thread = threading.Thread(target=poll_telegram_updates, daemon=True)
        polling_thread.start()
        
        scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
        scheduler.add_job(scheduled_job, 'cron', hour='8,14,20', misfire_grace_time=3600)
        scheduler.add_job(daily_download_job, 'cron', hour=0, minute=5, misfire_grace_time=3600)
        scheduler.start()
        
        send_telegram_message(
            "🛠️ <b>Bot Started!</b> Run a demo?",
            reply_markup={"inline_keyboard": [[{"text": "✅ Run Demo", "callback_data": "run_demo"}, {"text": "⏭️ Skip", "callback_data": "skip_demo"}]]}
        )
        with open(DEMO_FILE, 'w') as f: json.dump({}, f)
        decision = wait_for_decision(DEMO_FILE, timeout=300)
        if decision is True: perform_demo()

        logger.info("Bot is fully operational.")
        polling_thread.join()

    except (KeyboardInterrupt, SystemExit):
        if scheduler and scheduler.running: scheduler.shutdown()
        send_telegram_message("⏸️ <b>Bot shutting down.</b>")
    except Exception as e:
        logger.critical(f"A fatal error occurred: {e}", exc_info=True)
        if scheduler and scheduler.running: scheduler.shutdown()
        send_telegram_message(f"❌ <b>FATAL ERROR:</b> <code>{e}</code>")
