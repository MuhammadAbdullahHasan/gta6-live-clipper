#!/usr/bin/env python3
"""GTA 6 live clipper.

Watches the streamers in streamers.txt (Twitch, YouTube, Kick, TikTok). While a
stream is live it keeps the last few minutes of video on disk. Twitch chat spikes
(anonymous IRC) or audio-loudness spikes mark a moment; the clip is cut into a
1080x1920 Short (facecam panel + gameplay), captioned word-by-word with
faster-whisper, scored, and the best clips are uploaded through YouTube Studio
(yt_uploader.py) or dropped into a synced folder for Zapier.

Run: python3 clipper.py   (see README.md)
"""
import json
import os
import random
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Heavy files live OUTSIDE Desktop: Google Drive backs up the Desktop folder.
DATA = Path.home() / "GTA6ClipperData"
WORK = DATA / "work"
CLIPS = DATA / "clips"
LOGS = ROOT / "logs"
STATE_FILE = ROOT / "state.json"
BIN = Path.home() / ".local" / "bin"
FFMPEG = str(BIN / "ffmpeg") if (BIN / "ffmpeg").exists() else "ffmpeg"
STREAMLINK = str(BIN / "streamlink") if (BIN / "streamlink").exists() else "streamlink"
FONT = "/System/Library/Fonts/Supplemental/Impact.ttf"

for d in (DATA, WORK, CLIPS, LOGS):
    d.mkdir(exist_ok=True)

_log_lock = threading.Lock()


def log(msg):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    with _log_lock:
        print(line, flush=True)
        with open(LOGS / "clipper.log", "a") as f:
            f.write(line + "\n")


def load_config():
    p = ROOT / "config.json"
    if not p.exists():
        p = ROOT / "config.example.json"
    with open(p) as f:
        return json.load(f)


def env(key, default=""):
    """Value from the environment or the local .env file (never committed)."""
    if os.environ.get(key):
        return os.environ[key]
    try:
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return default


def load_streamers():
    names = []
    for line in (ROOT / "streamers.txt").read_text().splitlines():
        line = line.strip().lower()
        if line and not line.startswith("#"):
            names.append(line)
    return names


# ---------------------------------------------------------------- state
_state_lock = threading.Lock()


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"youtube": []}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------- sources
# streamers.txt entries: "xqc" = Twitch, "yt:DuckyReloaded1" = YouTube handle
def source_url(name):
    if name.startswith("yt:"):
        return "https://www.youtube.com/@%s/live" % name[3:]
    if name.startswith("kick:"):
        return "https://kick.com/" + name[5:]
    if name.startswith("tiktok:"):
        return "https://www.tiktok.com/@%s/live" % name[7:]
    return "https://twitch.tv/" + name


def source_key(name):
    return name.replace(":", "_")


# ---------------------------------------------------------------- live check
def is_live(name):
    """Live AND playing GTA (Twitch/Kick report the category; YouTube doesn't)."""
    try:
        out = subprocess.run(
            [STREAMLINK, "--json", source_url(name)],
            capture_output=True, text=True, timeout=40,
        ).stdout
        d = json.loads(out)
        if not d.get("streams"):
            return False
        meta = d.get("metadata") or {}
        cat = (meta.get("category") or "").lower()
        title = (meta.get("title") or "").lower()
        if not cat and not title:
            return True
        return "grand theft auto" in cat or "gta" in cat or "gta" in title
    except Exception:
        return False


# ---------------------------------------------------------------- chat
HYPE = {
    "kekw", "lul", "lulw", "omegalul", "pog", "pogu", "pogchamp", "poggers",
    "monkas", "monkaw", "lmao", "lmfao", "omg", "wtf", "nah", "nahh", "w",
    "holy", "insane", "clip", "clipit", "bruh", "icant", "aware", "deadge",
    "pepelaugh", "kappa", "ez", "goated", "hahaha", "xd",
}
HYPE_CHARS = ("\U0001F480", "\U0001F602", "\U0001F923", "\U0001F62D", "\U0001F631", "\U0001F525", "\U0001F92F")
_word_re = re.compile(r"[a-z]+")


