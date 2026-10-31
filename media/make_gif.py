"""Records media/demo.gif: four players estimate a Jira issue and save the result back (against media/mock_jira.py).

    pip install -r media/requirements.txt
    playwright install chromium     # or have Edge installed: the script falls back to it
    python media/make_gif.py
"""
import io
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageChops
from playwright.sync_api import Error as PlaywrightError, sync_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "demo.gif"
PORT = 8765
URL = f"http://127.0.0.1:{PORT}"
JIRA_URL = "http://127.0.0.1:8766"
SIZE = {"width": 1280, "height": 940}
PLAYERS = {"Alice": "5", "Bob": "8", "Carol": "5", "Dave": "5"}


def wait_for(url):
    for _ in range(50):
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"{url} did not start")


def start_servers():
    """The fake Jira and the app pointed at it. These JIRA_* values win over .env, so real credentials never show."""
    quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    jira = subprocess.Popen([sys.executable, str(HERE / "mock_jira.py")], **quiet)
    env = {**os.environ, "HOST": "127.0.0.1", "PORT": str(PORT), "JIRA_BASE_URL": JIRA_URL,
           "JIRA_EMAIL": "demo@example.com", "JIRA_TOKEN": "demo", "JIRA_FLAVOR": "cloud",
           "JIRA_DEFAULT_FILTER": "resolution = Unresolved ORDER BY Rank"}
    app = subprocess.Popen([sys.executable, str(ROOT / "poker.py")], cwd=ROOT, env=env, **quiet)
    try:
        wait_for(JIRA_URL + "/rest/api/3/project/search")
        wait_for(URL)
    except Exception:
        jira.kill()
        app.kill()
        raise
    return jira, app


def launch(pw):
    try:
        return pw.chromium.launch()
    except Exception:
        return pw.chromium.launch(channel="msedge")


def record():
    """[(image, seconds)] of Alice's screen, captured back to back."""
    jira, app = start_servers()
    frames = []
    try:
        with sync_playwright() as pw:
            browser = launch(pw)
            pages = {n: browser.new_context(viewport=SIZE, color_scheme="light").new_page() for n in PLAYERS}
            alice = pages["Alice"]
            last = [time.monotonic()]

            def snap():
                alice.mouse.move(2, 400)  # keep hover effects off the cards
                try:
                    img = Image.open(io.BytesIO(alice.screenshot())).convert("RGB")
                except PlaywrightError:  # the page is navigating: skip this frame
                    return
                now = time.monotonic()
                frames.append((img, now - last[0]))
                last[0] = now

            def hold(sec):
                """Keep recording for `sec` seconds, so animations (countdown, card flip) come out smooth."""
                end = time.monotonic() + sec
                while time.monotonic() < end:
                    snap()

            def click(selector, wait=0.7):
                alice.click(selector)
                hold(wait)

            alice.goto(URL)
            alice.wait_for_selector("#createBtn")
            hold(1.2)
            click("#projectSel")
            click('.combo-list .opt:has-text("Payments")')
            click("#createBtn", 0.1)
            alice.wait_for_selector("#nameIn")

            room_url = alice.url
            for name, page in pages.items():
                if page is not alice:
                    page.goto(room_url)
                page.fill("#nameIn", name)
                page.click("#joinBtn")
                page.wait_for_selector("#deck .dcard")
            alice.wait_for_selector("#taskList")
            hold(1.2)

            click("#fieldIn")
            click('.combo-list .opt:has-text("Story Points")')
            click("#loadBtn", 0.1)
            alice.wait_for_selector("#taskList li")
            hold(1.5)

            alice.hover('li[data-k="PAY-101"]')
            click('li[data-k="PAY-101"] [data-start]', 0.1)
            alice.wait_for_selector("#taskInfo:not([hidden])")
            hold(1.0)
            click("#descSum", 1.5)

            for name, value in PLAYERS.items():
                pages[name].click(f'#deck .dcard[data-v="{value}"]')
                hold(0.6)

            click("#revealBtn", 4.2)  # countdown, card flip, stats
            hold(1.0)

            alice.fill("#pushVal", "5")
            hold(0.5)
            click("#pushBtn", 0.1)
            alice.wait_for_selector("#toasts >> text=saved to Jira")
            hold(2.5)
            browser.close()
    finally:
        app.kill()
        jira.kill()
    return frames


def save(frames):
    # merge identical neighbours, so a still screen costs one frame however long it lasts
    merged = []
    for img, d in frames:
        if merged and ImageChops.difference(merged[-1][0], img).getbbox() is None:
            merged[-1][1] += d
        else:
            merged.append([img, d])
    # one palette built from samples of the whole recording: small file, no colour flicker between frames
    samples = [img for img, _ in merged[::max(1, len(merged) // 12)]]
    sheet = Image.new("RGB", (SIZE["width"], SIZE["height"] * len(samples)))
    for i, img in enumerate(samples):
        sheet.paste(img, (0, i * SIZE["height"]))
    pal = sheet.quantize(256, method=Image.Quantize.MEDIANCUT)
    imgs = [img.quantize(palette=pal, dither=Image.Dither.NONE) for img, _ in merged]
    durations = [max(20, round(d * 1000)) for _, d in merged]
    imgs[0].save(OUT, save_all=True, append_images=imgs[1:], duration=durations, loop=0, optimize=True)
    print(f"{OUT} ({OUT.stat().st_size // 1024} KB, {len(imgs)} frames, {sum(durations) / 1000:.1f}s)")


if __name__ == "__main__":
    save(record())
