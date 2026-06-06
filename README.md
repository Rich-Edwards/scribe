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
