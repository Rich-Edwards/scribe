"""Local transcription and speaker diarization with senko and parakeet."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple, Protocol

UNKNOWN_SPEAKER = "UNKNOWN"
_PARAKEET_VERSION = "parakeet-mlx>=0.4,<1"
_FUTURE_TIMEOUT_S = 600
_ERROR_FUTURE_TIMEOUT_S = 30


class ScribeError(Exception):
    """Raised when the transcription or diarization pipeline fails."""


class _Diarizer(Protocol):
    """Protocol for a senko-compatible diarizer."""

    def diarize(self, audio_path: str) -> dict | None: ...


class DiarizationSegment(NamedTuple):
    """A speaker-labeled time segment from diarization."""

    speaker: str
    start: float
    end: float


class TranscriptionSegment(NamedTuple):
    """A transcribed text segment with timestamps from parakeet."""

    text: str
    start: float
    end: float


class MergedSegment(NamedTuple):
    """A transcription segment attributed to a speaker."""

    speaker: str
    text: str
    start: float
    end: float


def _log(message: str) -> None:
    """Print a progress message to stderr."""
    print(message, file=sys.stderr)  # noqa: T201


def _find_ffmpeg() -> str:
    """Return the path to ffmpeg, or raise if not installed."""
    path = shutil.which("ffmpeg")
    if path is None:
        msg = "ffmpeg is required for audio normalization.\nInstall: brew install ffmpeg"
        raise ScribeError(msg)
    return path


def _find_uvx() -> str:
    """Return the path to uvx, or raise if not installed."""
    path = shutil.which("uvx")
    if path is None:
        msg = "uvx not found. Install uv: https://docs.astral.sh/uv/"
        raise ScribeError(msg)
    return path


@contextmanager
def _prepare_audio(audio_path: Path) -> Iterator[Path]:
    """Yield a normalized WAV (16kHz mono), converting via ffmpeg."""
    ffmpeg = _find_ffmpeg()
    fd, tmp_name = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    tmp_path = Path(tmp_name)

    try:
        _log(f"Preparing audio ({audio_path.suffix})...")
        subprocess.run(  # noqa: S603
            [
                ffmpeg,
                "-i",
                str(audio_path),
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(tmp_path),
                "-y",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        yield tmp_path
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else "unknown error"
        msg = f"ffmpeg conversion failed: {stderr}"
        raise ScribeError(msg) from exc
    finally:
        tmp_path.unlink(missing_ok=True)


def _create_diarizer() -> _Diarizer:
    """Create a senko Diarizer with automatic device selection."""
    try:
        import senko  # noqa: PLC0415
    except ImportError as exc:
        msg = "senko is not installed. Run: uv sync"
        raise ScribeError(msg) from exc

    try:
        return senko.Diarizer(device="auto")  # type: ignore[return-value] — no stubs
    except Exception as exc:
        msg = f"Failed to initialize diarizer: {exc}"
        raise ScribeError(msg) from exc


def run_diarization(
    diarizer: _Diarizer,
    audio_path: Path,
) -> list[DiarizationSegment]:
    """Run senko diarization on an audio file.

    Args:
        diarizer: A senko Diarizer instance.
        audio_path: Path to a 16kHz mono WAV file.
    """
    try:
        result = diarizer.diarize(str(audio_path))
        if result is None:
            return []
        return [
            DiarizationSegment(seg["speaker"], seg["start"], seg["end"])
            for seg in result["merged_segments"]
        ]
    except ScribeError:
        raise
    except Exception as exc:
        msg = f"Diarization failed on {audio_path}: {exc}"
        raise ScribeError(msg) from exc


def run_transcription(
    audio_path: Path,
) -> list[TranscriptionSegment]:
    """Run parakeet-mlx transcription on an audio file."""
    uvx = _find_uvx()
    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            subprocess.run(  # noqa: S603
                [
                    uvx,
                    _PARAKEET_VERSION,
                    str(audio_path),
                    "--output-format",
                    "json",
                    "--output-dir",
                    tmp_dir,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else "unknown error"
            msg = f"parakeet-mlx failed: {stderr}"
            raise ScribeError(msg) from exc

        json_files = list(Path(tmp_dir).glob("*.json"))
        if not json_files:
            msg = "parakeet-mlx produced no JSON output"
            raise ScribeError(msg)

        try:
            data = json.loads(json_files[0].read_text())
        except json.JSONDecodeError as exc:
            msg = f"parakeet-mlx returned invalid JSON: {exc}"
            raise ScribeError(msg) from exc

    segments = []
    for segment in data.get("segments", data.get("sentences", [])):
        text = segment.get("text", "").strip()
        start = segment.get("start")
        end = segment.get("end")
        if text and start is not None and end is not None:
            segments.append(TranscriptionSegment(text, float(start), float(end)))
    return segments


def merge(
    diarization: list[DiarizationSegment],
    transcription: list[TranscriptionSegment],
) -> list[MergedSegment]:
    """Assign speakers to transcription segments by time overlap."""
    diarization = sorted(diarization, key=lambda s: s.start)
    transcription = sorted(transcription, key=lambda s: s.start)
    if not diarization:
        return [MergedSegment(UNKNOWN_SPEAKER, s.text, s.start, s.end) for s in transcription]
    merged = []
    d_start = 0
    for seg in transcription:
        best_speaker = UNKNOWN_SPEAKER
        best_overlap = 0.0
        while d_start < len(diarization) and diarization[d_start].end <= seg.start:
            d_start += 1
        for i in range(d_start, len(diarization)):
            dseg = diarization[i]
            if dseg.start >= seg.end:
                break
            overlap = min(seg.end, dseg.end) - max(seg.start, dseg.start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = dseg.speaker
        merged.append(MergedSegment(best_speaker, seg.text, seg.start, seg.end))
    return merged


def format_text(segments: list[MergedSegment]) -> str:
    """Format as plain text, merging consecutive same-speaker segments."""
    if not segments:
        return ""

    lines: list[str] = []
    current_speaker: str | None = None
    current_texts: list[str] = []

    for seg in segments:
        if seg.speaker != current_speaker:
            if current_texts:
                lines.append(f"{current_speaker}: {' '.join(current_texts)}")
                lines.append("")
            current_speaker = seg.speaker
            current_texts = [seg.text]
        else:
            current_texts.append(seg.text)

    if current_texts:
        lines.append(f"{current_speaker}: {' '.join(current_texts)}")

    return "\n".join(lines).strip() + "\n"


def format_json(segments: list[MergedSegment]) -> str:
    """Format as JSON with timestamps and speaker attribution."""
    speakers = sorted({seg.speaker for seg in segments})
    data = {
        "speakers": speakers,
        "segments": [
            {
                "speaker": seg.speaker,
                "text": seg.text,
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
            }
            for seg in segments
        ],
    }
    return json.dumps(data, indent=2) + "\n"


def format_transcript_text(segments: list[TranscriptionSegment]) -> str:
    """Format transcription as plain text without speaker labels."""
    if not segments:
        return ""
    return " ".join(seg.text for seg in segments) + "\n"


def format_transcript_json(segments: list[TranscriptionSegment]) -> str:
    """Format transcription as JSON without speaker attribution."""
    data = {
        "segments": [
            {
                "text": seg.text,
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
            }
            for seg in segments
        ],
    }
    return json.dumps(data, indent=2) + "\n"


def _run_with_diarization(
    audio_path: Path,
    *,
    output_format: str,
) -> str:
    """Run full pipeline: transcription + diarization in parallel.

    Launches transcription in a background thread while the senko
    diarizer loads and runs, so their wall-clock times overlap.
    """
    with ThreadPoolExecutor(max_workers=1) as executor:
        _log("Starting transcription (background)...")
        transcription_future = executor.submit(run_transcription, audio_path)

        try:
            _log("Loading diarization model...")
            diarizer = _create_diarizer()

            _log(f"Running diarization on {audio_path}...")
            diarization = run_diarization(diarizer, audio_path)
        except Exception:
            try:
                transcription_future.result(timeout=_ERROR_FUTURE_TIMEOUT_S)
            except Exception as tx_exc:  # noqa: BLE001
                _log(f"Transcription also failed: {tx_exc}")
            raise

        speaker_count = len({s.speaker for s in diarization})
        _log(f"  Found {speaker_count} speakers")

        if not diarization:
            _log("Warning: no speech segments detected")

        transcription = transcription_future.result(timeout=_FUTURE_TIMEOUT_S)
        _log(f"  Transcribed {len(transcription)} segments")

    _log("Merging results...")
    merged = merge(diarization, transcription)

    unknown_count = sum(1 for s in merged if s.speaker == UNKNOWN_SPEAKER)
    if unknown_count:
        _log(f"Warning: {unknown_count} segments could not be attributed to a speaker")

    return format_json(merged) if output_format == "json" else format_text(merged)


def _run_transcript_only(audio_path: Path, *, output_format: str) -> str:
    """Run transcription without diarization."""
    _log("Running transcription...")
    transcription = run_transcription(audio_path)
    _log(f"  Transcribed {len(transcription)} segments")
    return (
        format_transcript_json(transcription)
        if output_format == "json"
        else format_transcript_text(transcription)
    )


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Local transcription and speaker diarization with senko + parakeet",
    )
    parser.add_argument("audio", type=Path, help="Path to audio file")
    parser.add_argument(
        "-o",
        "--output",
        help="Output file path, or - for stdout (default: input file with .txt/.json extension)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        dest="output_format",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--no-diarize",
        action="store_true",
        help="Transcribe only, without speaker diarization",
    )
    args = parser.parse_args()

    if not args.audio.exists():
        sys.exit(f"Error: audio file not found: {args.audio}")

    try:
        with _prepare_audio(args.audio) as wav_path:
            if args.no_diarize:
                output = _run_transcript_only(
                    wav_path,
                    output_format=args.output_format,
                )
            else:
                output = _run_with_diarization(
                    wav_path,
                    output_format=args.output_format,
                )

        if args.output == "-":
            sys.stdout.write(output)
        else:
            ext = ".json" if args.output_format == "json" else ".txt"
            output_path = Path(args.output) if args.output else args.audio.with_suffix(ext)
            output_path.write_text(output)
            _log(f"Wrote transcript to {output_path}")

    except ScribeError as exc:
        sys.exit(f"Error: {exc}")


if __name__ == "__main__":
    main()
