"""
Subtitle tool for Sourcer.

Pipeline:
  1. User uploads a video.
  2. ffmpeg extracts a compact mono audio track (chunked if long) that fits the
     gateway's 20MB inline limit.
  3. Each chunk is transcribed to word-level timestamps by Gemini 2.5 Flash,
     reached through the *Vercel AI Gateway* using only AI_GATEWAY_API_KEY
     (same key/transport the Media-Sourcing project uses — replicated here in
     pure Python, no Node SDK).
  4. Words are grouped into subtitle cues (breaks at sentence/clause boundaries
     and pauses, with max chars/words + a minimum on-screen time).
  5. The UI previews and lets the user edit cues, then picks a style.
  6. Cues + style compile to an ASS file, which ffmpeg burns into the video
     (libass) — or is exported as .srt / .ass sidecars.

Only AI_GATEWAY_API_KEY is required. This module does not touch the reference
project; it merely mirrors its gateway-Gemini transcription approach.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import subprocess
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------- providers
#
# Word-timestamp transcription, mirroring Media-Sourcing-Auto's provider chain:
#   1. Deepgram Nova-3 — purpose-built STT, ~10-50ms word timing (the source of
#      that project's tight caption sync). Whole audio in one call (2GB cap),
#      so there are no chunk-boundary seams.
#   2. Gemini 2.5 Flash via the Vercel AI Gateway — fallback when no
#      DEEPGRAM_API_KEY. Chunked to fit the 20MB inline limit; each chunk's
#      timeline is normalized against its ffprobe-measured duration because
#      Gemini's clock drifts on long clips.

GATEWAY_URL = "https://ai-gateway.vercel.sh/v4/ai/language-model"
GATEWAY_MODEL = "google/gemini-2.5-flash"
GEMINI_INLINE_LIMIT = 18 * 1024 * 1024  # keep margin under the 20MB gateway cap

# language=multi: auto-handles non-English speech (nova-3 code-switching).
DEEPGRAM_URL = (
    "https://api.deepgram.com/v1/listen"
    "?model=nova-3&language=multi&punctuate=true&smart_format=true&filler_words=true"
)


def deepgram_key() -> str:
    return (os.environ.get("DEEPGRAM_API_KEY") or "").strip()


def _deepgram_transcribe(audio_bytes: bytes, mime: str = "audio/mpeg") -> list[dict]:
    """One whole-file transcription via Deepgram Nova-3 (word timestamps)."""
    req = urllib.request.Request(
        DEEPGRAM_URL,
        data=audio_bytes,
        headers={"Authorization": f"Token {deepgram_key()}", "Content-Type": mime},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=600)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")[:300]
        raise RuntimeError(f"Deepgram transcription failed (HTTP {e.code}): {detail}")
    data = json.loads(resp.read().decode())
    raw = (
        (((data.get("results") or {}).get("channels") or [{}])[0].get("alternatives")
         or [{}])[0].get("words") or []
    )
    words = []
    for w in raw:
        if (isinstance(w.get("word"), str)
                and isinstance(w.get("start"), (int, float))
                and isinstance(w.get("end"), (int, float))
                and w["end"] >= w["start"]):
            words.append({
                # punctuated_word reads better on screen ("Hello," vs "hello")
                "word": w.get("punctuated_word") or w["word"],
                "start": float(w["start"]),
                "end": float(w["end"]),
            })
    if not words:
        raise RuntimeError("Deepgram returned no word timestamps")
    return words


def gateway_key() -> str:
    key = (os.environ.get("AI_GATEWAY_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "AI_GATEWAY_API_KEY is not set. Put it in Sourcer's .env "
            "(same Vercel AI Gateway key used by the Media-Sourcing project)."
        )
    return key


_TRANSCRIBE_PROMPT = (
    "Transcribe the attached audio with precise word-level timestamps.\n"
    'Return ONLY valid JSON matching this schema:\n'
    '{ "words": [ { "word": "<token>", "start": <seconds>, "end": <seconds> } ] }\n'
    "Rules:\n"
    "- start/end are seconds from the START of THIS audio clip (decimals allowed).\n"
    "- Include EVERY spoken word in order; keep punctuation attached to its word.\n"
    "- Transcribe speech only; ignore music/sound effects.\n"
    "- Do not invent words. If a language other than English is spoken, transcribe it "
    "in its native script.\n"
    "- Output JSON only, no markdown fences."
)


def _gateway_transcribe(audio_bytes: bytes, mime: str = "audio/mpeg") -> list[dict]:
    """One transcription call through the Vercel AI Gateway → Gemini 2.5 Flash."""
    body = {
        "prompt": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _TRANSCRIBE_PROMPT},
                    {
                        "type": "file",
                        "data": {"type": "data",
                                 "data": base64.b64encode(audio_bytes).decode()},
                        "mediaType": mime,
                    },
                ],
            }
        ],
        "temperature": 0,
    }
    req = urllib.request.Request(
        GATEWAY_URL,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {gateway_key()}",
            "ai-gateway-protocol-version": "0.0.1",
            "ai-language-model-specification-version": "4",
            "ai-language-model-id": GATEWAY_MODEL,
            "ai-language-model-streaming": "false",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")[:400]
        raise RuntimeError(f"Gateway transcription failed (HTTP {e.code}): {detail}")
    data = json.loads(resp.read().decode())
    parts = data.get("content") or []
    text = ""
    for p in parts:
        if p.get("type") == "text":
            text += p.get("text", "")
    return _parse_words(text)


def _parse_words(text: str) -> list[dict]:
    clean = text.replace("```json", "").replace("```", "").strip()
    m = re.search(r"\{[\s\S]*\}", clean)
    if not m:
        raise RuntimeError("Transcription returned no JSON")
    parsed = json.loads(m.group(0))
    words = []
    for w in parsed.get("words", []):
        if (isinstance(w.get("word"), str)
                and isinstance(w.get("start"), (int, float))
                and isinstance(w.get("end"), (int, float))
                and w["end"] >= w["start"]):
            words.append({"word": w["word"], "start": float(w["start"]),
                          "end": float(w["end"])})
    return words


# ---------------------------------------------------------------- ffmpeg audio

def probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def has_audio_stream(path: str) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    return bool(out.stdout.strip())


def _extract_audio(video: str, out_mp3: str, start: float, dur: float, kbps: int = 48):
    cmd = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", video, "-t", f"{dur:.3f}",
           "-vn", "-ac", "1", "-ar", "16000", "-b:a", f"{kbps}k", out_mp3]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Audio extraction failed: {r.stderr[-300:]}")


def normalize_timeline(words: list[dict], total_dur: float) -> list[dict]:
    """Bound word times to the measured audio (port of the reference project's
    normalizeTimeline). Gross overflow (timeline runs well past the real audio —
    the classic Gemini drift) rescales the whole timeline to fit; every word is
    then clamped into [0, total_dur] with end >= start. Correct timelines pass
    through untouched (scale stays 1)."""
    if not words or total_dur <= 0:
        return words
    max_end = max(w["end"] for w in words)
    scale = total_dur / max_end if max_end > total_dur * 1.2 else 1.0
    out = []
    for w in words:
        start = min(max(0.0, w["start"] * scale), total_dur)
        end = min(max(start, w["end"] * scale), total_dur)
        out.append({"word": w["word"], "start": start, "end": end})
    return out


def word_hygiene(words: list[dict]) -> list[dict]:
    """Monotonic, non-inverted, no zero-length hangs (port of the reference's
    post-alignment hygiene). Bad word timing is what makes captions freeze or
    karaoke sweeps jump backwards: enforce ordered starts, a tiny minimum
    duration, and cap each word at the next word's start."""
    MIN_WORD = 0.02
    cursor = 0.0
    n = len(words)
    for i, w in enumerate(words):
        s = max(w["start"], cursor)
        e = max(w["end"], s + MIN_WORD)
        if i + 1 < n:
            nxt = max(words[i + 1]["start"], s + MIN_WORD)
            if e > nxt:
                e = nxt
        w["start"] = round(s, 3)
        w["end"] = round(e, 3)
        cursor = s
    return words