def is_hype(text):
    t = text.lower()
    if any(c in text for c in HYPE_CHARS):
        return True
    if "???" in t or "!!!" in t or "clip it" in t:
        return True
    if re.search(r"(.)\1{4,}", t):  # NAHHHHH, LMAOOOOO
        return True
    return any(w in HYPE for w in _word_re.findall(t))


class Chat(threading.Thread):
    """Anonymous read-only Twitch IRC (justinfan login, no account needed)."""

    def __init__(self, channel, stop):
        super().__init__(daemon=True)
        self.channel = channel
        self.stop = stop
        self.msgs = deque()  # (ts, hype_bool)
        self.lock = threading.Lock()
        self.started = None

    def run(self):
        while not self.stop.is_set():
            try:
                self._session()
            except Exception as e:
                log("[%s] chat reconnect: %s" % (self.channel, e))
            self.stop.wait(5)

    def _session(self):
        s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=30)
        s.sendall(b"NICK justinfan%d\r\n" % random.randint(10000, 99999))
        s.sendall(("JOIN #%s\r\n" % self.channel).encode())
        if self.started is None:
            self.started = time.time()
        buf = b""
        s.settimeout(5)
        last_rx = time.time()
        while not self.stop.is_set():
            try:
                data = s.recv(65536)
                if not data:
                    raise ConnectionError("closed")
                last_rx = time.time()
            except socket.timeout:
                if time.time() - last_rx > 360:
                    raise ConnectionError("idle")
                continue
            buf += data
            while b"\r\n" in buf:
                raw, buf = buf.split(b"\r\n", 1)
                line = raw.decode("utf-8", "ignore")
                if line.startswith("PING"):
                    s.sendall(b"PONG :tmi.twitch.tv\r\n")
                    continue
                if " PRIVMSG " in line:
                    text = line.split(" :", 1)[1] if " :" in line else ""
                    now = time.time()
                    with self.lock:
                        self.msgs.append((now, is_hype(text)))
                        while self.msgs and self.msgs[0][0] < now - 300:
                            self.msgs.popleft()
        s.close()

    def window(self, start, end):
        with self.lock:
            sel = [h for (t, h) in self.msgs if start <= t < end]
        return len(sel), sum(sel)


# ---------------------------------------------------------------- recorder
SEG_FMT = "%Y%m%d-%H%M%S"


class Recorder(threading.Thread):
    """streamlink -> ffmpeg segmenter. Sequential files; start time = file birth time."""

    def __init__(self, channel, cfg, stop, url):
        super().__init__(daemon=True)
        self.channel = channel
        self.url = url
        self.cfg = cfg
        self.stop = stop
        self.dir = WORK / channel / "buf"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.alive = True

    def run(self):
        fails = 0
        while not self.stop.is_set():
            sl = subprocess.Popen(
                [STREAMLINK, "--twitch-disable-ads", "--stream-segment-threads", "2",
                 "-O", self.url, self.cfg["quality"]],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            ff = subprocess.Popen(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                 "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                 "-f", "segment", "-segment_time", str(self.cfg["segment_seconds"]),
                 "-reset_timestamps", "1",
                 str(self.dir / ("%d_%%06d.ts" % int(time.time())))],
                stdin=sl.stdout, stderr=subprocess.DEVNULL,
            )
            sl.stdout.close()
            started = time.time()
            while not self.stop.is_set() and ff.poll() is None:
                self._cleanup()
                self.stop.wait(10)
            for p in (sl, ff):
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(10)
                    except Exception:
                        p.kill()
            if self.stop.is_set():
                break
            fails = fails + 1 if time.time() - started < 60 else 0
            if fails >= 3:
                log("[%s] recorder keeps failing, stream probably ended" % self.channel)
                self.alive = False
                break
            self.stop.wait(5)

    def _cleanup(self):
        cutoff = time.time() - self.cfg["buffer_minutes"] * 60
        for p in self.dir.glob("*.ts"):
            if seg_time(p) < cutoff:
                try:
                    p.unlink()
                except OSError:
                    pass

    def segments(self):
        return sorted((seg_time(p), p) for p in self.dir.glob("*.ts"))


