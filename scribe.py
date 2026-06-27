"""Local transcription and speaker diarization with senko and parakeet."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
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
_DEFAULT_OPENAI_MODEL = "gpt-5.5"
_DEFAULT_MAX_SUMMARY_INPUT_CHARS = 600_000
MEETINGS_ROOT = Path(
    "/Users/edwards/Documents/Git/Armen OS/07-meetings/Meeting Transcripts and Summaries",
)
MEETING_TYPES = ("L10", "Customer", "Other")
CANONICAL_SUMMARY_EXAMPLES = {
    "L10": Path("Sales-and-CS-L10_2026-06-08_Summary.md"),
    "Customer": Path("Numa-Armen Stone Monthly Review - 6.5.26.txt"),
    "Other": Path("1x1-with-Caroline-Massey_2026-06-17_Summary_and_Transcript.md"),
}


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


class MeetingSummaryMetadata(NamedTuple):
    """Metadata used to generate a meeting summary."""

    title: str
    meeting_date: str
    meeting_type: str


class FinalizedArtifacts(NamedTuple):
    """Final transcript and summary locations after filing."""

    transcript: Path
    summary: Path


TrashFile = Callable[[Path], None]


class SummaryRequester(Protocol):
    """Callable that turns a prompt into summary Markdown."""

    def __call__(self, *, prompt: str, api_key: str, model: str) -> str:
        """Return summary Markdown for the supplied prompt."""
        ...


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


def default_summary_path(transcript_path: Path) -> Path:
    """Return the default companion summary path for a transcript."""
    if not transcript_path.name.endswith("_Transcript.md"):
        msg = "summary generation requires a transcript filename ending in _Transcript.md"
        raise ScribeError(msg)
    return transcript_path.with_name(
        f"{transcript_path.name.removesuffix('_Transcript.md')}_Summary.md",
    )


def validate_summary_text(summary: str) -> str:
    """Validate generated Markdown summary text and return normalized content."""
    normalized = summary.strip()
    if not normalized:
        msg = "API returned an empty summary"
        raise ScribeError(msg)
    if normalized.startswith("```") and normalized.endswith("```"):
        msg = "API returned summary wrapped in a code fence"
        raise ScribeError(msg)
    body = normalized
    if normalized.startswith("---\n"):
        frontmatter_end = normalized.find("\n---", 4)
        if frontmatter_end == -1:
            msg = "API summary front matter is missing a closing delimiter"
            raise ScribeError(msg)
        body = normalized[frontmatter_end + 4 :].lstrip()
    if not body.startswith("# "):
        msg = "API summary must start with a Markdown heading"
        raise ScribeError(msg)
    return f"{normalized}\n"


def _canonical_example_paths(meetings_root: Path) -> dict[str, Path]:
    """Return absolute canonical example paths under the meetings root."""
    return {
        meeting_type: meetings_root / meeting_type / relative_path
        for meeting_type, relative_path in CANONICAL_SUMMARY_EXAMPLES.items()
    }


def _read_canonical_examples(meetings_root: Path) -> dict[str, str]:
    """Read canonical summary examples used to ground summary generation."""
    examples = {}
    for meeting_type, path in _canonical_example_paths(meetings_root).items():
        if not path.exists():
            msg = f"canonical example for {meeting_type} not found: {path}"
            raise ScribeError(msg)
        examples[meeting_type] = path.read_text()
    return examples


def _ordered_summary_examples(
    *,
    selected_meeting_type: str,
    examples: dict[str, str],
) -> list[tuple[str, str]]:
    """Return summary examples with the selected meeting type first."""
    ordered_examples = []
    if selected_meeting_type in examples:
        ordered_examples.append((selected_meeting_type, examples[selected_meeting_type]))
    ordered_examples.extend(
        (meeting_type, content)
        for meeting_type, content in examples.items()
        if meeting_type != selected_meeting_type
    )
    return ordered_examples


def _summary_input_budget(env: Mapping[str, str]) -> int:
    """Return the configured summary input budget in characters."""
    raw_value = env.get("SCRIBE_MAX_SUMMARY_INPUT_CHARS")
    if raw_value is None:
        return _DEFAULT_MAX_SUMMARY_INPUT_CHARS
    try:
        budget = int(raw_value)
    except ValueError as exc:
        msg = "SCRIBE_MAX_SUMMARY_INPUT_CHARS must be an integer"
        raise ScribeError(msg) from exc
    if budget <= 0:
        msg = "SCRIBE_MAX_SUMMARY_INPUT_CHARS must be positive"
        raise ScribeError(msg)
    return budget


def _summary_contract(metadata: MeetingSummaryMetadata) -> str:
    """Return meeting-type-specific summary instructions."""
    if metadata.meeting_type == "L10":
        return (
            "Primary contract for L10 meetings:\n"
            "- Treat the canonical L10 example as the primary format to imitate.\n"
            "- Produce a tight EOS/L10-ready Markdown note that can be pasted into Strety.\n"
            "- Use simple WYSIWYG-safe Markdown: headings and bullets only; no tables.\n"
            "- Start with YAML front matter using title, date, meeting_type, company,\n"
            "  source when inferable, tags, then a matching H1.\n"
            "- In YAML front matter, source must equal the exact source transcript filename.\n"
            "- Include a one-sentence meeting description after the H1.\n"
            "- Use these sections when applicable: Executive Summary, Key Issues and\n"
            "  Discussion, Decisions Made, To-Dos / Next Steps, Cascading Messages,\n"
            "  Meeting Rating, Bottom Line.\n"
            "- Under Key Issues and Discussion, use thematic subsections instead of a\n"
            "  chronological transcript recap.\n"
            "- Under To-Dos / Next Steps, group action items by owner when owners are\n"
            "  clear; include only concrete actions from the transcript.\n"
            "- Capture EOS/L10 concepts such as Rocks, IDS/issues, scorecard/headlines,\n"
            "  cascading messages, and ratings only when present.\n"
            "- If a standard L10 section has no content in the transcript, state that\n"
            "  briefly rather than inventing details.\n"
            "- Preserve business-specific names, customers, systems, projects, and dates\n"
            "  accurately.\n"
            "- Be concise, but keep enough operational detail that the summary can stand\n"
            "  alone after the meeting."
        )

    return """Primary contract for non-L10 meetings:
