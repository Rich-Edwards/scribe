"""Local transcription and speaker diarization with senko and parakeet."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, Protocol

UNKNOWN_SPEAKER = "UNKNOWN"
_PARAKEET_VERSION = "parakeet-mlx>=0.4,<1"
_FUTURE_TIMEOUT_S = 600
_ERROR_FUTURE_TIMEOUT_S = 30
_DEFAULT_SNIPPET_SECONDS = 8.0


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


class SpeakerSample(NamedTuple):
    """A representative audio sample for naming a diarized speaker."""

    speaker: str
    start: float
    end: float


class MeetingMetadata(NamedTuple):
    """Optional metadata to add to transcript output."""

    title: str | None
    meeting_date: str | None


class SpeakerLabelingOptions(NamedTuple):
    """Options for interactive speaker labeling."""

    enabled: bool
    snippet_seconds: float


def _log(message: str) -> None:
    """Print a progress message to stderr."""
    print(message, file=sys.stderr)  # noqa: T201


def _today_iso() -> str:
    """Return today's local date in ISO format."""
    return datetime.now(tz=UTC).astimezone().date().isoformat()


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


def _meeting_heading(title: str | None, meeting_date: str | None) -> str | None:
    """Return a Markdown heading for a titled meeting transcript."""
    if title is None:
        return None
    normalized_title = title.strip()
    if not normalized_title:
        return None
    display_date = meeting_date or _today_iso()
    return f"# {normalized_title} - {display_date}"


def _with_meeting_heading(
    body: str,
    *,
    title: str | None,
    meeting_date: str | None,
) -> str:
    """Prefix text output with the meeting heading when provided."""
    heading = _meeting_heading(title, meeting_date)
    if heading is None:
        return body
    return f"{heading}\n\n{body}".rstrip() + "\n"


