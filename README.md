# Lecture parser

Export UT Austin Lectures Online recordings as timestamped transcripts, complete MP4 files, or context for an AI assistant. Optionally generate a study summary with a local Ollama model. Media and caption URLs are discovered from the player, without hardcoded lecture IDs.

Supports **macOS, Windows, and Linux** with Python 3.10+ and a desktop Chromium login flow. macOS can also reuse an existing Arc or Google Chrome tab.

## Install on another machine

Clone this private repository using a GitHub account with access:

```sh
git clone https://github.com/KingPinged/lecture-parser.git
cd lecture-parser
```

Create and activate a virtual environment:

**macOS / Linux**

```sh
python3 -m venv .venv
source .venv/bin/activate
```

**Windows PowerShell**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Then install the tool and its browser on any platform:

```sh
python -m pip install ".[browser]"
python -m playwright install chromium
lectureparse --help
```

On Linux, use `python -m playwright install --with-deps chromium` if browser system libraries are missing; installing those libraries may require administrator access. See Playwright's [browser installation instructions](https://playwright.dev/python/docs/browsers) and [supported systems](https://playwright.dev/python/docs/intro#system-requirements).

If PowerShell blocks activation, use `.\.venv\Scripts\python.exe` in place of `python` and `.\.venv\Scripts\lectureparse.exe` in place of `lectureparse`. No shell policy change is needed. `python -m lectureparse` also works after installation.

For MP4 export, install [FFmpeg](https://ffmpeg.org/download.html) and make sure both `ffmpeg` and `ffprobe` are on PATH. On a Mac with Homebrew: `brew install ffmpeg`. Caption and context exports do not require FFmpeg.

## Sign in and export

```sh
lectureparse login
```

In the Chromium window, sign in to Canvas, open your course's **Lectures Online**, and select a recording. Leave that page open, return to the terminal, and press Enter. The tool saves the session and last selected lecture locally under `~/.lectureparse/`, then closes its browser window.

Each machine and user signs in separately. The saved session contains authentication cookies; it stays outside the checkout. Do not share that folder. `--profile PATH` selects another local session folder; use the same option for login and export. Run one command per profile at a time. If Canvas expires the session, run `lectureparse login` again.

```sh
# Export the last lecture selected during login.
lectureparse export --format transcript

# Export context.md and timestamped text for Claude or another AI assistant.
lectureparse export --format context --output exports/my-lecture

# Export transcripts, context, and the full MP4 recording.
lectureparse export --format all

# List available lectures for the current course.
lectureparse list

# Select a different lecture by its actual episode URL.
lectureparse export --url "PASTE_EPISODE_URL_HERE" --format context

# Inspect dynamically discovered media and caption sources.
lectureparse inspect
```

Default output: `exports/<episode-id>/`. An existing nonempty output directory is protected; choose a new folder or use `--overwrite` to replace output files. Add `--headless` to hide Chromium after completing login. Headed login needs a desktop session; later exports can run headlessly.

`--quality lowest` selects the smallest HLS rendition. `--source-index` chooses another discovered feed, and `--caption-index` chooses another caption track. `--snapshot discovery.json` reuses a previous discovery without browser automation while its media URLs remain valid; authenticated browser fallback is unavailable in snapshot mode.

### Reuse an existing browser on macOS

Open a recording through Canvas in Arc or Google Chrome and enable **Allow JavaScript from Apple Events** in that browser's developer menu. These modes use the existing signed-in tab and do not require `lectureparse login` or Playwright:

```sh
lectureparse export --browser Arc --format context
lectureparse export --browser "Google Chrome" --format all
```

For this macOS-only route, `python -m pip install .` is sufficient. Direct source usage (`python3 lecture_parser.py ...`) also remains available.

## AI summaries and missing captions

`context.md` contains the lecture's speech in timestamped sections; upload it to your AI assistant. It is not itself an AI-written summary.

For a generated `summary.md`, install and start [Ollama](https://ollama.com), install a model suitable for your machine, and replace `YOUR_INSTALLED_MODEL` below with its name:

```sh
lectureparse export --format context --ollama-model YOUR_INSTALLED_MODEL
lectureparse summarize exports/my-lecture --ollama-model YOUR_INSTALLED_MODEL
```

Summaries are generated locally by default. `--ollama-url` can select a different Ollama server; that server will receive the transcript.

When a recording has no usable captions, install the optional speech dependency and use local transcription:

```sh
python -m pip install ".[speech]"
lectureparse export --format context --transcribe-missing --whisper-model base
```

Whisper requires FFmpeg and may download model weights on first use. Speech recognition and summaries may mishear technical terms. Text outputs cover speech; they do not OCR slides or reconstruct code shown only on screen.

## Exported files

| File | Purpose |
|---|---|
| `lecture.mp4` | Complete recording, remuxed without re-encoding and checked for duration, video, and audio |
| `captions.vtt` / `captions.srt` | Original caption track |
| `transcript.txt`, `.srt`, `.json` | Timestamped speech as text, subtitles, and structured segments |
| `context.md` | Full transcript grouped into timestamped sections |
| `chunks.jsonl` | Bounded timestamped chunks for retrieval or model processing |
| `outline.md` | Extracted keywords and caption excerpts |
| `summary.md`, `summary-parts.md` | AI-written study guide and intermediate notes, when requested |
| `metadata.json` | Source, duration, counts, completion status, and MP4 validation |
| `discovery.json` | Discovered media and caption URLs |

Exports, recordings, transcripts, local sessions, and environment files are excluded from Git. This repository contains the tool, documentation, and synthetic tests.

The exporter supports completed HLS recordings, alternate audio renditions, and direct MP4 sources. It rejects live/incomplete playlists. Segmented HLS subtitle playlists are not parsed; use a discovered VTT/SRT track or Whisper. Servers requiring authentication on every media segment may reject FFmpeg. Reopen the recording for fresh links if an export fails. Caption and manifest requests can fall back to the authenticated browser.

## Update and verify

After pulling a new version, reinstall it in the activated environment:

```sh
git pull --ff-only
python -m pip install --upgrade ".[browser]"
python -m playwright install chromium
python -m unittest discover -s tests -v
```

Browser integration tests use synthetic pages and a temporary session; they never log into Canvas. Enable them with `LECTUREPARSE_BROWSER_TESTS=1` (PowerShell: `$env:LECTUREPARSE_BROWSER_TESTS = "1"`) before running the tests. GitHub Actions runs them on macOS, Windows, and Linux with Python 3.10 and 3.13. The MP4 test additionally runs when FFmpeg is installed.

Implementation references: [FFmpeg stream copy](https://ffmpeg.org/ffmpeg.html#Streamcopy), [WebVTT](https://developer.mozilla.org/en-US/docs/Web/API/WebVTT_API), [Playwright browser authentication](https://playwright.dev/python/docs/auth), [Ollama generate API](https://docs.ollama.com/api/generate).