def transcribe_video(video_path: str, work_dir: Path, progress=None) -> list[dict]:
    """Extract audio and return an absolute-time word list.

    Deepgram Nova-3 first (whole file, one call — tightest sync); gateway
    Gemini as fallback (chunked, per-chunk timeline normalization).
    """
    if not has_audio_stream(video_path):
        raise RuntimeError("This video has no audio track to transcribe.")
    total = probe_duration(video_path)
    if total <= 0:
        raise RuntimeError("Could not read video duration.")

    kbps = 48

    # --- Provider 1: Deepgram (whole audio, no chunk seams) -----------------
    if deepgram_key():
        mp3 = str(work_dir / "full_audio.mp3")
        _extract_audio(video_path, mp3, 0.0, total, kbps)
        if progress:
            progress(0, 1)
        try:
            words = _deepgram_transcribe(Path(mp3).read_bytes())
            measured = probe_duration(mp3) or total
            return word_hygiene(normalize_timeline(words, measured))
        finally:
            try:
                os.remove(mp3)
            except OSError:
                pass

    # --- Provider 2: gateway Gemini (chunked) -------------------------------
    est_bytes = total * (kbps * 1000 / 8)
    n_chunks = max(1, math.ceil(est_bytes / GEMINI_INLINE_LIMIT))
    # Shorter chunks than the inline limit requires: Gemini's timestamp drift
    # grows with clip length, so tighter chunks keep each timeline anchored.
    max_chunk = 300.0
    n_chunks = max(n_chunks, math.ceil(total / max_chunk))
    chunk_dur = total / n_chunks

    all_words: list[dict] = []
    for i in range(n_chunks):
        start = i * chunk_dur
        dur = min(chunk_dur, total - start) + (0.0 if i == n_chunks - 1 else 0.25)
        mp3 = str(work_dir / f"chunk_{i}.mp3")
        _extract_audio(video_path, mp3, start, dur, kbps)
        if progress:
            progress(i, n_chunks)
        words = _gateway_transcribe(Path(mp3).read_bytes())
        # Anchor this chunk's timeline to its real length before offsetting —
        # unchecked Gemini drift is the main cause of captions lagging the voice.
        measured = probe_duration(mp3) or dur
        words = normalize_timeline(words, measured)
        for w in words:
            all_words.append({
                "word": w["word"],
                "start": round(w["start"] + start, 3),
                "end": round(w["end"] + start, 3),
            })
        try:
            os.remove(mp3)
        except OSError:
            pass
    all_words.sort(key=lambda w: w["start"])
    return word_hygiene(all_words)


