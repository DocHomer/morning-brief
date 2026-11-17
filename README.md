# The Morning Brief

A private daily news podcast.

- Each morning a Claude scheduled task follows `PROMPT.md`, researches the news, and pushes `scripts/YYYY-MM-DD.txt`.
- `.github/workflows/build-episode.yml` turns the script into an MP3 using a free Microsoft Edge neural voice, publishes it as a release, and updates `docs/feed.xml`.
- Subscribe in Overcast: **Add URL** → `https://<username>.github.io/morning-brief/feed.xml`

Change the voice or speed in the workflow with `VOICE` / `RATE` environment variables (for example `RATE: "+8%"`).