- Treat the selected meeting-type example as the primary format to imitate.
- Produce one tight Markdown summary for review and archive.
- Prefer practical business framing over transcript recap.
- Include decisions, next steps, owners, open questions, and bottom line when present.
- Use simple WYSIWYG-safe Markdown and do not use tables.
- State briefly when no decisions or to-dos were identified rather than inventing them."""


def _build_summary_prompt(
    *,
    transcript_text: str,
    metadata: MeetingSummaryMetadata,
    examples: dict[str, str],
    revision_note: str | None,
    source_name: str | None = None,
) -> str:
    """Build the prompt used for the shared v1 meeting-summary contract."""
    revision = ""
    if revision_note and revision_note.strip():
        revision = f"\nRevision note for this retry:\n{revision_note.strip()}\n"

    example_sections = "\n\n".join(
        f"## {'Primary' if meeting_type == metadata.meeting_type else 'Supporting'} "
        f"{meeting_type} Example\n\n{content.strip()}"
        for meeting_type, content in _ordered_summary_examples(
            selected_meeting_type=metadata.meeting_type,
            examples=examples,
        )
    )

    return f"""You write concise, operator-ready Armen Stone meeting summaries.

Use the selected meeting-type example as the primary source for tone, structure,
level of detail, and practical business framing.
Generate exactly one Markdown summary. Do not wrap the response in a code fence.
The output must be useful for review and paste into Strety without material rewriting.

Meeting metadata:
- Title: {metadata.title}
- Date: {metadata.meeting_date}
- Type: {metadata.meeting_type}
- Source transcript filename: {source_name or "not provided"}
{revision}
{_summary_contract(metadata)}

