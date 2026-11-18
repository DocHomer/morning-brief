"""Turn the newest script in scripts/ into an MP3, publish it as a GitHub
release asset, and rebuild the podcast feed in docs/feed.xml.

Runs inside GitHub Actions (see .github/workflows/build-episode.yml).
"""
import asyncio
import datetime as dt
import email.utils
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

import edge_tts
from mutagen.mp3 import MP3

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
DOCS = ROOT / "docs"
EPISODES_JSON = DOCS / "episodes.json"

VOICE = os.environ.get("VOICE", "en-US-AndrewMultilingualNeural")
RATE = os.environ.get("RATE", "+0%")
PIPER_VOICE = os.environ.get("PIPER_VOICE", "en_US-ryan-high")
KEEP = int(os.environ.get("KEEP_EPISODES", "21"))
SHOW_TITLE = "The Morning Brief"
SHOW_DESC = "A private daily news briefing: sports, world, U.S., Kentucky and Louisville news, plus the Bengals, Bills, U.S. men's national soccer team, Bearcats, Cardinals and Wildcats."

REPO = os.environ["GITHUB_REPOSITORY"]  # owner/name
OWNER, NAME = REPO.split("/")
SITE = f"https://{OWNER.lower()}.github.io/{NAME}"


def newest_script(date_arg: str | None) -> Path:
    if date_arg:
        return SCRIPTS / f"{date_arg}.txt"
    files = sorted(p for p in SCRIPTS.glob("*.txt") if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.txt", p.name))
    if not files:
        sys.exit("No scripts found")
    return files[-1]


def clean_for_speech(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[#*_`>]+", "", text)  # stray markdown
    return re.sub(r"\n{3,}", "\n\n", text).strip()


PAUSE_STORY = float(os.environ.get("PAUSE_STORY", "2.0"))  # seconds between paragraphs/stories
PAUSE_SHORT = float(os.environ.get("PAUSE_SHORT", "1.0"))  # after short lead-ins like "Now, the Bengals."
SHORT_WORDS = 12
SAMPLE_RATE = 24000


def split_paragraphs(text: str) -> list[str]:
    """Each blank-line-separated paragraph is one story or lead-in."""
    return [" ".join(p.split()) for p in re.split(r"\n\s*\n", text) if p.strip()]


async def _edge(text: str, out: Path) -> None:
    await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(out))


async def _edge_all(chunks: list[str], outs: list[Path]) -> None:
    sem = asyncio.Semaphore(4)

    async def one(text: str, out: Path) -> None:
        async with sem:
            for attempt in range(3):
                try:
                    await _edge(text, out)
                    if out.exists() and out.stat().st_size > 1000:
                        return
                except Exception as exc:  # noqa: BLE001
                    print(f"Edge voice attempt {attempt + 1} failed: {exc!r}")
                await asyncio.sleep(2 * (attempt + 1))
            raise RuntimeError("Edge voice failed for a paragraph")

    await asyncio.gather(*(one(t, o) for t, o in zip(chunks, outs)))


def _piper_all(chunks: list[str], outs: list[Path]) -> None:
    import wave
    from piper import PiperVoice
    vdir = ROOT / ".voices"
    vdir.mkdir(exist_ok=True)
    model = vdir / f"{PIPER_VOICE}.onnx"
    if not model.exists():
        subprocess.run([sys.executable, "-m", "piper.download_voices", "--download-dir", str(vdir), PIPER_VOICE], check=True)
    voice = PiperVoice.load(str(model))
    for text, out in zip(chunks, outs):
        with wave.open(str(out), "wb") as w:
            voice.synthesize_wav(text, w)


def _to_wav(src: Path, dst: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1",
                    "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(dst)], check=True)


def _silence(seconds: float, dst: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"anullsrc=r={SAMPLE_RATE}:cl=mono", "-t", f"{seconds:.2f}",
                    "-c:a", "pcm_s16le", str(dst)], check=True)


def synthesize(text: str, out: Path) -> None:
    """Voice each paragraph separately, then join them with short pauses so
    stories don't run together. Free Microsoft Edge voice first; open-source
    Piper voice as a fallback."""
    chunks = split_paragraphs(text)
    work = out.parent / f".build-{out.stem}"
    if work.exists():
        for f in work.iterdir():
            f.unlink()
    work.mkdir(exist_ok=True)

    raw = [work / f"{i:03d}.mp3" for i in range(len(chunks))]
    try:
        asyncio.run(_edge_all(chunks, raw))
        print("Voice: Edge", VOICE)
    except Exception as exc:  # noqa: BLE001
        print(f"Edge voice failed ({exc!r}); falling back to Piper voice", PIPER_VOICE)
        raw = [work / f"{i:03d}.piper.wav" for i in range(len(chunks))]
        _piper_all(chunks, raw)

    pause_long, pause_short = work / "pause_long.wav", work / "pause_short.wav"
    _silence(PAUSE_STORY, pause_long)
    _silence(PAUSE_SHORT, pause_short)

    parts: list[Path] = []
    for i, (chunk, src) in enumerate(zip(chunks, raw)):
        wav = work / f"{i:03d}.wav"
        _to_wav(src, wav)
        parts.append(wav)
        if i < len(chunks) - 1:
            parts.append(pause_short if len(chunk.split()) <= SHORT_WORDS else pause_long)

    listing = work / "list.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-ac", "1", "-b:a", "64k", str(out)], check=True)
    for f in work.iterdir():
        f.unlink()
    work.rmdir()
    print(f"Joined {len(chunks)} paragraphs with {PAUSE_STORY}s pauses")