# ---------------------------------------------------------------- cue builder

SENTENCE_END = re.compile(r"[.!?…]['\")\]]?$")
CLAUSE_END = re.compile(r"[,;:—]$")

# Connector words that should never end a cue — the phrase they introduce
# belongs with them, so they slide to the next cue (Netflix TTSG / BBC rule,
# via the reference project's caption pager).
CONNECTORS = frozenset(
    "the a an of to in on at by for and or but with as is are was were be "
    "his her its our their my your this that these those".split()
)


def build_cues(words: list[dict], max_chars=42, max_words=7,
               pause=0.45, min_dur=0.8, hold_after=0.3) -> list[dict]:
    """Group word timings into subtitle cues that break where a human subtitler
    would — sentence/clause boundaries and silences — with broadcast-style
    display timing (rules adapted from the reference project's caption pager):
      • hard break after sentence-final punctuation
      • break at silences >= `pause`; the cue then hides `hold_after` later
        instead of lingering through the gap
      • never end a cue on a connector word (it slides to the next cue)
      • no orphans: a 1-word final cue steals a word from the previous cue
      • seamless transitions: a cue stays up until the next one appears when
        the gap is short (no flicker between cues)
      • minimum display time so short cues don't strobe
    """
    groups: list[list[dict]] = []
    cur: list[dict] = []

    for i, w in enumerate(words):
        cur.append(w)
        text_len = sum(len(x["word"]) + 1 for x in cur)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["start"] - w["end"]) if nxt else 0.0
        end_sentence = bool(SENTENCE_END.search(w["word"]))
        end_clause = bool(CLAUSE_END.search(w["word"]))
        is_connector = w["word"].strip(".,;:!?…\"'").lower() in CONNECTORS
        should_break = (
            end_sentence
            or len(cur) >= max_words
            or text_len >= max_chars
            or gap >= pause
            or (end_clause and len(cur) >= 3)
        )
        # Connector rule: don't break right after "the"/"of"/… unless a real
        # silence forces it — the next phrase belongs with its connector.
        if should_break and is_connector and gap < pause and nxt is not None:
            should_break = len(cur) >= max_words + 2  # safety valve
        if should_break and cur:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    # Orphan rescue: a lone-word final group steals the previous group's last
    # word so captions never end on a stranded single word.
    if len(groups) >= 2 and len(groups[-1]) == 1 and len(groups[-2]) >= 3:
        groups[-1].insert(0, groups[-2].pop())

    cues: list[dict] = []
    for g in groups:
        cues.append({
            "start": round(g[0]["start"], 3),
            "end": round(g[-1]["end"], 3),
            "text": " ".join(w["word"] for w in g).strip(),
            "words": [{"w": w["word"], "s": round(w["start"], 3),
                       "e": round(w["end"], 3)} for w in g],
        })

    # Display timing. Between back-to-back cues leave a small breathing gap
    # (broadcast two-frame rule) so each caption reads as a new beat instead of
    # one wall of morphing text; at real silences hold briefly then hide. The
    # word timestamps inside `words` stay intact for karaoke/word styles.
    INTER_GAP = 0.08
    for i, c in enumerate(cues):
        nxt = cues[i + 1] if i + 1 < len(cues) else None
        if nxt:
            gap = nxt["start"] - c["end"]
            if gap < pause:
                # run up to just shy of the next cue — the tiny gap is the beat
                c["end"] = max(c["start"] + 0.2, nxt["start"] - INTER_GAP)
            else:
                c["end"] = c["end"] + hold_after  # hide shortly after speech
        # Minimum on-screen time, never into the next cue's slot.
        if c["end"] - c["start"] < min_dur:
            c["end"] = c["start"] + min_dur
        if nxt and c["end"] > nxt["start"] - INTER_GAP:
            c["end"] = max(c["start"] + 0.2, nxt["start"] - INTER_GAP)
        c["end"] = round(c["end"], 3)
    return cues