def seg_time(p):
    """Wall-clock time the segment started being written (file birth time).
    Sequential names: bursty streams can deliver 2 segments in the same second."""
    try:
        st = p.stat()
        return getattr(st, "st_birthtime", st.st_ctime)
    except OSError:
        return 0


_encode_lock = threading.Lock()
_rms_re = re.compile(r"pts_time:([0-9.]+)|RMS_level=(-?[0-9.]+|-inf)")


def segment_rms(path):
    """[(offset_seconds, rms_db)] one value per second of the segment's audio."""
    r = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0?", "-af",
         "aresample=8000,asetnsamples=n=8000:p=0,astats=metadata=1:reset=1,"
         "ametadata=print:key=lavfi.astats.Overall.RMS_level",
         "-f", "null", "-"], capture_output=True, text=True)
    out, t = [], None
    for m in _rms_re.finditer(r.stderr):
        if m.group(1) is not None:
            t = float(m.group(1))
        elif t is not None:
            v = m.group(2)
            out.append((t, -90.0 if v == "-inf" else float(v)))
            t = None
    return out


# ---------------------------------------------------------------- clip builder
def layout_filter(channel, cfg, hook, credit, ass=None):
    """Professional 1080x1920 clip layout.
    Known streamer layout -> facecam panel on top + wide gameplay panel below.
    Unknown -> wide 4:3 gameplay in the middle. Blurred gameplay fills the rest."""
    W, H = 1080, 1920
    lay = cfg.get("layouts", {}).get(channel)
    keep = (lay or {}).get("keep", cfg.get("game_keep_width", 0.66))  # share of stream width kept
    g = ["[0:v]split=3[s1][s2][s3]",
         "[s1]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
         "boxblur=14:3,eq=brightness=-0.28:saturation=1.1,scale=%d:%d[bg]" % (W, H)]
    if lay:
        TOP, CAMH, GH = 170, 680, 880
        cx, cy, cw, ch = lay["cam"]
        g.append("[s2]crop=iw*%.4f:ih*%.4f:iw*%.4f:ih*%.4f,scale=%d:%d:flags=lanczos,"
                 "unsharp=5:5:0.5:3:3:0.0[cam]" % (cw, ch, cx, cy, W, CAMH))
        ar = GH / W
        g.append("[s3]crop=iw*%.3f:iw*%.3f*%.4f:(iw-iw*%.3f)/2:(ih-iw*%.3f*%.4f)/2,"
                 "scale=%d:%d:flags=lanczos,unsharp=5:5:0.4:3:3:0.0[game]"
                 % (keep, keep, ar, keep, keep, ar, W, GH))
        g.append("[bg][cam]overlay=0:%d[a1]" % TOP)
        g.append("[a1][game]overlay=0:%d,drawbox=x=0:y=%d:w=%d:h=6:color=white@0.9:t=fill[base]"
                 % (TOP + CAMH, TOP + CAMH - 3, W))
        hook_y, credit_y = 48, TOP + CAMH + GH + 50
    else:
        g.append("[s2]nullsink")
        g.append("[s3]crop=ih*4/3:ih:(iw-ih*4/3)/2:0,scale=%d:810:flags=lanczos,"
                 "unsharp=5:5:0.4:3:3:0.0[game]" % W)
        g.append("[bg][game]overlay=0:(H-h)/2[base]")
        hook_y, credit_y = 420, 1440
    last = "base"
    if hook:
        g.append("[%s]drawtext=fontfile='%s':textfile='%s':fontsize=76:fontcolor=white:"
                 "borderw=7:bordercolor=black:x=(w-tw)/2:y=%d[h1]" % (last, FONT, hook, hook_y))
        last = "h1"
    if credit:
        g.append("[%s]drawtext=fontfile='%s':textfile='%s':fontsize=40:fontcolor=white:"
                 "borderw=4:bordercolor=black:x=(w-tw)/2:y=%d[c1]" % (last, FONT, credit, credit_y))
        last = "c1"
    if ass and Path(ass).exists():
        g.append("[%s]ass='%s'[s1]" % (last, ass))
        last = "s1"
    g.append("[%s]format=yuv420p[v]" % last)
    return ";".join(g)


