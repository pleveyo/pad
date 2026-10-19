import os
import re
import sys
import json
import time
import shutil
from playwright.sync_api import sync_playwright
import yt_dlp

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
DATABASE_FILE = os.path.join(APP_DIR, "active_series.json")


def load_config():
    """Loads config.json or initializes default configuration for containerized environments."""
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
    """Removes invalid filesystem characters while preserving special symbols like ★."""
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
    """Launches Chromium with headless container flags to avoid memory sandbox issues."""
    args = [
        "--disable-blink-features=AutomationControlled",
        "--mute-audio",
        "--no-sandbox",
        "--disable-dev-shm-usage"
    ]
    return p.chromium.launch(headless=not show, args=args)


def check_status_and_latest_ep(page, series_url):
    """Navigates to series page and detects total published episodes and status."""
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
    """Intercepts media stream and downloads the episode using yt-dlp."""
    episode_url = f"{series_url.rstrip('/')}/{episode_num}/"
    base_filename = f"{series_name} {episode_num:02d}"

    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs(target_dir, exist_ok=True)

    temp_template = os.path.join(temp_dir, f"{base_filename}.%(ext)s")
    temp_mp4 = os.path.join(temp_dir, f"{base_filename}.mp4")
    final_mp4 = os.path.join(target_dir, f"{base_filename}.mp4")

    print(f"\n  -> Opening Episode {episode_num:02d}: {episode_url}")
    detected_stream = {"url": None, "referer": episode_url}

    def intercept_request(request):
        url = request.url.lower()
        if (".m3u8" in url or ".mp4" in url) and not detected_stream["url"]:
            if not any(ad in url for ad in ["doubleclick", "googlesyndication", "imasdk"]):
                detected_stream["url"] = request.url
                detected_stream["referer"] = request.headers.get("referer", episode_url)
                print(f"  [+] Stream detected: {request.url[:75]}...")

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
            'quiet': False
        }

        try:
            print(f"  [*] Downloading to temporary folder: {temp_mp4}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([detected_stream["url"]])

            if os.path.exists(temp_mp4):
                print(f"  [*] Moving file to final destination: {final_mp4}")
                shutil.move(temp_mp4, final_mp4)
                print(f"  [✓] Episode {episode_num:02d} ready: {final_mp4}")
                return True
            else:
                print(f"  [!] Final file not found in temp folder for episode {episode_num:02d}.")
                return False
        except Exception as err:
            print(f"  [!] Download or move error on episode {episode_num:02d}: {err}")
            return False
    else:
        print(f"  [!] Video stream not found for episode {episode_num:02d}.")
        return False


def process_series(p, series_url, series_name, config):
    """Processes a single series: checks episodes and downloads missing ones."""
    download_dir = config["download_dir"]
    clean_name = sanitize_filename(series_name)
    target_dir = os.path.join(download_dir, clean_name)
    temp_dir = config["temp_dir"]

    os.makedirs(target_dir, exist_ok=True)
    print(f"[*] Target directory: {target_dir}")

    browser = launch_browser(p, show=config.get("show_browser", False))
    context = browser.new_context(viewport={"width": 1280, "height": 720})
    page = context.new_page()

    is_completed, latest_ep = check_status_and_latest_ep(page, series_url)
    local_episodes = get_local_episodes(target_dir, clean_name)
    all_published = set(range(1, latest_ep + 1))
    missing_episodes = sorted(list(all_published - local_episodes))

    status_str = "Completed" if is_completed else "Airing"
    print(f"[*] Status: {status_str} | Published: {latest_ep} | Local: {sorted(list(local_episodes))} | Missing: {missing_episodes}")

    all_downloaded = (latest_ep > 0)
    for ep in missing_episodes:
        success = download_episode(page, series_url, ep, target_dir, temp_dir, clean_name)
        if not success:
            all_downloaded = False

    browser.close()
    return is_completed and all_downloaded


def display_dashboard(config, active_series):
    """Displays project status overview in CLI."""
    browser_mode = "VISIBLE" if config.get("show_browser", False) else "HEADLESS (Background)"

    print("=========================================================")
    print("         PAD (Pleveyo Anime Downloader) - STATUS         ")
    print("=========================================================")
    print(f" Download directory : {config['download_dir']}")
    print(f" Temporary directory: {config['temp_dir']}")
    print(f" Browser mode       : {browser_mode}")
    print("---------------------------------------------------------")
    if not active_series:
        print(" Active tracked series (0): None")
    else:
        print(f" Active tracked series ({len(active_series)}):")
        for i, (url, name) in enumerate(active_series.items(), 1):
            print(f"   {i}. {name}  -->  {url}")
    print("=========================================================")


def main():
    config = load_config()
    active_series = load_series()

    if len(sys.argv) < 2 or sys.argv[1] in ["--status", "--list"]:
        display_dashboard(config, active_series)
        return

    with sync_playwright() as p:
        # MODE 1: Automated or manual execution via --check
        if sys.argv[1] == "--check":
            if not active_series:
                print("[*] No active series pending review.")
                return

            for url, name in list(active_series.items()):
                print(f"\n================ Checking: {name} ================")
                completed_and_full = process_series(p, url, name, config)
                if completed_and_full:
                    print(f"[✓] '{name}' is completed and fully downloaded. Removing from tracking.")
                    del active_series[url]
                    save_series(active_series)

        # MODE 2: Add or update series ("<URL> <Name>")
        else:
            url_input = sys.argv[1]
            name_input = " ".join(sys.argv[2:])
            if not name_input:
                print("[!] Error: Specify a series name after the URL.")
                return

            completed_and_full = process_series(p, url_input, name_input, config)

            if not completed_and_full:
                active_series[url_input] = name_input
                save_series(active_series)
                print(f"\n[+] '{name_input}' added to tracking list.")
            else:
                if url_input in active_series:
                    del active_series[url_input]
                    save_series(active_series)
                print(f"\n[✓] '{name_input}' is complete and up to date.")


if __name__ == "__main__":
    main()