def reconcile_words(cues: list[dict]) -> list[dict]:
    """Make each cue's word list agree with its (possibly hand-edited) text.

    The editor is supposed to keep `words` in sync as the user types, but the
    burned output must never trust that: word-driven styles render from
    `words`, so any drift silently resurrects the pre-edit transcription.
    Same token count → keep real timings, swap the text in. Different count →
    spread the cue's span evenly across the new tokens.
    """
    for c in cues:
        ws = c.get("words") or []
        tokens = (c.get("text") or "").split()
        if not ws:
            continue
        if not tokens:
            c["words"] = None
            continue
        if len(tokens) == len(ws) and all(w.get("w") == t for w, t in zip(ws, tokens)):
            continue  # already in sync
        if len(tokens) == len(ws):
            for w, t in zip(ws, tokens):
                w["w"] = t
        else:
            dur = max(0.01, float(c["end"]) - float(c["start"]))
            n = len(tokens)
            c["words"] = [
                {"w": t,
                 "s": round(c["start"] + dur * i / n, 3),
                 "e": round(c["start"] + dur * (i + 1) / n, 3)}
                for i, t in enumerate(tokens)
            ]
    return cues


# ---------------------------------------------------------------- ASS export

@dataclass
class SubtitleStyle:
    font: str = "Arial"
    size: int = 54            # px at 1080p reference height
    weight: int = 700         # font thickness 100 (thin) .. 900 (black)
    primary: str = "#FFFFFF"  # text color
    accent: str = "#FFE24D"   # karaoke / highlight color
    outline_color: str = "#000000"
    outline: float = 3.0
    shadow: float = 1.0
    italic: bool = False
    uppercase: bool = False
    background: str = "none"  # none | box | blur (blur approximated as translucent box)
    box_opacity: float = 0.55
    max_width: int = 84       # caption block max width as % of frame width (wrapping)
    vpos: float = 90.0        # precise vertical center: 0 = top .. 100 = bottom
    # Word emphasis style (ported from the reference project's caption renderer):
    #   none       — static caption
    #   karaoke    — color sweeps across words as they're spoken
    #   active     — the spoken word lights up in the accent color (Submagic look)
    #   wordbyword — words appear one by one as they're spoken
    #   focus      — one big word at a time, popping in with the voice
    highlight: str = "none"
    pop_in: bool = False      # each caption lands with a quick punch-in + fade
    # legacy fields kept for backward compatibility with older callers/presets
    bold: bool = True
    position: str = "bottom"  # superseded by vpos; ignored when vpos is set
    margin_v: int = 60


