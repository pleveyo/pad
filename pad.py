import os
import re
import sys
import json
import time
import shutil
import logging
import threading
import schedule
from flask import Flask, render_template, request, redirect, url_for
from playwright.sync_api import sync_playwright
import yt_dlp

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
DATABASE_FILE = os.path.join(APP_DIR, "active_series.json")
LOG_FILE = os.path.join(APP_DIR, "pad.log")

# Setup logging to write to both stdout and a log file
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)

is_running = False
job_lock = threading.Lock()

app = Flask(__name__)


def load_config():
    """Loads config.json or returns default configuration."""
    default_config = {
        "download_dir": "/downloads",
        "temp_dir": "/temp",
        "check_schedules": ["08:00", "20:00"],
        "show_browser": False
    }

    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default_config, f, indent=4, ensure_ascii=False)
        return default_config

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    updated = False
    for key, value in default_config.items():
        if key not in data:
            data[key] = value
            updated = True

    if updated:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    return data


def load_series():
    """Loads active series list from database file."""
    if os.path.exists(DATABASE_FILE):
        with open(DATABASE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_series(series_dict):
    """Saves active series list into database file."""
    with open(DATABASE_FILE, "w", encoding="utf-8") as f:
        json.dump(series_dict, f, indent=4, ensure_ascii=False)


def sanitize_filename(name):
    """Removes invalid filesystem characters."""
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def get_local_episodes(target_dir, clean_name):
    """Checks which episode files already exist in target directory."""
    existing = set()
    if not os.path.exists(target_dir):
        return existing

    pattern = re.compile(
        rf'^{re.escape(clean_name)}(?:\s+-\s+E|\s+)(\d+)\.mp4$',
        re.IGNORECASE
    )

    for filename in os.listdir(target_dir):
        match = pattern.match(filename)
        if match:
            existing.add(int(match.group(1)))
    return existing


def launch_browser(p, show=False):
    """Launches Chromium with headless container arguments."""
    args = [
        "--disable-blink-features=AutomationControlled",
        "--mute-audio",
        "--no-sandbox",
        "--disable-dev-shm-usage"
    ]
    return p.chromium.launch(headless=not show, args=args)


def check_status_and_latest_ep(page, series_url):
    """Inspects series page to determine state and total episodes."""
    page.goto(series_url, wait_until="domcontentloaded")
    time.sleep(2)

    while True:
        try:
            view_more_button = page.locator("text='Ver más'").first
            if view_more_button.is_visible():
                view_more_button.click()
                time.sleep(1.5)
            else:
                break
        except Exception:
            break

    html = page.content()
    page_text = page.inner_text("body")

    is_completed = "Concluido" in page_text or "Finalizado" in page_text
    episode_candidates = [0]

    slug = series_url.rstrip("/").split("/")[-1]
    episode_links = re.findall(rf'{re.escape(slug)}/(\d+)/', html)
    if episode_links:
        episode_candidates.extend([int(e) for e in episode_links])

    latest_match = re.search(r'Último episodio:.*?- (\d+)', page_text, re.IGNORECASE)
    if latest_match:
        episode_candidates.append(int(latest_match.group(1)))

    if is_completed:
        total_episodes_match = re.search(r'Episodios:\s*(\d+)', page_text, re.IGNORECASE)
        if total_episodes_match and int(total_episodes_match.group(1)) > 0:
            episode_candidates.append(int(total_episodes_match.group(1)))

    range_matches = re.findall(r'Ep\s+\d+\s*-\s*(\d+)', page_text, re.IGNORECASE)
    if range_matches:
        episode_candidates.extend([int(e) for e in range_matches])

    return is_completed, max(episode_candidates)


def download_episode(page, series_url, episode_num, target_dir, temp_dir, series_name):
    """Intercepts media stream and triggers download."""
    episode_url = f"{series_url.rstrip('/')}/{episode_num}/"
    base_filename = f"{series_name} {episode_num:02d}"

    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs(target_dir, exist_ok=True)

    temp_template = os.path.join(temp_dir, f"{base_filename}.%(ext)s")
    temp_mp4 = os.path.join(temp_dir, f"{base_filename}.mp4")
    final_mp4 = os.path.join(target_dir, f"{base_filename}.mp4")

    logging.info(f"Opening Episode {episode_num:02d}: {episode_url}")
    detected_stream = {"url": None, "referer": episode_url}

    def intercept_request(request):
        url = request.url.lower()
        if (".m3u8" in url or ".mp4" in url) and not detected_stream["url"]:
            if not any(ad in url for ad in ["doubleclick", "googlesyndication", "imasdk"]):
                detected_stream["url"] = request.url
                detected_stream["referer"] = request.headers.get("referer", episode_url)
                logging.info(f"Stream detected: {request.url[:75]}...")

    page.on("request", intercept_request)
    page.goto(episode_url, wait_until="domcontentloaded")
    time.sleep(3)

    if not detected_stream["url"]:
        try:
            iframe = page.locator("iframe").first
            box = iframe.bounding_box()
            if box:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        except Exception:
            pass

    retry_count = 0
    while not detected_stream["url"] and retry_count < 15:
        time.sleep(1)
        retry_count += 1

    page.remove_listener("request", intercept_request)

    if detected_stream["url"]:
        user_agent = page.evaluate("() => navigator.userAgent")
        ydl_opts = {
            'outtmpl': temp_template,
            'merge_output_format': 'mp4',
            'http_headers': {
                'Referer': detected_stream["referer"],
                'User-Agent': user_agent
            },
            'concurrent_fragment_downloads': 4,
            'quiet': True
        }

        try:
            logging.info(f"Downloading to temp directory: {temp_mp4}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([detected_stream["url"]])

            if os.path.exists(temp_mp4):
                logging.info(f"Moving file to final location: {final_mp4}")
                shutil.move(temp_mp4, final_mp4)
                logging.info(f"Episode {episode_num:02d} completed successfully: {final_mp4}")
                return True
            else:
                logging.error(f"File not found in temp folder: {temp_mp4}")
                return False
        except Exception as err:
            logging.error(f"Download/move error on episode {episode_num:02d}: {err}")
            return False
    else:
        logging.warning(f"Video stream not detected for episode {episode_num:02d}.")
        return False


def process_series(p, series_url, series_name, config):
    """Processes a single series: checks episodes and downloads missing ones."""
    download_dir = config["download_dir"]
    clean_name = sanitize_filename(series_name)
    target_dir = os.path.join(download_dir, clean_name)
    temp_dir = config["temp_dir"]

    os.makedirs(target_dir, exist_ok=True)
    logging.info(f"Checking series '{clean_name}' at {target_dir}")

    browser = launch_browser(p, show=config.get("show_browser", False))
    context = browser.new_context(viewport={"width": 1280, "height": 720})
    page = context.new_page()

    is_completed, latest_ep = check_status_and_latest_ep(page, series_url)
    local_episodes = get_local_episodes(target_dir, clean_name)
    all_published = set(range(1, latest_ep + 1))
    missing_episodes = sorted(list(all_published - local_episodes))

    status_str = "Completed" if is_completed else "Airing"
    logging.info(f"Status: {status_str} | Published: {latest_ep} | Local: {sorted(list(local_episodes))} | Missing: {missing_episodes}")

    all_downloaded = (latest_ep > 0)
    for ep in missing_episodes:
        success = download_episode(page, series_url, ep, target_dir, temp_dir, clean_name)
        if not success:
            all_downloaded = False

    browser.close()
    return is_completed and all_downloaded


def run_full_check():
    """Worker routine that checks all tracked series."""
    global is_running
    with job_lock:
        is_running = True
        try:
            logging.info("Starting global check...")
            config = load_config()
            active_series = load_series()

            if not active_series:
                logging.info("No active series pending review.")
                return

            with sync_playwright() as p:
                for url, name in list(active_series.items()):
                    logging.info(f"--- Processing: {name} ---")
                    completed_and_full = process_series(p, url, name, config)
                    if completed_and_full:
                        logging.info(f"Series '{name}' is completed and fully downloaded. Removing from tracking.")
                        del active_series[url]
                        save_series(active_series)

            logging.info("Global check finished successfully.")
        except Exception as e:
            logging.error(f"Unexpected error during global check: {e}")
        finally:
            is_running = False


def scheduler_loop():
    """Background scheduler thread running checks at configured intervals."""
    config = load_config()
    schedules = config.get("check_schedules", ["08:00", "20:00"])

    for sched_time in schedules:
        schedule.every().day.at(sched_time).do(
            lambda: threading.Thread(target=run_full_check, daemon=True).start()
        )
        logging.info(f"Scheduled automated check daily at {sched_time}")

    while True:
        schedule.run_pending()
        time.sleep(30)


@app.route("/")
def index():
    series = load_series()
    log_content = ""
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
                log_content = "".join(lines[-150:])  # Show latest 150 lines
        except Exception:
            log_content = "Failed to load log file."
    else:
        log_content = "No logs recorded yet."

    message = request.args.get("message")
    return render_template("index.html", series=series, logs=log_content, is_running=is_running, message=message)


@app.route("/add", methods=["POST"])
def add_series():
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()

    if name and url:
        series = load_series()
        series[url] = name
        save_series(series)
        logging.info(f"Added series: '{name}' ({url})")
        # Trigger background check for newly added series
        if not is_running:
            threading.Thread(target=run_full_check, daemon=True).start()
        return redirect(url_for("index", message=f"Added '{name}'. Check started in background."))

    return redirect(url_for("index", message="Error: Both name and URL are required."))


@app.route("/delete", methods=["POST"])
def delete_series():
    url = request.form.get("url")
    if url:
        series = load_series()
        if url in series:
            removed_name = series.pop(url)
            save_series(series)
            logging.info(f"Removed series: '{removed_name}' ({url})")
            return redirect(url_for("index", message=f"Removed '{removed_name}'."))

    return redirect(url_for("index"))


@app.route("/run-check", methods=["POST"])
def trigger_check():
    global is_running
    if not is_running:
        threading.Thread(target=run_full_check, daemon=True).start()
        return redirect(url_for("index", message="Manual check initiated."))
    else:
        return redirect(url_for("index", message="A check is already running."))


if __name__ == "__main__":
    # Start scheduler daemon thread
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()

    # Start Flask Web Interface
    app.run(host="0.0.0.0", port=5000)