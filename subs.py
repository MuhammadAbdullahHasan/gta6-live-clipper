#!/usr/bin/env python3
"""Make word-by-word animated captions (.ass) for a clip's audio.

  .venv/bin/python subs.py AUDIO.wav OUT.ass CAPTION_Y
Roman Urdu / Hinglish / English (Whisper forced to Latin script), bad words masked.
Prints the number of words captioned.
"""
import re
import sys

from faster_whisper import WhisperModel

BAD = [r"\bmaa?\s*ki\b", r"\bmadar\w*", r"\bbehen?\s*ch\w*", r"\bbhen\s*ch\w*", r"\bb\.?c\b", r"\bm\.?c\b",
       r"\bchut\w*", r"\bchod\w*", r"\bgaa?nd\w*", r"\bbhosd\w*", r"\blaud\w*", r"\blund\w*",
       r"\bkutt[ae]\b", r"\bharam\w*", r"\bfu+c*k\w*", r"\bshit\w*", r"\bbitch\w*", r"\bnigg\w*",
       r"\bass(hole)?\b", r"\bdick\w*", r"\bpussy\w*", r"\bslut\w*", r"\bwhore\w*", r"\bdamn\b"]
BAD_RE = re.compile("|".join(BAD), re.I)


def clean(word):
    return BAD_RE.sub(lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1), word)


def ts(t):
    t = max(t, 0)
    return "%d:%02d:%02d.%02d" % (t // 3600, t % 3600 // 60, int(t % 60), int((t * 100) % 100))


def main():
    wav, out, y = sys.argv[1], sys.argv[2], int(sys.argv[3])
    m = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=2)
    segs, _ = m.transcribe(wav, task="transcribe", language="en", word_timestamps=True,
                           vad_filter=True, condition_on_previous_text=False,
                           initial_prompt="Yaar bhai kya scene hai, ye dekho, chalo bhai, arey yaar, bro what.")
    words = []
    for s in segs:
        if s.no_speech_prob > 0.6:
            continue
        for w in s.words or []:
            txt = clean(w.word.strip())
            if txt and w.probability > 0.15:
                words.append((w.start, w.end, txt))
    # group into short chunks (max 3 words / 1.6 s) like pro shorts captions
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= 3 or w[1] - cur[0][0] > 1.6 or w[0] - cur[-1][1] > 0.6):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)

    head = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Impact,92,&H00FFFFFF,&H0000E5FF,&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,8,3,5,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for ch in chunks:
        end = max(ch[-1][1], ch[0][0] + 0.5)
        for i, (ws, we, _) in enumerate(ch):
            we2 = ch[i + 1][0] if i + 1 < len(ch) else end
            parts = []
            for j, (_, _, t) in enumerate(ch):
                t = t.upper()
                parts.append("{\\c&H0000E5FF&}%s{\\c&H00FFFFFF&}" % t if j == i else t)
            pop = "{\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)}" if i == 0 else ""
            lines.append("Dialogue: 0,%s,%s,Cap,,0,0,0,,{\\pos(540,%d)}%s%s"
                         % (ts(ws), ts(we2), y, pop, " ".join(parts)))
    with open(out, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(lines) + "\n")
    import json
    low = " ".join(w[2].lower() for w in words)
    hype = sum(low.count(k) for k in ["oh my god", "omg", "what", "bro", "bhai", "yaar", "no way", "wtf",
                                      "haha", "oh no", "kya", "wow", "run", "let's go", "lets go", "abe",
                                      "arey", "are yaar", "dekho", "oye", "nahi", "chalo", "insane"])
    print(json.dumps({"words": len(words), "hype": hype}))


if __name__ == "__main__":
    main()