def build_clip(channel, rec, t, score, cfg):
    pre, post = cfg["pre_seconds"], cfg["post_seconds"]
    start, end = t - pre, t + post
    # wait until the segment covering `end` is closed (a newer one exists)
    deadline = time.time() + post + 90
    while time.time() < deadline:
        segs = rec.segments()
        if segs and segs[-1][0] > end:
            break
        time.sleep(3)
    segs = rec.segments()
    use = [(st, p) for i, (st, p) in enumerate(segs)
           if st < end and (i + 1 == len(segs) or segs[i + 1][0] > start)]
    use = [x for x in use if x != segs[-1]]  # last one may still be writing
    # (single-instance lock prevents duplicate writers; bursty streams deliver
    #  several segments within seconds, so no time-based dedup here)
    if not use:
        log("[%s] no buffered video for clip" % channel)
        return
    tmp = WORK / channel / ("cut_%d" % int(t))
    tmp.mkdir(exist_ok=True)
    try:
        joined = tmp / "joined.ts"
        with open(joined, "wb") as out:
            for _, p in use:
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, out)
        offset = max(0.0, start - use[0][0])
        dur = pre + post

        hook = None
        if cfg.get("hook_text_on"):
            hook = tmp / "hook.txt"
            hook.write_text(random.choice(cfg["hook_texts"]))
        credit = cfg.get("credits", {}).get(channel)
        ctxt = None
        if credit and credit.get("video_text"):
            ctxt = tmp / "credit.txt"
            ctxt.write_text(credit["video_text"])
        subs_info = {"words": 0, "hype": 0}
        ass = None
        if cfg.get("subtitles_on", True):
            wav = tmp / "a.wav"
            subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(joined), "-ss", "%.2f" % offset,
                            "-t", str(dur), "-ac", "1", "-ar", "16000", str(wav)])
            ass = tmp / "caps.ass"
            cap_y = 850 if cfg.get("layouts", {}).get(channel) else 1290
            try:
                r = subprocess.run([str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "subs.py"),
                                    str(wav), str(ass), str(cap_y)],
                                   capture_output=True, text=True, timeout=600)
                subs_info = json.loads(r.stdout.strip().splitlines()[-1])
            except Exception as e:
                log("[%s] subtitles failed: %s" % (channel, e))
                ass = None
        graph = layout_filter(channel, cfg, hook, ctxt, ass)
        day = datetime.now().strftime("%Y-%m-%d")
        outdir = CLIPS / day
        outdir.mkdir(exist_ok=True)
        name = "%s_%s_s%05.1f" % (datetime.fromtimestamp(t).strftime("%H%M%S"), channel, score)
        out = outdir / (name + ".mp4")
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
               "-i", str(joined), "-ss", "%.2f" % offset, "-t", str(dur),
               "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
               "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
               "-c:v", "h264_videotoolbox", "-b:v", "9M", "-maxrate", "12M",
               "-bufsize", "36M",
               "-pix_fmt", "yuv420p", "-r", "60",
               "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
               "-movflags", "+faststart", str(out)]
        with _encode_lock:  # one encode at a time, the Mac only has 2 cores
            r = subprocess.run(cmd, capture_output=True, text=True)
        try:
            got = float(re.search(r"Duration: \d+:\d+:([\d.]+)", subprocess.run(
                [FFMPEG, "-hide_banner", "-i", str(out)], capture_output=True, text=True
            ).stderr).group(1))
        except Exception:
            got = 0
        if got and got < dur * 0.75:
            log("[%s] clip too short (%.0fs, stream lagged) - skipped" % (channel, got))
            out.unlink()
            return
        if r.returncode != 0 or not out.exists() or out.stat().st_size < 50000:
            log("[%s] ffmpeg failed: %s" % (channel, r.stderr[-300:]))
            return
        try:
            info = subprocess.run([FFMPEG, "-hide_banner", "-i", str(use[0][1])],
                                  capture_output=True, text=True).stderr
            src_h = int(re.search(r", \d{3,4}x(\d{3,4})", info).group(1))
        except Exception:
            src_h = 1080
        if src_h < 1000:
            score *= 0.6
        # streamer actually reacting = better clip; silence = boring
        if subs_info["words"] < 5:
            score *= 0.5
        score *= 1 + min(subs_info["hype"], 6) * 0.08
        meta = {"channel": channel, "t": t, "score": round(score, 2), "src_h": src_h,
                "words": subs_info["words"], "hype": subs_info["hype"],
                "file": str(out), "created": time.time()}
        (outdir / (name + ".json")).write_text(json.dumps(meta))
        log("[%s] CLIP saved  score=%.1f  %s" % (channel, score, out.name))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- per-stream
