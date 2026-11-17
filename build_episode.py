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
KEEP = int(os.environ.get("KEEP_EPISODES", "21"))
SHOW_TITLE = "The Morning Brief"
SHOW_DESC = "A private daily news briefing: sports, world, U.S., Kentucky and Louisville news, plus the Bengals, Bills, Bearcats, Cardinals and Wildcats."

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


async def synthesize(text: str, out: Path) -> None:
    await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(out))


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def main() -> None:
    script = newest_script(sys.argv[1] if len(sys.argv) > 1 else None)
    date = script.stem
    text = clean_for_speech(script.read_text())
    sources_file = script.with_name(f"{date}.sources.txt")
    sources = sources_file.read_text().strip() if sources_file.exists() else ""

    mp3 = ROOT / f"morning-brief-{date}.mp3"
    asyncio.run(synthesize(text, mp3))
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
    main()
