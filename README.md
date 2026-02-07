# scribe

Local transcription and speaker diarization with
[pyannote](https://github.com/pyannote/pyannote-audio) and
[parakeet-mlx](https://github.com/senstella/parakeet-mlx).

Takes an audio file, transcribes the speech, optionally identifies
who spoke when, and produces a speaker-attributed transcript.

## Requirements

- **macOS with Apple Silicon** (parakeet-mlx requires MLX)
- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (used to run parakeet-mlx)
- [ffmpeg](https://ffmpeg.org/) (all input is normalized to 16kHz mono WAV):
  `brew install ffmpeg`
- HuggingFace account with token

## Install

```sh
uv sync
```

## Setup

### 1. Accept gated model licenses

Visit each page and click "Agree and access repository":

- [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
- [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)

### 2. Create a HuggingFace token

https://huggingface.co/settings/tokens

### 3. Set the token

```sh
export HF_TOKEN=hf_xxxxx
```

The model downloads on first run and is cached locally.

## Usage

```sh
scribe meeting.wav
```

Writes `meeting.txt`:

```
SPEAKER_00: Thank you for joining. Let's start with the agenda.

SPEAKER_01: Sure, I wanted to discuss the roadmap first.
```

### Transcript only (no diarization)

Skip speaker diarization and just transcribe. No HuggingFace token or
pyannote install needed:

```sh
scribe meeting.wav --no-diarize
```

### Speaker count hints

Constraining the number of speakers speeds up clustering:

```sh
scribe meeting.wav --num-speakers 3
scribe meeting.wav --min-speakers 2 --max-speakers 6
```

### Options

```sh
scribe meeting.wav -o transcript.txt      # custom output path
scribe meeting.wav -o -                   # write to stdout
scribe meeting.wav --format json          # JSON with timestamps
scribe meeting.wav --format json -o -     # JSON to stdout
scribe meeting.wav --no-diarize           # transcript without speakers
scribe meeting.wav --num-speakers 2       # exact speaker count
scribe meeting.wav --min-speakers 2       # at least 2 speakers
scribe meeting.wav --max-speakers 4       # at most 4 speakers
```

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

## Known version constraints

pyannote.audio 4.0 pins torch==2.8.0 exactly. This is an upstream
restriction. The lock file encodes the working set of versions —
avoid running `uv lock --upgrade` without testing.

## Development

```sh
uv sync --dev
uv run pytest -q
uv run ruff check
```

## License

MIT
