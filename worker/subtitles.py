"""Burned-in subtitles for the dubbed narration: faster-whisper timing, script spelling, ASS + ffmpeg."""
import difflib
import json
import math
import re
import subprocess
import unicodedata
from pathlib import Path

MAX_CUE_WORDS = 4
MAX_CUE_CHARS = 28
MAX_CUE_SECONDS = 1.8
FONT = "DejaVu Sans"


def _norm(word):
    word = "".join(c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c))
    return re.sub(r"[^\w]", "", word.casefold())


def script_words(narration):
    return [w for w in str(narration or "").split() if _norm(w)]


def align_to_script(words, narration):
    """Keep whisper timing, but take the spelling of the script when the spoken words match it.

    `words` is a list of dicts {start, end, word}. Unmatched words keep the transcription,
    so improvised speech is still subtitled as heard.
    """
    script = script_words(narration)
    heard = [_norm(w["word"]) for w in words]
    wanted = [_norm(w) for w in script]
    result = [dict(w, word=w["word"].strip()) for w in words]
    matcher = difflib.SequenceMatcher(a=heard, b=wanted, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            for offset in range(i2 - i1):
                spoken, written = heard[i1 + offset], wanted[j1 + offset]
                if tag == "equal" or difflib.SequenceMatcher(a=spoken, b=written).ratio() >= 0.6:
                    result[i1 + offset]["word"] = script[j1 + offset]
    return [w for w in result if w["word"]]


def cues(words):
    """Short groups of words: phrases end at punctuation or pauses, then split evenly (no orphan words)."""
    words = [dict(w, word=w["word"].strip()) for w in words if w["word"].strip()]
    phrases, current = [], []
    for word in words:
        if current and (word["start"] - current[-1]["end"] > 0.6 or re.search(r"[.!?;:,]$", current[-1]["word"])):
            phrases.append(current)
            current = []
        current.append(word)
    if current:
        phrases.append(current)
    groups = []
    for phrase in phrases:
        chars = len(" ".join(w["word"] for w in phrase))
        seconds = phrase[-1]["end"] - phrase[0]["start"]
        parts = max(math.ceil(len(phrase) / MAX_CUE_WORDS), math.ceil(chars / MAX_CUE_CHARS),
                    math.ceil(seconds / MAX_CUE_SECONDS), 1)
        parts = min(parts, len(phrase))
        size = len(phrase) / parts
        groups.extend(phrase[round(i * size):round((i + 1) * size)] for i in range(parts))
    result = []
    for index, group in enumerate(groups):
        start = group[0]["start"]
        end = max(group[-1]["end"], start + 0.35)
        if index + 1 < len(groups):
            end = min(end, groups[index + 1][0]["start"])
        text = " ".join(w["word"] for w in group).rstrip(",;:")
        result.append(dict(start=round(start, 2), end=round(end, 2), text=text))
    return result


def _time(seconds):
    centis = int(round(seconds * 100))
    return f"{centis // 360000}:{centis // 6000 % 60:02d}:{centis // 100 % 60:02d}.{centis % 100:02d}"


BADGE_SECONDS = 3.5
ORANGE = "&H00226BFF"     # #FF6B22 in ASS (&HAABBGGRR)
DARK = "&H50101010"       # translucent near-black box


def _escape(text):
    return str(text).replace("\\", "").replace("{", "(").replace("}", ")")


def badge_styles(width, height):
    """Opaque-box styles of the purchase badge, above the spoken subtitle (which sits at 17% of the height)."""
    seal, button = round(height * 0.03), round(height * 0.027)
    return [f"Style: Selo,{FONT},{seal},&H00FFFFFF,&H00FFFFFF,{DARK},{DARK},-1,0,0,0,100,100,0,0,3,"
            f"{round(seal * 0.3)},0,2,{round(width * 0.06)},{round(width * 0.06)},{round(height * 0.35)},1",
            f"Style: Botao,{FONT},{button},&H00FFFFFF,&H00FFFFFF,{ORANGE},{ORANGE},-1,0,0,0,100,100,0,0,3,"
            f"{round(button * 0.4)},0,2,{round(width * 0.04)},{round(width * 0.04)},{round(height * 0.26)},1"]


def badge_events(duration, rating, sold):
    """Last 3.5 s: '★ 4,9 • 206 vendidos' capsule and a pulsing 'CONFIRA NO PRODUTO MARCADO' button."""
    start, end = max(0.0, duration - BADGE_SECONDS), max(duration, 0.1)
    pulse = "".join(f"\\t({i * 500},{i * 500 + 250},\\fscx106\\fscy106)\\t({i * 500 + 250},{i * 500 + 500},\\fscx100\\fscy100)"
                    for i in range(int(BADGE_SECONDS * 2)))
    return [f"Dialogue: 1,{_time(start)},{_time(end)},Selo,,0,0,0,,{{\\q2\\fad(250,0)}}★ {_escape(rating)} • {_escape(sold)}",
            f"Dialogue: 1,{_time(start)},{_time(end)},Botao,,0,0,0,,{{\\q2\\fad(250,0){pulse}}}CONFIRA NO PRODUTO MARCADO"]


# Chamadas do Estúdio (estilo "problema"): 3 textos grandes na faixa de cima, cada um com sua cor e janela de tempo
# (fração da duração). A 3ª fica até o fim, junto do selo de compra. Cores em ASS (&HAABBGGRR): texto, caixa.
CALLOUT_LOOKS = (("&H00FFFFFF", "&H003539E5", "\\frz3"),    # a dor: branco na caixa vermelha #E53935
                 ("&H00101010", "&H0000D4FF", "\\frz-3"),   # a virada: preto na caixa amarela #FFD400
                 ("&H00FFFFFF", ORANGE, "\\frz2"))          # não perca: branco no laranja, pulsando
CALLOUT_WINDOWS = ((0.05, 0.30), (0.33, 0.60), (0.63, 1.0))


def callout_styles(width, height):
    size = round(height * 0.046)
    return [f"Style: Chamada{i},{FONT},{size},{text},{text},{box},{box},-1,0,0,0,100,100,0,0,3,"
            f"{round(size * 0.32)},0,8,{round(width * 0.09)},{round(width * 0.09)},{round(height * 0.15)},1"
            for i, (text, box, _) in enumerate(CALLOUT_LOOKS, 1)]


def callout_events(callouts, duration):
    """Each callout pops in (35% → 115% → 100%), slightly tilted; the last one keeps pulsing until the end."""
    events = []
    for index, text in enumerate(list(callouts)[:len(CALLOUT_LOOKS)]):
        text = _escape(text).strip()
        if not text:
            continue
        first, last = CALLOUT_WINDOWS[index]
        start, end = duration * first, duration * last
        pop = "\\fscx35\\fscy35\\t(0,170,\\fscx115\\fscy115)\\t(170,300,\\fscx100\\fscy100)"
        pulse = ""
        if index == len(CALLOUT_LOOKS) - 1:
            pulse = "".join(f"\\t({300 + i * 600},{600 + i * 600},\\fscx108\\fscy108)"
                            f"\\t({600 + i * 600},{900 + i * 600},\\fscx100\\fscy100)"
                            for i in range(max(0, int((end - start - 0.3) / 0.6))))
        fade = "\\fad(0,0)" if index == len(CALLOUT_LOOKS) - 1 else "\\fad(0,120)"
        events.append(f"Dialogue: 2,{_time(start)},{_time(max(end, start + 0.3))},Chamada{index + 1},,0,0,0,,"
                      f"{{{fade}{CALLOUT_LOOKS[index][2]}{pop}{pulse}}}{text}")
    return events


def ass(entries, width, height, badge=None, callouts=None, duration=None):
    """`badge` = dict(duration, rating, sold) adds the purchase badge (Modern mode); `callouts` = up to 3 texts shown
    big on the top band (Estúdio, style "problema"), timed on `duration`."""
    size = round(height * 0.047)
    margin = round(height * 0.17)  # Above the marketplace's bottom overlay (product card, buttons).
    outline = max(2, round(size * 0.09))
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
             "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
             "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
             "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
             "MarginR, MarginV, Encoding",
             f"Style: Fala,{FONT},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,"
             f"{outline},1,2,{round(width * 0.08)},{round(width * 0.08)},{margin},1",
             *(badge_styles(width, height) if badge else []), *(callout_styles(width, height) if callouts else []), "",
             "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for entry in entries:
        text = entry["text"].replace("\\", "").replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{_time(entry['start'])},{_time(entry['end'])},Fala,,0,0,0,,{text}")
    if badge:
        lines += badge_events(badge["duration"], badge["rating"], badge["sold"])
    if callouts:
        lines += callout_events(callouts, duration or (badge or {}).get("duration") or 10.0)
    return "\n".join(lines) + "\n"


def probe(path):
    output = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                            capture_output=True, text=True, timeout=60, check=True).stdout
    data = json.loads(output)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if not video:
        raise ValueError("arquivo sem trilha de vídeo")
    return dict(width=int(video["width"]), height=int(video["height"]),
                duration=float(data["format"].get("duration") or video.get("duration") or 0),
                audio=any(s["codec_type"] == "audio" for s in data["streams"]))


