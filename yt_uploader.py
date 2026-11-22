#!/usr/bin/env python3
"""Free local YouTube uploader: drives YouTube Studio in a dedicated Chrome
window (its own profile, logged in once by the owner). No API, no Zapier, no Drive.

  .venv/bin/python yt_uploader.py login                 # open Studio to sign in once
  .venv/bin/python yt_uploader.py check                 # is the session logged in?
  .venv/bin/python yt_uploader.py upload FILE "TITLE" "DESCRIPTION"
Prints a final line  RESULT {"ok": true, "url": "..."}  (or ok false + error).
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
PROFILE = Path.home() / "GTA6ClipperData" / "yt-chrome"
PORT = 9333
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def channel_id():
    try:
        cfg = ROOT / "config.json"
        if not cfg.exists():
            cfg = ROOT / "config.example.json"
        return json.loads(cfg.read_text()).get("youtube_channel_id", "")
    except Exception:
        return ""


def chrome_up():
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/json/version" % PORT, timeout=3).read()
        return True
    except Exception:
        return False


def ensure_chrome():
    if chrome_up():
        return
    PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [CHROME, "--user-data-dir=%s" % PROFILE, "--remote-debugging-port=%d" % PORT,
         "--no-first-run", "--no-default-browser-check", "--window-size=1280,900",
         # keep working while the screen is off / lid closed
         "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
         "--disable-backgrounding-occluded-windows",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(40):
        if chrome_up():
            return
        time.sleep(0.5)
    raise RuntimeError("Chrome did not start")


def result(ok, **kw):
    kw["ok"] = ok
    print("RESULT " + json.dumps(kw, ensure_ascii=False), flush=True)
    sys.exit(0 if ok else 1)


def logged_in(page):
    page.goto("https://studio.youtube.com/channel/%s" % channel_id(), wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    return "accounts.google.com" not in page.url and "studio.youtube.com" in page.url


def do_upload(page, path, title, desc, vis="PUBLIC"):
    page.goto("https://studio.youtube.com/channel/%s/videos/upload?d=ud" % channel_id(),
              wait_until="domcontentloaded")
    if "accounts.google.com" in page.url:
        raise RuntimeError("not logged in")
    page.set_default_timeout(120000)
    page.locator("#select-files-button").wait_for(state="visible", timeout=90000)
    # hand Chrome the local path directly (no byte copying through the debugger)
    cdp = page.context.new_cdp_session(page)
    root = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})["root"]["nodeId"]
    nid = cdp.send("DOM.querySelector", {"nodeId": root, "selector": "input[type=file][name=Filedata]"})["nodeId"]
    if not nid:
        nid = cdp.send("DOM.querySelector", {"nodeId": root, "selector": "input[type=file]"})["nodeId"]
    cdp.send("DOM.setFileInputFiles", {"files": [str(Path(path).resolve())], "nodeId": nid})

    box = page.locator("#title-textarea #textbox")
    box.wait_for(timeout=120000)
    page.wait_for_timeout(2500)          # Studio pre-fills the filename; let it settle
    box.click()
    page.keyboard.press("Meta+A")
    page.keyboard.press("Backspace")
    page.keyboard.insert_text(title[:100])

    d = page.locator("#description-textarea #textbox")
    d.click()
    page.keyboard.insert_text(desc)

    page.locator('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]').click()
    for _ in range(3):
        page.locator("#next-button").click()
        page.wait_for_timeout(1500)
    page.locator('tp-yt-paper-radio-button[name="%s"]' % vis).click()
    page.wait_for_timeout(1000)

    # wait until the file has fully uploaded (Studio keeps going server-side after that)
    deadline = time.time() + 20 * 60
    while time.time() < deadline:
        txt = (page.locator("ytcp-video-upload-progress").first.inner_text() or "").lower()
        if txt.strip() and "uploading" not in txt:
            break
        page.wait_for_timeout(3000)

    url = ""
    try:
        url = page.locator("ytcp-video-info a").first.get_attribute("href", timeout=10000) or ""
    except Exception:
        pass

    page.locator("#done-button").click()
    # success = "Video published" / "Video processing" confirmation appears
    import re as _re
    try:
        page.get_by_text(_re.compile(r"Video published|Video processing|still processing|Video saved")).first.wait_for(timeout=180000)
    except Exception:
        raise RuntimeError("no publish confirmation")
    try:
        if not url:
            url = page.locator("a[href*='youtu']").filter(has_text="youtu").first.get_attribute("href", timeout=5000) or ""
    except Exception:
        pass
    page.wait_for_timeout(2000)
    return url


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    cmd = sys.argv[1]
    ensure_chrome()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:%d" % PORT)
        ctx = browser.contexts[0]
        if cmd == "login":
            page = ctx.new_page()
            page.goto("https://studio.youtube.com/channel/%s" % channel_id())
            page.bring_to_front()
            result(True, note="sign in in the Chrome window, then leave it open")
        page = ctx.new_page()
        try:
            if cmd == "check":
                result(logged_in(page), url=page.url)
            if cmd == "upload":
                path, title = Path(sys.argv[2]), sys.argv[3]
                desc = sys.argv[4] if len(sys.argv) > 4 else ""
                vis = sys.argv[5] if len(sys.argv) > 5 else "PUBLIC"
                url = do_upload(page, path, title, desc, vis)
                result(True, url=url)
        except SystemExit:
            raise
        except Exception as e:
            try:
                shot = Path.home() / "GTA6ClipperData" / "upload_error.png"
                page.screenshot(path=str(shot))
            except Exception:
                pass
            result(False, error=str(e)[:300])
        finally:
            try:
                page.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