def gh(*args: str) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {r.stderr.strip()}")
    return r.stdout


def main() -> None:
    script = newest_script(sys.argv[1] if len(sys.argv) > 1 else None)
    date = script.stem
    text = clean_for_speech(script.read_text())
    sources_file = script.with_name(f"{date}.sources.txt")
    sources = sources_file.read_text().strip() if sources_file.exists() else ""

    mp3 = ROOT / f"morning-brief-{date}.mp3"
    synthesize(text, mp3)
    seconds = int(MP3(mp3).info.length)

    tag = f"ep-{date}"
    subprocess.run(["gh", "release", "delete", tag, "--cleanup-tag", "-y"], capture_output=True)
    gh("release", "create", tag, str(mp3), "--title", f"Morning Brief {date}",
       "--notes", sources or "Daily episode", "--latest=false")
    url = f"https://github.com/{REPO}/releases/download/{tag}/{mp3.name}"

    episodes = json.loads(EPISODES_JSON.read_text()) if EPISODES_JSON.exists() else []
    episodes = [e for e in episodes if e["date"] != date]
    pub = dt.datetime.now(dt.timezone.utc)
    episodes.append({
        "date": date, "url": url, "bytes": mp3.stat().st_size, "seconds": seconds,
        "pub": email.utils.format_datetime(pub), "sources": sources,
        "summary": text.split("\n\n")[0][:600],
    })
    episodes.sort(key=lambda e: e["date"], reverse=True)

    for old in episodes[KEEP:]:
        subprocess.run(["gh", "release", "delete", f"ep-{old['date']}", "--cleanup-tag", "-y"],
                       capture_output=True)
    episodes = episodes[:KEEP]

    DOCS.mkdir(exist_ok=True)
    EPISODES_JSON.write_text(json.dumps(episodes, indent=2))
    (DOCS / "feed.xml").write_text(render_feed(episodes))
    print(f"Published {date}: {seconds // 60}m{seconds % 60:02d}s -> {url}")


def render_feed(episodes: list[dict]) -> str:
    items = []
    for e in episodes:
        d = dt.date.fromisoformat(e["date"])
        title = f"{d:%A, %B} {d.day}, {d.year}"
        notes = e["summary"]
        if e["sources"]:
            notes += "\n\nSources:\n" + e["sources"]
        items.append(f"""    <item>
      <title>{escape(title)}</title>
      <guid isPermaLink="false">morning-brief-{e['date']}</guid>
      <pubDate>{e['pub']}</pubDate>
      <enclosure url="{escape(e['url'])}" length="{e['bytes']}" type="audio/mpeg"/>
      <itunes:duration>{e['seconds']}</itunes:duration>
      <description>{escape(notes)}</description>
    </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{SHOW_TITLE}</title>
    <link>{SITE}/</link>
    <description>{escape(SHOW_DESC)}</description>
    <language>en-us</language>
    <itunes:author>{SHOW_TITLE}</itunes:author>
    <itunes:image href="{SITE}/cover.png"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:block>Yes</itunes:block>
{chr(10).join(items)}
  </channel>
</rss>
"""


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        DOCS.mkdir(exist_ok=True)
        (DOCS / "last-error.txt").write_text(traceback.format_exc())
        raise