{example_sections}

## Source Transcript

{transcript_text.strip()}
"""


def _request_openai_summary(*, prompt: str, api_key: str, model: str) -> str:
    """Request a summary from OpenAI's Responses API."""
    try:
        from openai import OpenAI  # noqa: PLC0415
    except ImportError as exc:
        msg = "openai is not installed. Run: uv sync"
        raise ScribeError(msg) from exc

    try:
        client = OpenAI(api_key=api_key)
        response = client.responses.create(model=model, input=prompt)
    except Exception as exc:
        msg = f"OpenAI summary generation failed: {exc}"
        raise ScribeError(msg) from exc

    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        return output_text

    msg = "OpenAI summary response did not include output_text"
    raise ScribeError(msg)


def generate_summary_from_transcript(  # noqa: PLR0913
    transcript_path: Path,
    *,
    metadata: MeetingSummaryMetadata,
    env: Mapping[str, str] = os.environ,
    meetings_root: Path = MEETINGS_ROOT,
    revision_note: str | None = None,
    request_summary: SummaryRequester = _request_openai_summary,
) -> Path:
    """Generate and atomically write a companion summary for a transcript."""
    if metadata.meeting_type not in MEETING_TYPES:
        msg = f"unsupported meeting type: {metadata.meeting_type}"
        raise ScribeError(msg)
    if not transcript_path.exists():
        msg = f"transcript file not found: {transcript_path}"
        raise ScribeError(msg)

    transcript_text = transcript_path.read_text().strip()
    if not transcript_text:
        msg = "cannot summarize an empty transcript"
        raise ScribeError(msg)

    api_key = env.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        msg = "OPENAI_API_KEY is required for summary generation"
        raise ScribeError(msg)
    model = env.get("SCRIBE_OPENAI_MODEL", _DEFAULT_OPENAI_MODEL).strip()
    if not model:
        msg = "SCRIBE_OPENAI_MODEL must not be blank"
        raise ScribeError(msg)

    examples = _read_canonical_examples(meetings_root)
    prompt = _build_summary_prompt(
        transcript_text=transcript_text,
        metadata=metadata,
        examples=examples,
        revision_note=revision_note,
        source_name=transcript_path.name,
    )
    if len(prompt) > _summary_input_budget(env):
        msg = "summary input is too large for the configured v1 budget"
        raise ScribeError(msg)

    summary_text = validate_summary_text(
        request_summary(prompt=prompt, api_key=api_key, model=model),
    )
    summary_path = default_summary_path(transcript_path)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{summary_path.name}.",
        suffix=".tmp",
        dir=summary_path.parent,
    )
    os.close(fd)
    tmp_path = Path(tmp_name)

    try:
        tmp_path.write_text(summary_text)
        tmp_path.replace(summary_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    return summary_path


def _send_to_trash(path: Path) -> None:
    """Move a file to macOS Trash."""
    if not path.exists():
        return
    script = """
on run argv
  set targetFile to POSIX file (item 1 of argv) as alias
  tell application "Finder"
    delete targetFile
  end tell
end run
"""
    try:
        subprocess.run(  # noqa: S603
            ["/usr/bin/osascript", "-", str(path)],
            input=script,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else str(exc)
        msg = f"failed to move file to Trash ({path}): {stderr}"
        raise ScribeError(msg) from exc


def _validate_finalization_inputs(
    *,
    recording: Path,
    transcript: Path,
    summary: Path,
    meeting_type: str,
    meetings_root: Path,
) -> Path:
    """Validate finalization inputs and return the destination directory."""
    if meeting_type not in MEETING_TYPES:
        msg = f"unsupported meeting type: {meeting_type}"
        raise ScribeError(msg)
    if not meetings_root.exists():
        msg = f"meetings folder not found: {meetings_root}"
        raise ScribeError(msg)
    destination_dir = meetings_root / meeting_type
    if not destination_dir.exists():
        msg = f"meeting type folder not found: {destination_dir}"
        raise ScribeError(msg)
    for label, path in (
        ("recording", recording),
        ("transcript", transcript),
        ("summary", summary),
    ):
        if not path.exists():
            msg = f"{label} file not found: {path}"
            raise ScribeError(msg)
    return destination_dir


def _destination_artifact_paths(
    *,
    destination_dir: Path,
    transcript: Path,
    summary: Path,
) -> FinalizedArtifacts:
    """Return final artifact paths, failing if either destination exists."""
    destination_transcript = destination_dir / transcript.name
    destination_summary = destination_dir / summary.name
    for destination in (destination_transcript, destination_summary):
        if destination.exists():
            msg = f"destination already exists: {destination}"
            raise ScribeError(msg)
    return FinalizedArtifacts(destination_transcript, destination_summary)


def _staging_artifact_paths(
    *,
    destination_dir: Path,
    transcript: Path,
    summary: Path,
) -> FinalizedArtifacts:
    """Return temporary artifact paths for atomic filing."""
    staged_transcript = destination_dir / f".{transcript.name}.{os.getpid()}.tmp"
    staged_summary = destination_dir / f".{summary.name}.{os.getpid()}.tmp"
    for staged in (staged_transcript, staged_summary):
        if staged.exists():
            msg = f"staging file already exists: {staged}"
            raise ScribeError(msg)
    return FinalizedArtifacts(staged_transcript, staged_summary)


def _stage_artifacts(
    *,
    transcript: Path,
    summary: Path,
    staged: FinalizedArtifacts,
) -> None:
    """Copy source artifacts into temporary destination files."""
    try:
        shutil.copy2(transcript, staged.transcript)
        shutil.copy2(summary, staged.summary)
    except OSError as exc:
        staged.transcript.unlink(missing_ok=True)
        staged.summary.unlink(missing_ok=True)
        msg = f"failed to stage meeting artifacts: {exc}"
        raise ScribeError(msg) from exc


def _rename_staged_artifacts(
    *,
    staged: FinalizedArtifacts,
    destination: FinalizedArtifacts,
) -> None:
    """Rename staged artifacts into their final destination paths."""
    renamed_transcript = False
    try:
        staged.transcript.rename(destination.transcript)
        renamed_transcript = True
        staged.summary.rename(destination.summary)
    except OSError as exc:
        rollback = ""
        if renamed_transcript:
            try:
                destination.transcript.rename(staged.transcript)
                rollback = " Rolled back staged transcript."
            except OSError as rollback_exc:
                rollback = f" Transcript rollback failed: {rollback_exc}."
        staged.transcript.unlink(missing_ok=True)
        staged.summary.unlink(missing_ok=True)
        msg = f"failed to file meeting artifacts: {exc}.{rollback}"
        raise ScribeError(msg) from exc


def finalize_meeting_artifacts(  # noqa: PLR0913
    *,
    recording: Path,
    transcript: Path,
    summary: Path,
    log_file: Path | None,
    meeting_type: str,
    meetings_root: Path = MEETINGS_ROOT,
    scratch_files: list[Path] | None = None,
    trash_file: TrashFile = _send_to_trash,
) -> FinalizedArtifacts:
    """File durable meeting artifacts, then move source-side leftovers to Trash."""
    destination_dir = _validate_finalization_inputs(
        recording=recording,
        transcript=transcript,
        summary=summary,
        meeting_type=meeting_type,
        meetings_root=meetings_root,
    )
    destination = _destination_artifact_paths(
        destination_dir=destination_dir,
        transcript=transcript,
        summary=summary,
    )
    staged = _staging_artifact_paths(
        destination_dir=destination_dir,
        transcript=transcript,
        summary=summary,
    )
    _stage_artifacts(transcript=transcript, summary=summary, staged=staged)
    _rename_staged_artifacts(staged=staged, destination=destination)

    trash_targets = [recording, transcript, summary]
    if log_file is not None:
        trash_targets.append(log_file)
    if scratch_files:
        trash_targets.extend(scratch_files)

    for target in trash_targets:
        trash_file(target)

    return destination


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


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Local transcription and speaker diarization with senko + parakeet",
    )
    parser.add_argument(
        "input_path",
        type=Path,
        help="Path to audio, transcript, or recording file",
    )
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
    parser.add_argument(
        "--meeting-type",
        choices=MEETING_TYPES,
        help="Meeting type for summary generation or final filing",
    )
    parser.add_argument(
        "--generate-summary",
        action="store_true",
        help="Generate a companion summary from an existing transcript",
    )
    parser.add_argument(
        "--revision-note",
        help="Revision note to apply when regenerating a summary",
    )
    parser.add_argument(
        "--finalize-meeting",
        action="store_true",
        help="File transcript and summary, then move source-side artifacts to Trash",
    )
    parser.add_argument(
        "--transcript",
        type=Path,
        help="Transcript path for --finalize-meeting",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        help="Summary path for --finalize-meeting",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="Log file path to trash after successful --finalize-meeting",
    )
    parser.add_argument(
        "--meetings-root",
        type=Path,
        default=MEETINGS_ROOT,
        help=argparse.SUPPRESS,
    )
    return parser


def _handle_generate_summary(args: argparse.Namespace) -> bool:
    """Handle --generate-summary mode. Return True when handled."""
    if not args.generate_summary:
        return False
    if not args.input_path.exists():
        sys.exit(f"Error: transcript file not found: {args.input_path}")
    if not args.title or not args.meeting_type:
        sys.exit("Error: --generate-summary requires --title and --meeting-type")
    summary_path = generate_summary_from_transcript(
        args.input_path,
        metadata=MeetingSummaryMetadata(
            args.title,
            args.meeting_date,
            args.meeting_type,
        ),
        meetings_root=args.meetings_root,
        revision_note=args.revision_note,
    )
    _log(f"Wrote summary to {summary_path}")
    return True


def _handle_finalize_meeting(args: argparse.Namespace) -> bool:
    """Handle --finalize-meeting mode. Return True when handled."""
    if not args.finalize_meeting:
        return False
    if not args.meeting_type or not args.transcript or not args.summary:
        sys.exit(
            "Error: --finalize-meeting requires --meeting-type, "
            "--transcript, and --summary",
        )
    result = finalize_meeting_artifacts(
        recording=args.input_path,
        transcript=args.transcript,
        summary=args.summary,
        log_file=args.log_file,
        meeting_type=args.meeting_type,
        meetings_root=args.meetings_root,
    )
    _log(f"Filed transcript to {result.transcript}")
    _log(f"Filed summary to {result.summary}")
    return True


def _write_transcription_output(args: argparse.Namespace, output: str) -> None:
    """Write transcription output to stdout or a file."""
    if args.output == "-":
        sys.stdout.write(output)
        return

    output_path = (
        Path(args.output)
        if args.output
        else default_output_path(
            args.input_path,
            output_format=args.output_format,
            title=args.title,
            meeting_date=args.meeting_date,
        )
    )
    output_path.write_text(output)
    _log(f"Wrote transcript to {output_path}")


def _handle_transcription(args: argparse.Namespace) -> None:
    """Handle the default transcription mode."""
    if not args.input_path.exists():
        sys.exit(f"Error: audio file not found: {args.input_path}")
    if args.snippet_seconds <= 0:
        sys.exit("Error: --snippet-seconds must be greater than 0")

    metadata = MeetingMetadata(args.title, args.meeting_date)
    speaker_labeling = SpeakerLabelingOptions(args.label_speakers, args.snippet_seconds)

    with _prepare_audio(args.input_path) as wav_path:
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

    _write_transcription_output(args, output)


def main() -> None:
    """CLI entry point."""
    args = _build_parser().parse_args()

    try:
        if _handle_generate_summary(args):
            return
        if _handle_finalize_meeting(args):
            return
        _handle_transcription(args)
    except ScribeError as exc:
        sys.exit(f"Error: {exc}")


if __name__ == "__main__":
    main()
