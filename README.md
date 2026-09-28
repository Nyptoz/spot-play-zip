# spotpl

**One Spotify playlist link in, one ZIP out.**

Paste a link, get a ZIP with every track. No account, no API keys, no config file.

---

## Setup (one time)

**Windows** - double-click **`start.bat`**
**macOS / Linux** - run **`./start.sh`** in a terminal

The script creates a small virtual environment, installs `spotdl`, and opens your browser.
That's it. Python 3.10+ is the only requirement (macOS: `brew install python`).

## Use

The browser page opens at `http://127.0.0.1:8710`:

1. Paste your Spotify playlist link.
2. Press **Get ZIP**.
3. Watch the live log, then click **Download**.

Every ZIP is also kept in the **`downloads/`** folder, so the page can re-download old ones.

Prefer the terminal? Drop the link straight on the command line:

```bash
python spotpl.py https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M
# -> downloads/todays-top-hits.zip
```

Options: `--format flac|opus|m4a|wav` (default `mp3`), `--port 8710`, `--no-browser`.

## What lands in the ZIP

```
todays-top-hits/
  01 - Artist - Track.mp3
  02 - Artist - Track.mp3
  _playlist.txt        <- name, source link, track count, date
```

Tracks are numbered in playlist order. Tracks that can't be matched (region-locked,
removed from the source) are listed in the on-screen log and simply skipped - the
rest still make it into the ZIP.

## How it works

`spotpl` is one Python file (`spotpl.py`, stdlib only) driving
[`spotdl`](https://github.com/spdlclr/spotdl), which resolves the Spotify tracklist
and fetches matching audio. spotpl adds the part spotdl doesn't do: run it in a temp
folder, package the result into a single ZIP, and hand you that ZIP.

```
spotpl.py        the whole app: CLI + local web UI
start.bat/.sh    one-time setup, then start
requirements.txt spotdl
downloads/       your ZIPs
```

## Notes

- Local only: the server binds to `127.0.0.1`, nothing is exposed to your network.
- Needs `ffmpeg`. If it isn't on your PATH, spotpl fetches it automatically on first run.
- A track that is unavailable on the matching source is skipped, not fatal.
- For personal listening. Please support the artists.

---

*Built for Nyptoz.*