def _hex_to_ass(color: str, alpha: int = 0) -> str:
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _ass_time(t: float) -> str:
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    cs = int(round((t - int(t)) * 100))
    if cs == 100:
        cs = 0
        s += 1
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_ass(cues: list[dict], style: SubtitleStyle,
              play_w: int, play_h: int) -> str:
    # scale reference size (defined at 1080p) to the actual video height
    size = max(12, int(round(style.size * play_h / 1080)))
    outline = round(style.outline * play_h / 1080, 2)
    shadow = round(style.shadow * play_h / 1080, 2)

    # Caption block max width -> symmetric left/right margins (px in PlayRes).
    # This constrains where libass wraps lines.
    max_width = min(100, max(20, int(style.max_width)))
    margin_lr = int(round((100 - max_width) / 200 * play_w))

    # Precise vertical placement: anchor at the exact point via \pos with the
    # centre alignment (\an5), so the caption's middle sits at `vpos`% of height.
    vpos = min(100.0, max(0.0, float(style.vpos)))
    pos_x = play_w // 2
    pos_y = int(round(vpos / 100 * play_h))
    align = 5  # middle-centre; \pos below drives the actual location

    weight = min(900, max(100, int(style.weight)))

    if style.background in ("box", "blur"):
        border_style = 3  # opaque box using BackColour
        alpha = int(round((1 - style.box_opacity) * 255))
        back = _hex_to_ass("#000000", alpha)
    else:
        border_style = 1  # outline + drop shadow
        back = _hex_to_ass("#000000", 0)

    primary = _hex_to_ass(style.primary)
    # For karaoke: unsung=primary(white), sung=accent -> SecondaryColour holds base,
    # PrimaryColour holds the accent that sweeps in.
    if style.highlight == "karaoke":
        prim_field = _hex_to_ass(style.accent)
        sec_field = primary
    else:
        prim_field = primary
        sec_field = _hex_to_ass(style.accent)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{style.font},{size},{prim_field},{sec_field},{_hex_to_ass(style.outline_color)},{back},{weight},{-1 if style.italic else 0},0,0,100,100,0,0,{border_style},{outline},{shadow},{align},{margin_lr},{margin_lr},0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, Effect, Text
