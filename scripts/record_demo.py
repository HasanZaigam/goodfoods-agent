#!/usr/bin/env python3
"""Record the GoodFoods demo video automatically.

Starts the Streamlit app, drives every user journey in a real browser, records the
screen with on-screen captions, then (if ffmpeg is installed) speeds up the waits
while the model is thinking and writes an MP4.

Usage (from the repo root):
    .venv/bin/pip install playwright && .venv/bin/python -m playwright install chromium
    .venv/bin/python scripts/record_demo.py              # headless, writes demo/goodfoods_demo.mp4
    .venv/bin/python scripts/record_demo.py --headed     # watch it run
    .venv/bin/python scripts/record_demo.py --app path/to/app.py

Nothing here touches your .env: the app reads its own config exactly as when you run it.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PHONE = "9876543210"
CODE_RE = re.compile(r"GF-[A-Z0-9]{6}")

# (scene title, caption shown during the scene, messages). "{code}" is filled with the
# confirmation code booked in scene 1, so the cancel scene cancels that same booking.
SCENES = [
    ("1 · Recommendation → availability → booking",
     "Goodie recommends outlets, checks live availability, reads the booking back and books only after an explicit 'yes'.",
     ["Suggest a romantic place for our anniversary this Saturday at 8 pm, around ₹1500 per person, 2 of us",
      "The first one sounds good, please check it",
      f"Name is Hasan Zaigam, mobile {PHONE}",
      "yes, confirm"]),
    ("2 · Fully booked → alternatives",
     "When a slot is full, Goodie offers other times at the same outlet or the same time at a sister branch.",
     ["Table for 6 at GoodFoods Andhra Spice - BTM Layout this Saturday at 8 pm"]),
    ("3 · Hinglish + dish + relative date",
     "'kal raat 8 baje' is resolved to a real date in code; 'biryani' is mapped to the right outlets.",
     ["Kal raat 8 baje 2 logon ke liye Indiranagar mein biryani wali jagah suggest karo"]),
    ("4 · Look up & cancel",
     "The booking from scene 1 is looked up with code + phone and cancelled only after confirmation.",
     [f"I want to cancel my booking {{code}}, my phone is {PHONE}",
      "yes, cancel it"]),
    ("5 · Edge cases",
     "Groups over 20 go to the events team · off-topic requests are declined · past dates are rejected.",
     ["We are 25 people for an office party next Friday at 8 pm",
      "Also, can you write me a poem about cats?",
      "Ok then book a table for 2 yesterday at 8 pm"]),
]

INTRO = ("GoodFoods · Goodie",
         "AI reservation agent for a 75-outlet restaurant chain<br><br>"
         "Streamlit UI · agent loop built from scratch (no LangChain)<br>"
         "Tools served over MCP · LLM decides intent via tool calls<br>"
         "Small open MoE model (Nemotron Nano, ~3B active) via NVIDIA NIM")
OUTRO = ("Thanks for watching",
         "Guardrails in code: confirm-before-book · relative dates · party-size limits<br>"
         "Unit tests + scripted end-to-end journeys (docs/TRANSCRIPTS.md)<br>"
         "Business strategy, ROI & expansion: docs/USE_CASE.md<br><br>"
         "Hasan Zaigam")


# --------------------------------------------------------------------------- app

def find_app(explicit: str | None) -> Path:
    if explicit:
        return (ROOT / explicit).resolve()
    for name in ("app.py", "streamlit_app.py", "ui/app.py", "goodfoods/app.py", "goodfoods/ui/app.py"):
        if (ROOT / name).exists():
            return ROOT / name
    for p in sorted(ROOT.rglob("*.py")):
        if ".venv" in p.parts or "site-packages" in p.parts:
            continue
        try:
            if "st.chat_input" in p.read_text(errors="ignore"):
                return p
        except OSError:
            pass
    sys.exit("Could not find the Streamlit app. Pass it with --app path/to/app.py")


def start_app(app: Path, port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(app), "--server.port", str(port),
         "--server.headless", "true", "--browser.gatherUsageStats", "false"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    url = f"http://localhost:{port}/_stcore/health"
    for _ in range(120):
        try:
            if urllib.request.urlopen(url, timeout=1).status == 200:
                return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    sys.exit("Streamlit did not start. Try running it manually: streamlit run " + str(app))


# --------------------------------------------------------------------------- overlays

OVERLAY_JS = """
([id, html, style]) => {
  let el = document.getElementById(id);
  if (!html) { if (el) el.remove(); return; }
  if (!el) { el = document.createElement('div'); el.id = id; document.body.appendChild(el); }
  el.setAttribute('style', style); el.innerHTML = html;
}
"""
CARD_STYLE = ("position:fixed;inset:0;z-index:2147483647;display:flex;flex-direction:column;justify-content:center;"
              "align-items:center;text-align:center;background:#0f172a;color:#f8fafc;"
              "font-family:-apple-system,Segoe UI,Roboto,sans-serif;padding:40px;")
CAPTION_STYLE = ("position:fixed;top:0;left:0;right:0;z-index:2147483646;background:rgba(15,23,42,.92);color:#f8fafc;"
                 "font-family:-apple-system,Segoe UI,Roboto,sans-serif;padding:10px 18px;font-size:15px;"
                 "line-height:1.35;box-shadow:0 2px 8px rgba(0,0,0,.25);")
BADGE_STYLE = ("position:fixed;bottom:110px;right:24px;z-index:2147483646;background:#f59e0b;color:#111;"
               "font-family:-apple-system,Segoe UI,Roboto,sans-serif;padding:6px 12px;border-radius:999px;"
               "font-size:13px;font-weight:600;")


HIDE_CHROME_CSS = ('header[data-testid="stHeader"],[data-testid="stToolbar"],[data-testid="stDecoration"],'
                   '[data-testid="stStatusWidget"]{visibility:hidden!important}'
                   '[data-testid="stMain"],section.main{padding-top:64px}')


def overlay(page: Page, oid: str, html: str | None, style: str = "") -> None:
    page.evaluate(OVERLAY_JS, [oid, html, style])


def hide_chrome(page: Page) -> None:
    """Hide Streamlit's header/Deploy toolbar so captions sit cleanly on top."""
    page.add_style_tag(content=HIDE_CHROME_CSS)


