import os
import random
from datetime import datetime
from instagrapi import Client
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv
import subprocess

# Load environment variables
load_dotenv()

USERNAME = os.getenv("IG_USERNAME")
PASSWORD = os.getenv("IG_PASSWORD")

SOURCE_ACCOUNTS = [
    "terabox_links.hub",
    "divya_links",
    "duniyaa_links_ki",
    "mx_links"
]

REELS_FOLDER = "reels"
cl = Client()

def login():
    if os.path.exists("session.json"):
        cl.load_settings("session.json")
    cl.login(USERNAME, PASSWORD)
    cl.dump_settings("session.json")

def download_reels():
    os.makedirs(REELS_FOLDER, exist_ok=True)
    for user in SOURCE_ACCOUNTS:
        url = f"https://www.instagram.com/{user}/reels"
        subprocess.call(["yt-dlp", "-P", REELS_FOLDER, url])

def upload_reel():
    files = os.listdir(REELS_FOLDER)
    if not files:
        print("No reels found to upload.")
        return
    reel = random.choice(files)
    path = os.path.join(REELS_FOLDER, reel)
    caption = f"🔥 Repost 📅 {datetime.now().strftime('%d-%m-%Y')}"
    cl.clip_upload(path, caption)
    os.remove(path)

def comment_on_sources():
    for user in SOURCE_ACCOUNTS:
        user_id = cl.user_id_from_username(user)
        medias = cl.user_medias(user_id, 1)
        if medias:
            cl.media_comment(medias[0].id, "All the latest videos Link In Bio")

def notify(message):
    user_id = cl.user_id_from_username("linuxlifestyle")
    cl.direct_send(message, [user_id])

def job():
    try:
        upload_reel()
        comment_on_sources()
        notify("✅ Successfully posted and commented")
    except Exception as e:
        notify(f"❌ Error: {str(e)}")

if __name__ == "__main__":
    login()
    download_reels()
    scheduler = BlockingScheduler()
    scheduler.add_job(job, 'cron', hour=8)
    scheduler.add_job(job, 'cron', hour=14)
    scheduler.add_job(job, 'cron', hour=20)
    scheduler.start()
