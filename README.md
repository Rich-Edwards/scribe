# scribe

Local transcription and speaker diarization with
[senko](https://github.com/narcotic-sh/senko) and
[parakeet-mlx](https://github.com/senstella/parakeet-mlx).

Takes an audio or video file, transcribes the speech, optionally
identifies who spoke when, and produces a speaker-attributed
transcript. Any format ffmpeg can read (mp4, mp3, m4a, webm, etc.)
is automatically converted to WAV for processing.

Diarization uses CoreML for hardware-accelerated inference on Apple
Silicon. Models download automatically on first run (no account needed).

## Requirements

- **macOS with Apple Silicon** (parakeet-mlx requires MLX)
- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (used to run parakeet-mlx)
- [ffmpeg](https://ffmpeg.org/) (all input is normalized to 16kHz mono WAV):
  `brew install ffmpeg`
- `OPENAI_API_KEY` for API-backed meeting summaries

## Install

```sh
uv sync
```

## Usage

```sh
scribe meeting.wav
scribe recording.mp4
scribe podcast.mp3
```

Writes `meeting.txt` / `recording.txt` / `podcast.txt`:

```
SPEAKER_00: Thank you for joining. Let's start with the agenda.

SPEAKER_01: Sure, I wanted to discuss the roadmap first.
```

### Transcript only (no diarization)

```sh
scribe meeting.wav --no-diarize
```

### Options

```sh
scribe meeting.wav -o transcript.txt      # custom output path
scribe meeting.wav -o -                   # write to stdout
scribe meeting.wav --format json          # JSON with timestamps
scribe meeting.wav --format json -o -     # JSON to stdout
scribe meeting.wav --no-diarize           # transcript without speakers
scribe meeting.wav --label-speakers       # play samples and rename speakers
```

### Meeting titles

Add a meeting title to put a Markdown heading on the first line and use a
dated transcript filename:

```sh
scribe recording.m4a --title "Sales and CS L10" --date 2026-06-06
```

Writes `Sales-and-CS-L10_2026-06-06_Transcript.md`:

```md
# Sales and CS L10 - 2026-06-06

Rich: Let's start with the scorecard.
```

If `--date` is omitted, today's date is used.

### Speaker labeling

After diarization, Scribe can play one short sample for each detected speaker
and prompt for the speaker's name:

```sh
scribe recording.m4a --label-speakers --title "Sales and CS L10"
```

Leave a name blank to keep the generated label such as `SPEAKER_00`. Use
`--snippet-seconds 5` to change the sample length.

### Meeting summaries

Scribe can generate one Markdown summary from a speaker-labeled meeting
transcript using the OpenAI Responses API. The transcript filename must end in
`_Transcript.md`; the companion summary is written as `_Summary.md`.

```sh
export OPENAI_API_KEY="..."
# Optional; defaults to the model configured in scribe.py.
export SCRIBE_OPENAI_MODEL="gpt-5.5"

scribe Sales-and-CS-L10_2026-06-06_Transcript.md \
  --generate-summary \
  --title "Sales and CS L10" \
  --date 2026-06-06 \
  --meeting-type L10
```

The macOS launcher also loads `/Users/edwards/Documents/Git/scribe/.env`
automatically when present. Keep API keys there for local use; `.env` is
ignored by git.

Supported meeting types are `L10`, `Customer`, and `Other`. Summary generation
uses canonical examples from the fixed Armen OS meetings folder:

```text
/Users/edwards/Documents/Git/Armen OS/07-meetings/Meeting Transcripts and Summaries
```

`L10` is the primary workflow. When `--meeting-type L10` is selected, Scribe
prioritizes the canonical L10 summary and asks the API for the established
EOS/L10 structure: front matter, executive summary, key issues/discussion,
decisions, owner-grouped next steps, cascading messages, meeting rating, and
bottom line when those details are present in the transcript. Output uses
simple WYSIWYG-safe Markdown, avoiding tables so it can be pasted into the web
rich-text notes editor.

After review, file the transcript and summary into the matching meeting-type
subfolder and move source-side leftovers to macOS Trash:

```sh
scribe recording.m4a \
  --finalize-meeting \
  --meeting-type L10 \
  --transcript Sales-and-CS-L10_2026-06-06_Transcript.md \
  --summary Sales-and-CS-L10_2026-06-06_Summary.md \
  --log-file recording.scribe.log
```

Finalization fails if the fixed meetings folder, selected meeting-type
subfolder, source recording, transcript, or summary is missing. It also refuses
to overwrite existing destination files. Files are copied into temporary
destination files before the final rename; cleanup to Trash happens only after
both durable artifacts are filed.

### macOS launcher

`scribe_picker.zsh` runs the end-to-end meeting workflow:

1. Choose the source recording.
2. Enter meeting title, date, and type.
3. Transcribe with interactive speaker labeling.
4. Generate the summary and open it in the default Markdown app for review.
5. Choose `Retry Summary`, `Finalize`, or `Cancel`.
6. Confirm Strety handoff is complete or intentionally skipped before filing.

If review is cancelled, the source recording, transcript, summary, and log are
left in place. On successful finalization, only the filed transcript and filed
summary remain outside Trash.

### JSON output

```sh
scribe meeting.wav --format json
```

Writes `meeting.json` with timestamps and speaker attribution:

```json
{
  "speakers": ["SPEAKER_00", "SPEAKER_01"],
  "segments": [
    {
      "speaker": "SPEAKER_00",
      "text": "Thank you for joining.",
      "start": 0.531,
      "end": 2.874
    }
  ]
}
```

## Development

```sh
uv sync --dev
uv run pytest -q
uv run ruff check
```

## License

MIT
