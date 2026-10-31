"""Records media/demo.gif: four players vote and reveal in a throwaway room.

    pip install -r media/requirements.txt
    playwright install chromium     # or have Edge installed: the script falls back to it
    python media/make_gif.py
"""
import io
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).with_name("demo.gif")
PORT = 8765
URL = f"http://127.0.0.1:{PORT}"
SIZE = {"width": 1100, "height": 860}
PLAYERS = {"Alice": "5", "Bob": "8", "Carol": "5", "Dave": "13"}


def start_server():
    # empty JIRA_* values win over .env, so the demo never shows real credentials
    env = {**os.environ, "HOST": "127.0.0.1", "PORT": str(PORT), "JIRA_BASE_URL": "", "JIRA_TOKEN": ""}
    p = subprocess.Popen([sys.executable, str(ROOT / "poker.py")], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            urllib.request.urlopen(URL, timeout=1)
            return p
        except OSError:
            time.sleep(0.2)
    p.kill()
    raise RuntimeError("server did not start")


def launch(pw):
    try:
        return pw.chromium.launch()
    except Exception:
        return pw.chromium.launch(channel="msedge")


def main():
    server = start_server()
    frames = []  # (image, duration ms)

    try:
        with sync_playwright() as pw:
            browser = launch(pw)
            pages = {}
            for name in PLAYERS:
                ctx = browser.new_context(viewport=SIZE, color_scheme="light")
                pages[name] = ctx.new_page()
            alice = pages["Alice"]

            def snap(ms):
                alice.mouse.move(2, 400)
                png = alice.screenshot()
                frames.append((Image.open(io.BytesIO(png)).convert("RGB"), ms))

            alice.goto(URL)
            alice.wait_for_selector("#createBtn")
            snap(1800)
            alice.click("#createBtn")
            alice.wait_for_selector("#nameIn")

            room_url = alice.url
            for name, page in pages.items():
                if page is not alice:
                    page.goto(room_url)
                page.fill("#nameIn", name)
                page.click("#joinBtn")
                page.wait_for_selector("#deck .dcard")
                if page is alice:
                    snap(1200)
            time.sleep(0.5)
            snap(1200)

            for name, value in PLAYERS.items():
                pages[name].click(f'#deck .dcard[data-v="{value}"]')
                time.sleep(0.5)
                snap(900)

            alice.click("#revealBtn")
            time.sleep(0.8)
            snap(600)  # countdown
            alice.wait_for_function("document.querySelector('#results') && document.querySelector('#results').innerText.trim().length > 0")
            time.sleep(1.2)  # card flip animation
            snap(3500)

            alice.click("#resetBtn")
            time.sleep(0.8)
            snap(1500)
            browser.close()
    finally:
        server.kill()

    # one shared palette keeps the GIF small and stops colours flickering between frames
    pal = frames[-2][0].quantize(256, method=Image.Quantize.MEDIANCUT)
    imgs = [im.quantize(palette=pal, dither=Image.Dither.NONE) for im, _ in frames]
    imgs[0].save(OUT, save_all=True, append_images=imgs[1:], duration=[d for _, d in frames], loop=0, optimize=True)
    print(f"{OUT} ({OUT.stat().st_size // 1024} KB, {len(frames)} frames)")


if __name__ == "__main__":
    main()