def _title_slug(title: str) -> str:
    """Return a filesystem-friendly title slug while preserving title case."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title.strip()).strip("-")
    return slug or "Meeting"


def default_output_path(
    audio_path: Path,
    *,
    output_format: str,
    title: str | None,
    meeting_date: str | None,
) -> Path:
    """Return the default output path for the requested transcript."""
    if title and title.strip():
        display_date = meeting_date or _today_iso()
        ext = ".json" if output_format == "json" else ".md"
        return audio_path.with_name(f"{_title_slug(title)}_{display_date}_Transcript{ext}")
    ext = ".json" if output_format == "json" else ".txt"
    return audio_path.with_suffix(ext)


def rename_speakers(
    segments: list[MergedSegment],
    speaker_labels: dict[str, str],
) -> list[MergedSegment]:
    """Apply user-provided speaker names to merged transcript segments."""
    return [
        MergedSegment(
            speaker_labels.get(seg.speaker, seg.speaker),
            seg.text,
            seg.start,
            seg.end,
        )
        for seg in segments
    ]


def _select_speaker_samples(
    diarization: list[DiarizationSegment],
    *,
    snippet_seconds: float,
) -> list[SpeakerSample]:
    """Select the longest available segment per speaker for labeling."""
    best_by_speaker: dict[str, DiarizationSegment] = {}
    for segment in diarization:
        if segment.speaker == UNKNOWN_SPEAKER:
            continue
        current = best_by_speaker.get(segment.speaker)
        if current is None or segment.end - segment.start > current.end - current.start:
            best_by_speaker[segment.speaker] = segment

    samples = []
    for speaker, segment in sorted(best_by_speaker.items()):
        duration = max(0.0, min(snippet_seconds, segment.end - segment.start))
        if duration > 0:
            samples.append(SpeakerSample(speaker, segment.start, segment.start + duration))
    return samples


def _play_audio_sample(audio_path: Path, sample: SpeakerSample) -> None:
    """Extract and play a short speaker sample with ffmpeg and afplay."""
    ffmpeg = _find_ffmpeg()
    player = shutil.which("afplay")
    if player is None:
        msg = "afplay not found; cannot play speaker samples on this system"
        raise ScribeError(msg)

    fd, tmp_name = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    tmp_path = Path(tmp_name)
    duration = sample.end - sample.start

    try:
        subprocess.run(  # noqa: S603
            [
                ffmpeg,
                "-ss",
                f"{sample.start:.3f}",
                "-t",
                f"{duration:.3f}",
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
        subprocess.run([player, str(tmp_path)], check=True)  # noqa: S603
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else str(exc)
        msg = f"speaker sample playback failed: {stderr}"
        raise ScribeError(msg) from exc
    finally:
        tmp_path.unlink(missing_ok=True)


def prompt_speaker_labels(
    audio_path: Path,
    diarization: list[DiarizationSegment],
    *,
    snippet_seconds: float,
) -> dict[str, str]:
    """Play one sample per speaker and prompt for replacement names."""
    labels: dict[str, str] = {}
    samples = _select_speaker_samples(diarization, snippet_seconds=snippet_seconds)
    if not samples:
        return labels

    _log("")
    _log("Speaker labeling")
    _log("Leave a name blank to keep the generated speaker label.")

    try:
        input("Press Enter when you are ready to identify speakers.")
    except EOFError:
        _log("No interactive input available; keeping generated speaker labels.")
        return labels

    for sample in samples:
        _log(f"Playing {sample.speaker} sample ({sample.start:.1f}s-{sample.end:.1f}s)...")
        try:
            _play_audio_sample(audio_path, sample)
        except ScribeError as exc:
            _log(f"Warning: {exc}")

        try:
            label = input(f"Name for {sample.speaker}: ").strip()
        except EOFError:
            _log("No interactive input available; keeping generated speaker labels.")
            return labels

        if label:
            labels[sample.speaker] = label

    return labels


def format_text(
    segments: list[MergedSegment],
    *,
    title: str | None = None,
    meeting_date: str | None = None,
) -> str:
    """Format as plain text, merging consecutive same-speaker segments."""
    if not segments:
        return _with_meeting_heading("", title=title, meeting_date=meeting_date) if title else ""

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

    body = "\n".join(lines).strip() + "\n"
    return _with_meeting_heading(body, title=title, meeting_date=meeting_date)


def format_json(
    segments: list[MergedSegment],
    *,
    title: str | None = None,
    meeting_date: str | None = None,
) -> str:
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
    if title and title.strip():
        data["title"] = title.strip()
        data["date"] = meeting_date or _today_iso()
    return json.dumps(data, indent=2) + "\n"


def format_transcript_text(
    segments: list[TranscriptionSegment],
    *,
    title: str | None = None,
    meeting_date: str | None = None,
) -> str:
    """Format transcription as plain text without speaker labels."""
    if not segments:
        return _with_meeting_heading("", title=title, meeting_date=meeting_date) if title else ""
    body = " ".join(seg.text for seg in segments) + "\n"
    return _with_meeting_heading(body, title=title, meeting_date=meeting_date)


def format_transcript_json(
    segments: list[TranscriptionSegment],
    *,
    title: str | None = None,
    meeting_date: str | None = None,
) -> str:
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
    if title and title.strip():
        data["title"] = title.strip()
        data["date"] = meeting_date or _today_iso()
    return json.dumps(data, indent=2) + "\n"


def _run_with_diarization(
    audio_path: Path,
    *,
    output_format: str,
    metadata: MeetingMetadata,
    speaker_labeling: SpeakerLabelingOptions,
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

    if speaker_labeling.enabled:
        labels = prompt_speaker_labels(
            audio_path,
            diarization,
            snippet_seconds=speaker_labeling.snippet_seconds,
        )
        merged = rename_speakers(merged, labels)

    return (
        format_json(merged, title=metadata.title, meeting_date=metadata.meeting_date)
        if output_format == "json"
        else format_text(merged, title=metadata.title, meeting_date=metadata.meeting_date)
    )


def _run_transcript_only(
    audio_path: Path,
    *,
    output_format: str,
    metadata: MeetingMetadata,
) -> str:
    """Run transcription without diarization."""
    _log("Running transcription...")
    transcription = run_transcription(audio_path)
    _log(f"  Transcribed {len(transcription)} segments")
    return (
        format_transcript_json(
            transcription,
            title=metadata.title,
            meeting_date=metadata.meeting_date,
        )
        if output_format == "json"
        else format_transcript_text(
            transcription,
            title=metadata.title,
            meeting_date=metadata.meeting_date,
        )
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
    parser.add_argument(
        "--label-speakers",
        action="store_true",
        help="After diarization, play one snippet per speaker and prompt for names",
    )
    parser.add_argument(
        "--snippet-seconds",
        type=float,
        default=_DEFAULT_SNIPPET_SECONDS,
        help="Seconds to play for each speaker-labeling sample (default: 8)",
    )
    parser.add_argument(
        "--title",
        help="Meeting title for the transcript heading and default output filename",
    )
    parser.add_argument(
        "--date",
        dest="meeting_date",
        default=_today_iso(),
        help="Meeting date to append to titled output files (default: today)",
    )
    args = parser.parse_args()

    if not args.audio.exists():
        sys.exit(f"Error: audio file not found: {args.audio}")

    if args.snippet_seconds <= 0:
        sys.exit("Error: --snippet-seconds must be greater than 0")

    metadata = MeetingMetadata(args.title, args.meeting_date)
    speaker_labeling = SpeakerLabelingOptions(args.label_speakers, args.snippet_seconds)

    try:
        with _prepare_audio(args.audio) as wav_path:
            if args.no_diarize:
                output = _run_transcript_only(
                    wav_path,
                    output_format=args.output_format,
                    metadata=metadata,
                )
            else:
                output = _run_with_diarization(
                    wav_path,
                    output_format=args.output_format,
                    metadata=metadata,
                    speaker_labeling=speaker_labeling,
                )

        if args.output == "-":
            sys.stdout.write(output)
        else:
            output_path = (
                Path(args.output)
                if args.output
                else default_output_path(
                    args.audio,
                    output_format=args.output_format,
                    title=args.title,
                    meeting_date=args.meeting_date,
                )
            )
            output_path.write_text(output)
            _log(f"Wrote transcript to {output_path}")

    except ScribeError as exc:
        sys.exit(f"Error: {exc}")


if __name__ == "__main__":
    main()