"""

    # Per-line override: pin the exact position and font weight. \b accepts a
    # numeric weight (100-900) in libass, giving finer thickness than plain bold.
    pin = f"{{\\an{align}\\pos({pos_x},{pos_y})\\b{weight}}}"

    accent_c = _hex_to_ass(style.accent)
    primary_c = _hex_to_ass(style.primary)

    def tok(w: dict) -> str:
        t = w["w"].upper() if style.uppercase else w["w"]
        return t.replace("{", "(").replace("}", ")")  # keep ASS override syntax safe

    # Punch-in: the caption lands at 92% and springs to 100% with a slight
    # overshoot — the reference renderer's signature page pop (\t times are ms).
    POP = r"\fscx92\fscy92\t(0,90,\fscx104\fscy104)\t(90,170,\fscx100\fscy100)"
    # Focus style: every word pops in solo at 130% base scale with overshoot.
    FOCUS_POP = r"\fscx85\fscy85\t(0,80,\fscx142\fscy142)\t(80,160,\fscx130\fscy130)"

    def emit(lines, t0, t1, overrides, body):
        if t1 <= t0:
            return
        lines.append(
            f"Dialogue: 0,{_ass_time(t0)},{_ass_time(t1)},Main,,0,0,0,"
            f"{{{overrides}}}{body}"
        )

    pin_raw = pin[1:-1]  # override block content without braces, for composing

    lines = [header]
    for ci, cue in enumerate(cues):
        text = cue["text"]
        if style.uppercase:
            text = text.upper()
        text = text.replace("\n", " ").strip().replace("{", "(").replace("}", ")")

        ws = cue.get("words") or []
        nxt_cue = cues[ci + 1] if ci + 1 < len(cues) else None
        # fade out only when the caption hides into a silence, not on hand-offs
        hides = not nxt_cue or (nxt_cue["start"] - cue["end"]) > 0.12
        fad_in = r"\fad(80,%d)" % (120 if hides else 0)
        base_fx = (fad_in + POP) if style.pop_in else ""

        # Word-driven styles need word timings; manually added cues without
        # them fall back to a static caption.
        mode = style.highlight if ws else "none"

        if mode == "karaoke" and ws:
            parts = []
            for i, w in enumerate(ws):
                nxt = ws[i + 1]["s"] if i + 1 < len(ws) else cue["end"]
                k_cs = max(1, int(round((nxt - w["s"]) * 100)))
                parts.append(f"{{\\kf{k_cs}}}{tok(w)} ")
            emit(lines, cue["start"], cue["end"], pin_raw + base_fx, "".join(parts).strip())

        elif mode == "active" and ws:
            # One event per word span: the spoken word carries the accent color
            # and max weight; everything else stays primary. No inline scaling —
            # \fscx on one word reflows the line, so emphasis is color + weight.
            for i, w in enumerate(ws):
                t0 = cue["start"] if i == 0 else w["s"]
                t1 = ws[i + 1]["s"] if i + 1 < len(ws) else cue["end"]
                parts = []
                for j, x in enumerate(ws):
                    if j == i:
                        parts.append(f"{{\\1c{accent_c}&\\b900}}{tok(x)}{{\\1c{primary_c}&\\b{weight}}}")
                    else:
                        parts.append(tok(x))
                fx = pin_raw
                if style.pop_in:
                    if i == 0:
                        fx += r"\fad(80,0)" + POP
                    if i == len(ws) - 1 and hides:
                        fx += r"\fad(0,120)"
                emit(lines, t0, t1, fx, " ".join(parts))

        elif mode == "wordbyword" and ws:
            # Cumulative reveal: all words occupy their final position from the
            # start (layout never shifts); upcoming ones are fully transparent.
            for i, w in enumerate(ws):
                t0 = cue["start"] if i == 0 else w["s"]
                t1 = ws[i + 1]["s"] if i + 1 < len(ws) else cue["end"]
                parts = []
                for j, x in enumerate(ws):
                    if j <= i:
                        parts.append(tok(x))
                    else:
                        parts.append(f"{{\\alpha&HFF&}}{tok(x)}{{\\alpha&H00&}}")
                fx = pin_raw
                if style.pop_in and i == len(ws) - 1 and hides:
                    fx += r"\fad(0,120)"
                emit(lines, t0, t1, fx, " ".join(parts))

        elif mode == "focus" and ws:
            # One big word at a time, springing in with the voice.
            for i, w in enumerate(ws):
                t0 = cue["start"] if i == 0 else w["s"]
                t1 = ws[i + 1]["s"] if i + 1 < len(ws) else cue["end"]
                emit(lines, t0, t1, pin_raw + FOCUS_POP, tok(w))

        else:
            emit(lines, cue["start"], cue["end"], pin_raw + base_fx, text)

    return "\n".join(lines) + "\n"


def to_srt(cues: list[dict]) -> str:
    def ts(t):
        h = int(t // 3600); m = int((t % 3600) // 60)
        s = int(t % 60); ms = int(round((t - int(t)) * 1000))
        if ms == 1000:
            ms = 0; s += 1
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{ts(c['start'])} --> {ts(c['end'])}\n{c['text']}\n")
    return "\n".join(out)


# ---------------------------------------------------------------- render

def video_dimensions(path: str) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0:s=x", path],
        capture_output=True, text=True,
    )
    try:
        w, h = out.stdout.strip().split("x")
        return int(w), int(h)
    except (ValueError, AttributeError):
        return 1920, 1080


def reframe_filter(src_w: int, src_h: int, ratio: str, fit: str = "crop",
                   pad_color: str = "#000000") -> tuple[str, int, int]:
    """Build an ffmpeg filter that reframes the video to a target aspect ratio.

    Returns (filter_string, out_w, out_h). `ratio` is "source" or "W:H"
    (e.g. "9:16"). `fit` is "crop" (fill the frame, cropping overflow) or
    "pad" (fit the whole frame, adding bars in `pad_color`). Output dimensions
    are forced even for H.264/yuv420p.
    """
    if not ratio or ratio == "source":
        return "", src_w, src_h
    try:
        rw, rh = (float(x) for x in ratio.replace("x", ":").split(":", 1))
        target_ar = rw / rh
    except (ValueError, ZeroDivisionError):
        return "", src_w, src_h
    if src_w <= 0 or src_h <= 0 or target_ar <= 0:
        return "", src_w, src_h
    src_ar = src_w / src_h

    def even(n: int) -> int:
        n = int(round(n))
        return n - (n % 2)

    if fit == "pad":
        if target_ar >= src_ar:            # wider target -> pillarbox (side bars)
            ow, oh = even(src_h * target_ar), even(src_h)
        else:                              # taller target -> letterbox (top/bottom)
            ow, oh = even(src_w), even(src_w / target_ar)
        color = "0x" + pad_color.lstrip("#")
        filt = f"pad={ow}:{oh}:(ow-iw)/2:(oh-ih)/2:{color}"
    else:                                  # crop (cover)
        if target_ar >= src_ar:            # wider target -> crop top/bottom
            ow, oh = even(src_w), even(src_w / target_ar)
        else:                              # taller target -> crop sides
            ow, oh = even(src_h * target_ar), even(src_h)
        filt = f"crop={ow}:{oh}"           # ffmpeg centres the crop by default
    return filt, ow, oh


def burn_subtitles(video: str, ass_path: str, out_path: str, crf: int = 20,
                   pre_filter: str = "", progress_cb=None) -> None:
    # libass reads the ASS via the subtitles filter. On Windows the path needs
    # escaping for the filtergraph (drive colon + backslashes). Any reframing
    # (crop/pad) must run BEFORE ass so subtitles land on the final frame.
    ass = "ass=" + _escape_filter_path(ass_path)
    filt = f"{pre_filter},{ass}" if pre_filter else ass
    cmd = [
        "ffmpeg", "-y", "-i", video, "-vf", filt,
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Burn-in failed: {r.stderr[-500:]}")


def mux_soft_subs(video: str, srt_path: str, out_path: str) -> None:
    cmd = ["ffmpeg", "-y", "-i", video, "-i", srt_path, "-c", "copy",
           "-c:s", "mov_text", "-metadata:s:s:0", "language=eng", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Muxing subtitles failed: {r.stderr[-400:]}")


def _escape_filter_path(path: str) -> str:
    p = str(Path(path).resolve())
    p = p.replace("\\", "/").replace(":", "\\:")
    return f"'{p}'"