def card(page: Page, title: str, body: str, secs: float) -> None:
    overlay(page, "demo-card",
            f"<div style='font-size:40px;font-weight:700;margin-bottom:24px'>{title}</div>"
            f"<div style='font-size:20px;line-height:1.6;color:#cbd5e1'>{body}</div>", CARD_STYLE)
    page.wait_for_timeout(int(secs * 1000))
    overlay(page, "demo-card", None)


def caption(page: Page, title: str, text: str) -> None:
    overlay(page, "demo-caption", f"<b>{title}</b><br>{text}", CAPTION_STYLE)


# --------------------------------------------------------------------------- chat driving

def scroll_bottom(page: Page) -> None:
    page.evaluate("""() => {
      for (const s of ['[data-testid="stMain"]', '[data-testid="stAppViewContainer"]', 'section.main'])
        document.querySelectorAll(s).forEach(e => e.scrollTo(0, e.scrollHeight));
      window.scrollTo(0, document.body.scrollHeight);
    }""")


def app_busy(page: Page) -> bool:
    return page.evaluate("""() => {
      if (document.querySelector('[data-testid="stSpinner"]')) return true;
      const w = document.querySelector('[data-testid="stStatusWidget"]');
      return !!(w && /running|stop/i.test(w.textContent || ''));
    }""")


def messages(page: Page):
    return page.locator('[data-testid="stChatMessage"]')


def send(page: Page, text: str, waits: list, t0: float, timeout: float = 300) -> str:
    box = page.locator('[data-testid="stChatInput"] textarea').first
    box.wait_for(state="visible", timeout=60_000)
    before = messages(page).count()
    box.click()
    page.keyboard.type(text, delay=22)
    page.wait_for_timeout(400)
    page.keyboard.press("Enter")

    start = time.monotonic()
    overlay(page, "demo-badge", "⏳ Goodie is thinking… (sped up in video)", BADGE_STYLE)
    last, stable_since = None, None
    while time.monotonic() - start < timeout:
        page.wait_for_timeout(500)
        scroll_bottom(page)
        n = messages(page).count()
        if n < before + 2 or app_busy(page):
            stable_since = None
            continue
        txt = messages(page).nth(n - 1).inner_text()
        if txt.strip() and txt == last:
            if stable_since and time.monotonic() - stable_since > 1.5:
                break
            stable_since = stable_since or time.monotonic()
        else:
            last, stable_since = txt, time.monotonic()
    else:
        print(f"  ! no reply within {timeout}s for: {text[:50]}")
    end = time.monotonic()
    overlay(page, "demo-badge", None)
    waits.append((start - t0 + 0.8, end - t0))  # keep the first ~0.8 s at normal speed
    scroll_bottom(page)
    reply = messages(page).nth(messages(page).count() - 1).inner_text()
    print(f"  you:    {text}\n  goodie: {reply.strip()[:140].replace(chr(10), ' ')}")
    page.wait_for_timeout(3500)  # time to read the reply
    return reply