def decode_audio(path):
    """16 kHz mono float32 via ffmpeg (PyAV 18 no longer accepts faster-whisper's open() arguments)."""
    import numpy
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True, timeout=120).stdout
    return numpy.frombuffer(raw, numpy.int16).astype(numpy.float32) / 32768.0


def transcribe(path, model_size="small", language="pt"):
    from faster_whisper import WhisperModel  # Imported lazily: only the VM worker has it.
    model = WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=4)
    try:
        segments, _ = model.transcribe(decode_audio(path), language=language, word_timestamps=True, vad_filter=True,
                                       beam_size=5, condition_on_previous_text=False)
        return [dict(start=w.start, end=w.end, word=w.word) for s in segments for w in (s.words or [])]
    finally:
        del model


def burn(source, entries, destination, workdir):
    info = probe(source)
    subtitle = Path(workdir) / "legenda.ass"
    subtitle.write_text(ass(entries, info["width"], info["height"]), encoding="utf-8")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(source), "-vf", f"ass={subtitle.name}",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-c:a", "copy", "-movflags", "+faststart", str(Path(destination).resolve())],
                   cwd=workdir, check=True, timeout=600, capture_output=True)
    return probe(destination)


def finish_command(source, destination, subtitle_name, width, height, audio):
    """Modern mode render: blurred fill + centred clip at width x height, 30 fps, subtitles and badge burned in,
    loudness-normalised AAC audio."""
    graph = (f"[0:v]split=2[bg][fg];[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
             f"crop={width}:{height},boxblur=20:2[blur];[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[clip];"
             f"[blur][clip]overlay=(W-w)/2:(H-h)/2,fps=30,setsar=1,ass={subtitle_name}[v]")
    command = ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-filter_complex", graph, "-map", "[v]",
               "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
    if audio:
        command += ["-map", "0:a:0", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
    return command + ["-movflags", "+faststart", str(Path(destination).resolve())]


def finish_video(source, narration, destination, workdir, finish, model_size="small", transcriber=transcribe):
    """Modern mode: subtitles + purchase badge + 1080x1920, even when no speech was detected.
    Estúdio "problema": `finish["subtitles"] = False` keeps the speech without subtitles (no transcription at all) and
    `finish["callouts"]` burns the 3 callouts."""
    info = probe(source)
    speech = finish.get("subtitles", True)
    words = transcriber(source, model_size) if info["audio"] and speech else []
    callouts = [str(c) for c in finish.get("callouts") or [] if str(c).strip()][:len(CALLOUT_LOOKS)]
    entries = cues(align_to_script(words, narration)) if words else []
    width, height = (finish.get("size") or [info["width"], info["height"]])[:2]
    seal = finish.get("badge") or {}
    badge = dict(duration=info["duration"], rating=seal["rating"], sold=seal["sold"]) if seal.get("rating") else None
    subtitle = Path(workdir) / "legenda.ass"
    subtitle.write_text(ass(entries, width, height, badge, callouts, info["duration"]), encoding="utf-8")
    subprocess.run(finish_command(source, destination, subtitle.name, width, height, info["audio"]),
                   cwd=workdir, check=True, timeout=600, capture_output=True)
    return dict(probe(destination), subtitles=len(entries), transcript=" ".join(e["text"] for e in entries),
                badge=bool(badge), callouts=len(callouts))


def extract_frames(source, folder, width=360):
    """Three JPEG frames (20%, 50%, 80% of the clip) for the fidelity check."""
    duration = probe(source)["duration"]
    frames = []
    for index, share in enumerate((0.2, 0.5, 0.8), 1):
        target = Path(folder) / f"quadro{index}.jpg"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{duration * share:.2f}", "-i", str(source),
                        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", str(target)],
                       check=True, timeout=60, capture_output=True)
        if target.exists() and target.stat().st_size:
            frames.append(target)
    return frames


def subtitle_video(source, narration, destination, workdir, model_size="small", transcriber=transcribe, finish=None):
    """Return metadata; without detected speech the video is copied unchanged and flagged.
    `finish` (Modern mode) also burns the purchase badge and scales to 1080x1920."""
    if finish:
        return finish_video(source, narration, destination, workdir, finish, model_size, transcriber)
    info = probe(source)
    words = transcriber(source, model_size) if info["audio"] else []
    entries = cues(align_to_script(words, narration)) if words else []
    if not entries:
        Path(destination).write_bytes(Path(source).read_bytes())
        return dict(info, subtitles=0, transcript="")
    result = burn(source, entries, destination, workdir)
    return dict(result, subtitles=len(entries), transcript=" ".join(e["text"] for e in entries))