class StreamWatch:
    def __init__(self, name, cfg):
        self.name = name
        self.channel = source_key(name)
        self.yt = ":" in name  # non-Twitch sources: no free chat feed -> audio spikes
        self.cfg = cfg
        self.stop = threading.Event()
        self.chat = None if self.yt else Chat(name, self.stop)
        self.rec = Recorder(self.channel, cfg, self.stop, source_url(name))
        self.det = threading.Thread(target=self._detect_audio if self.yt else self._detect,
                                    daemon=True)

    def start(self):
        log("[%s] LIVE -> recording + %s" % (self.channel, "listening for audio spikes" if self.yt else "reading chat"))
        if self.chat:
            self.chat.start()
        self.rec.start()
        self.det.start()

    def finished(self):
        return not self.rec.alive

    def close(self):
        self.stop.set()
        shutil.rmtree(WORK / self.channel, ignore_errors=True)
        log("[%s] stopped" % self.channel)

    def _detect_audio(self):
        """YouTube: no free chat feed, so detect moments where the streamer /
        game audio gets much louder than usual (screaming, laughing, chaos)."""
        cfg = self.cfg
        levels = deque()  # (wall_time, rms_db) one per second
        done = set()
        last_clip = 0
        started = time.time()
        while not self.stop.wait(3):
            segs = self.rec.segments()
            names = {p.name for _, p in segs}
            done &= names
            for st, p in segs[:-1]:  # last one is still being written
                if p.name in done:
                    continue
                done.add(p.name)
                for off, db in segment_rms(p):
                    levels.append((st + off, db))
            while levels and levels[0][0] < time.time() - 240:
                levels.popleft()
            if time.time() - started < cfg["warmup_seconds"] or len(levels) < 60:
                continue
            vals = sorted(d for _, d in levels)
            base = vals[len(vals) // 2]
            recent = [x for x in levels if x[0] > levels[-1][0] - 12]
            best, best_t = -999.0, None
            for i in range(len(recent) - 2):
                avg = sum(d for _, d in recent[i:i + 3]) / 3
                if avg > best:
                    best, best_t = avg, recent[i + 1][0]
            excess = best - base
            if base < -45:  # near-silent stream = sleep/room cam, not gameplay
                continue
            if (best_t and excess >= cfg.get("audio_spike_db", 9) and best > -35
                    and best_t - last_clip > cfg["cooldown_seconds"]):
                last_clip = best_t
                score = min(excess / 2.0, 7.0)  # keep audio scores comparable to chat scores
                log("[%s] AUDIO SPIKE  +%.1f dB over normal  score=%.1f" % (self.channel, excess, score))
                threading.Thread(target=build_clip,
                                 args=(self.channel, self.rec, best_t + 6, score, cfg),
                                 daemon=True).start()

    def _detect(self):
        cfg = self.cfg
        last_clip = 0
        while not self.stop.wait(1):
            if not self.chat.started or time.time() - self.chat.started < cfg["warmup_seconds"]:
                continue
            now = time.time()
            n5, h5 = self.chat.window(now - 5, now)
            # baseline: typical 5s message count over the previous ~3 min
            counts = []
            for k in range(3, 36):
                a = now - 5 * (k + 1)
                if self.chat.started and a < self.chat.started:
                    break
                counts.append(self.chat.window(a, a + 5)[0])
            if len(counts) < 10:
                continue
            counts.sort()
            base = counts[len(counts) // 2]
            hype = h5 / n5 if n5 else 0
            ratio = n5 / (base + 1.0)
            score = ratio * (0.5 + hype)
            spike = n5 >= max(8, 3 * base + 3) and (hype >= 0.3 or ratio >= 4)
            if spike and score >= cfg["min_score"] and now - last_clip > cfg["cooldown_seconds"]:
                # let the spike peak for a few seconds and keep the best score
                peak = score
                for _ in range(6):
                    if self.stop.wait(1):
                        return
                    m, h = self.chat.window(time.time() - 5, time.time())
                    peak = max(peak, (m / (base + 1.0)) * (0.5 + (h / m if m else 0)))
                last_clip = now
                log("[%s] SPIKE  msgs/5s=%d base=%d hype=%.0f%% score=%.1f"
                    % (self.channel, n5, base, hype * 100, peak))
                threading.Thread(target=build_clip,
                                 args=(self.channel, self.rec, now, peak, cfg),
                                 daemon=True).start()


# ---------------------------------------------------------------- publisher
def pick_title(cfg, used, channel=None):
    options = [t for t in cfg["titles"] if t not in used[-len(cfg["titles"]) + 1:]] or cfg["titles"]
    title = random.choice(options)
    tag = cfg.get("credits", {}).get(channel, {}).get("title_tag")
    name = cfg.get("names", {}).get(channel)
    full = (name + " " if name else "") + title + (" | " + tag if tag else "") + cfg.get("title_suffix", "")
    return title, full[:100]


def safe_filename(s):
    return re.sub(r'[\\/:*?"<>|]', "", s).strip()


def cleanup(cfg):
    """Keep storage tiny: delete our files from Drive once Zapier has had time to
    upload them, and delete old local clips."""
    with _state_lock:
        state = load_state()
        changed = False
        for x in state.get("youtube", []):
            age = time.time() - x["at"]
            title = Path(x["dest"]).name
            on_yt = title in state.get("uploaded_titles", [])
            # only delete once YouTube actually has it (or after 48h as a last resort)
            if not x.get("drive_deleted") and ((on_yt and age > 1800) or age > 48 * 3600):
                try:
                    Path(x["dest"]).unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    continue
                x["drive_deleted"] = True
                changed = True
                log("[cleanup] removed from Drive: %s" % Path(x["dest"]).name)
        if changed:
            save_state(state)
    cutoff = time.time() - cfg["local_keep_hours"] * 3600
    for f in CLIPS.glob("*/*"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
    for d in CLIPS.iterdir():
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()


def studio_upload(cfg, clip, title):
    """Upload through YouTube Studio in the dedicated Chrome profile (yt_uploader.py)."""
    desc = "#GTA6 #Shorts #GTA #Gaming"
    credit = cfg.get("credits", {}).get(clip.get("channel") or "", {})
    if credit.get("video_text"):
        desc = credit["video_text"] + "\n\n" + desc
    py = ROOT / ".venv" / "bin" / "python"
    try:
        out = subprocess.run([str(py), str(ROOT / "yt_uploader.py"), "upload", clip["file"], title, desc],
                             capture_output=True, text=True, timeout=30 * 60).stdout
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    for line in out.splitlines()[::-1]:
        if line.startswith("RESULT "):
            r = json.loads(line[7:])
            return r.get("ok", False), r.get("url", ""), r.get("error", "")
    return False, "", "no result: " + out[-200:]


def publisher(stop):
    while not stop.wait(60):
        try:
            cfg = load_config()
            cleanup(cfg)
            drive = Path(os.path.expanduser(cfg["drive_folder"]))
            with _state_lock:
                state = load_state()
                yt = state.setdefault("youtube", [])
                today = datetime.now().strftime("%Y-%m-%d")
                done_today = [x for x in yt if x["day"] == today]
                launch = today <= cfg.get("launch_until", "")
                per_day = cfg["launch_per_day"] if launch else cfg["youtube_per_day"]
                gap = cfg["launch_gap_minutes"] if launch else cfg["youtube_min_gap_minutes"]
                if len(done_today) >= per_day:
                    continue
                if yt and time.time() - yt[-1]["at"] < gap * 60:
                    continue
                published = {x["src"] for x in yt}
                best = None
                for mj in CLIPS.glob("*/*.json"):
                    m = json.loads(mj.read_text())
                    if m["file"] in published or not Path(m["file"]).exists():
                        continue
                    if time.time() - m["created"] > cfg["max_clip_age_hours"] * 3600:
                        continue
                    if best is None or m["score"] > best["score"]:
                        best = m
                if not best:
                    continue
                short, title = pick_title(cfg, [x.get("short") for x in yt], best.get("channel"))
                if cfg.get("upload_mode", "studio") == "studio":
                    entry = {"src": best["file"], "dest": "", "short": short, "title": title,
                             "day": today, "at": time.time(), "score": best["score"],
                             "drive_deleted": True, "status": "uploading"}
                    yt.append(entry)
                    save_state(state)
                else:
                    entry = None
            if entry is not None:
                ok, url, err = studio_upload(cfg, best, title)
                with _state_lock:
                    state = load_state()
                    for x in state["youtube"]:
                        if x["src"] == entry["src"]:
                            x["status"] = "ok" if ok else "failed"
                            x["url"] = url
                            if not ok:
                                x["day"] = "failed"   # doesn't count toward today's limit
                    save_state(state)
                if ok:
                    log("[publish] UPLOADED to YouTube: %s  %s  (score %.1f, %d/%d today)"
                        % (title, url, best["score"], len(done_today) + 1, per_day))
                else:
                    log("[publish] studio upload FAILED: %s" % err)
                    phone_notify("GTA clipper: upload fail", "YouTube Studio upload nahi hua: %s" % err[:120])
                    stop.wait(600)
                continue
            with _state_lock:
                state = load_state()
                yt = state.setdefault("youtube", [])
                if not drive.is_dir():
                    try:
                        drive.mkdir(parents=True, exist_ok=True)
                    except OSError as e:
                        log("[publish] Drive folder not reachable yet: %s" % e)
                        continue
                dest = drive / safe_filename(title)  # no extension -> YouTube title has no ".mp4"
                if dest.exists():
                    dest = drive / (safe_filename(title) + " %d" % (int(time.time()) % 1000))
                shutil.copy2(best["file"], dest)
                yt.append({"src": best["file"], "dest": str(dest), "short": short,
                           "day": today, "at": time.time(), "score": best["score"]})
                save_state(state)
                log("[publish] -> YouTube queue (Drive): %s  (score %.1f, %d/%d today)"
                    % (dest.name, best["score"], len(done_today) + 1, per_day))
        except Exception as e:
            log("[publish] error: %s" % e)


# ---------------------------------------------------------------- phone alerts
def phone_notify(title, body):
    """Optional quiet push notification: POSTs {title, body} as JSON to NOTIFY_URL
    (with NOTIFY_SECRET as the x-notify-secret header). Set both in .env."""
    import urllib.request
    url = env("NOTIFY_URL")
    if not url:
        return False
    try:
        req = urllib.request.Request(
            url, method="POST",
            data=json.dumps({"title": title, "body": body}).encode(),
            headers={"content-type": "application/json", "x-notify-secret": env("NOTIFY_SECRET")})
        urllib.request.urlopen(req, timeout=30)
        return True
    except Exception as e:
        log("[notify] failed: %s" % e)
        return False


def upload_watcher(stop):
    """Polls the channel's public feed; new video -> quiet phone notification."""
    import urllib.request
    while True:
        try:
            cfg = load_config()
            cid = cfg.get("youtube_channel_id")
            if cid:
                xml = urllib.request.urlopen(
                    "https://www.youtube.com/feeds/videos.xml?channel_id=" + cid, timeout=30
                ).read().decode("utf-8", "ignore")
                vids = re.findall(r"<yt:videoId>([^<]+)</yt:videoId>\s*<yt:channelId>[^<]*</yt:channelId>\s*<title>([^<]*)</title>", xml)
                with _state_lock:
                    state = load_state()
                    seen = state.setdefault("notified_videos", [])
                    new = [(v, t) for v, t in vids if v not in seen]
                for v, t in reversed(new):
                    import html
                    t = html.unescape(t)
                    if phone_notify("YouTube pe video upload ho gayi ✅",
                                    "%s\nyoutube.com/shorts/%s" % (t, v)):
                        log("[notify] uploaded: %s (%s)" % (t, v))
                        with _state_lock:
                            state = load_state()
                            state.setdefault("notified_videos", []).append(v)
                            state.setdefault("uploaded_titles", []).append(t)
                            save_state(state)
        except Exception as e:
            log("[notify] feed error: %s" % e)
        if stop.wait(300):
            return


def channel_stats(cfg):
    """Free public numbers: per-video views from the RSS feed + subscriber count."""
    import urllib.request, html as _h
    cid = cfg.get("youtube_channel_id")
    ua = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US"}
    xml = urllib.request.urlopen(urllib.request.Request(
        "https://www.youtube.com/feeds/videos.xml?channel_id=" + cid, headers=ua), timeout=30).read().decode("utf-8", "ignore")
    vids = []
    for e in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        t = _h.unescape(re.search(r"<title>(.*?)</title>", e).group(1))
        v = re.search(r'views="(\d+)"', e)
        vids.append((int(v.group(1)) if v else 0, t))
    page = urllib.request.urlopen(urllib.request.Request(
        "https://www.youtube.com/channel/" + cid, headers=ua), timeout=30).read().decode("utf-8", "ignore")
    m = re.search(r'"content":"([0-9.,KM]+|No) subscribers?"', page)
    subs = m.group(1) if m else "?"
    return vids, subs


def stats_reporter(stop):
    """Every 6 hours: write logs/stats.txt and send a quiet phone summary."""
    while not stop.wait(60):
        try:
            cfg = load_config()
            with _state_lock:
                state = load_state()
                last = state.get("stats_at", 0)
            if time.time() - last < cfg.get("stats_every_hours", 6) * 3600:
                continue
            vids, subs = channel_stats(cfg)
            total = sum(v for v, _ in vids)
            top = max(vids) if vids else (0, "-")
            lines = ["%s" % datetime.now().strftime("%Y-%m-%d %H:%M"),
                     "Subscribers: %s" % subs,
                     "Videos (latest 15): %d   Total views: %d" % (len(vids), total),
                     "Top: %d views - %s" % top, ""]
            lines += ["%6d  %s" % (v, t) for v, t in vids]
            (LOGS / "stats.txt").write_text("\n".join(lines) + "\n")
            phone_notify("GTA 6 Moments: %s subs, %d views" % (subs, total),
                         "%d videos. Top: %d views - %s" % (len(vids), top[0], top[1][:60]))
            log("[stats] subs=%s views=%d videos=%d" % (subs, total, len(vids)))
            with _state_lock:
                state = load_state()
                state["stats_at"] = time.time()
                save_state(state)
        except Exception as e:
            log("[stats] error: %s" % e)
            stop.wait(1800)


# ---------------------------------------------------------------- main
def main():
    import fcntl
    global _lockf
    _lockf = open(ROOT / ".clipper.lock", "w")
    try:
        fcntl.flock(_lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Clipper already running, exiting.")
        return
    log("=== GTA 6 clipper starting ===")
    stop = threading.Event()
    threading.Thread(target=publisher, args=(stop,), daemon=True).start()
    threading.Thread(target=upload_watcher, args=(stop,), daemon=True).start()
    threading.Thread(target=stats_reporter, args=(stop,), daemon=True).start()
    watching = {}
    while True:
        cfg = load_config()
        names = load_streamers()
        for n in list(watching):
            if watching[n].finished() or n not in names or not is_live(n):
                watching.pop(n).close()
        if len(watching) < cfg["max_streams"]:
            for n in names:
                if n in watching:
                    continue
                if is_live(n):
                    w = StreamWatch(n, cfg)
                    watching[n] = w
                    w.start()
                    if len(watching) >= cfg["max_streams"]:
                        break
        with open(LOGS / "status.txt", "w") as f:
            f.write("%s\nwatching: %s\n" % (datetime.now().strftime("%H:%M:%S"),
                                            ", ".join(watching) or "nobody live"))
        time.sleep(90)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("bye")