# --------------------------------------------------------------------------- ffmpeg

def speed_up(src: Path, dst: Path, waits: list, factor: float) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    segs, cur = [], 0.0
    for a, b in sorted(waits):
        if b - a < 1.0 or a < cur:
            continue
        segs += [(cur, a, 1.0), (a, b, factor)]
        cur = b
    segs.append((cur, None, 1.0))
    parts, labels = [], []
    for i, (a, b, f) in enumerate(segs):
        end = f":end={b:.3f}" if b is not None else ""
        parts.append(f"[0:v]trim=start={a:.3f}{end},setpts=(PTS-STARTPTS)/{f}[v{i}]")
        labels.append(f"[v{i}]")
    graph = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(segs)}:v=1:a=0[out]"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-filter_complex", graph, "-map", "[out]",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "22", "-r", "30", str(dst)]
    return subprocess.run(cmd).returncode == 0


# --------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app", help="Streamlit app file (auto-detected if omitted)")
    ap.add_argument("--port", type=int, default=8599)
    ap.add_argument("--out", default="demo", help="output folder (default: demo/)")
    ap.add_argument("--speedup", type=float, default=6.0, help="speed-up factor for model thinking time")
    ap.add_argument("--headed", action="store_true", help="show the browser while recording")
    ap.add_argument("--no-seed", action="store_true", help="skip resetting the demo database")
    args = ap.parse_args()

    out = (ROOT / args.out).resolve()
    raw_dir = out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    if not args.no_seed:
        print("Resetting demo data…")
        subprocess.run([sys.executable, "-m", "goodfoods.data.seed", "--reset"], cwd=ROOT, check=False)

    app = find_app(args.app)
    print(f"Starting {app.relative_to(ROOT)} on port {args.port}…")
    proc = start_app(app, args.port)
    url = f"http://localhost:{args.port}"
    waits: list[tuple[float, float]] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            ctx = browser.new_context(viewport={"width": 1280, "height": 800},
                                      record_video_dir=str(raw_dir),
                                      record_video_size={"width": 1280, "height": 800})
            page = ctx.new_page()
            t0 = time.monotonic()
            page.goto(url)
            page.locator('[data-testid="stChatInput"] textarea').first.wait_for(timeout=60_000)
            hide_chrome(page)
            card(page, *INTRO, secs=6)

            code = None
            for title, text, msgs in SCENES:
                page.goto(url)  # fresh session for every scene
                page.locator('[data-testid="stChatInput"] textarea').first.wait_for(timeout=60_000)
                hide_chrome(page)
                card(page, title, text, secs=3.5)
                caption(page, title, text)
                print(f"\n== {title}")
                for m in msgs:
                    if "{code}" in m:
                        if not code:
                            print("  ! no confirmation code from scene 1, skipping cancel scene")
                            break
                        m = m.replace("{code}", code)
                    reply = send(page, m, waits, t0)
                    found = CODE_RE.findall(reply)
                    if found and title.startswith("1"):
                        code = found[-1]
                page.wait_for_timeout(1500)
                overlay(page, "demo-caption", None)

            card(page, *OUTRO, secs=6)
            video = page.video
            ctx.close()
            browser.close()
            raw = Path(video.path())
    finally:
        proc.terminate()

    webm = out / "goodfoods_demo_raw.webm"
    shutil.move(str(raw), webm)
    shutil.rmtree(raw_dir, ignore_errors=True)
    mp4 = out / "goodfoods_demo.mp4"
    if speed_up(webm, mp4, waits, args.speedup):
        print(f"\nDone: {mp4}  (thinking time sped up {args.speedup:g}x; raw recording: {webm.name})")
    else:
        print(f"\nDone: {webm}\n(Install ffmpeg with `brew install ffmpeg` and re-run to get a shorter MP4.)")


if __name__ == "__main__":
    main()