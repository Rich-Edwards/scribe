"""Tests for the scribe module."""

import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scribe import (
    CANONICAL_SUMMARY_EXAMPLES,
    MEETING_TYPES,
    UNKNOWN_SPEAKER,
    DiarizationSegment,
    FinalizedArtifacts,
    MeetingSummaryMetadata,
    MergedSegment,
    ScribeError,
    SpeakerSample,
    TranscriptionSegment,
    _build_summary_prompt,
    _create_diarizer,
    _find_ffmpeg,
    _find_uvx,
    _prepare_audio,
    _select_speaker_samples,
    _send_to_trash,
    default_output_path,
    default_summary_path,
    finalize_meeting_artifacts,
    format_json,
    format_text,
    format_transcript_json,
    format_transcript_text,
    generate_summary_from_transcript,
    main,
    merge,
    prompt_speaker_labels,
    rename_speakers,
    run_diarization,
    run_transcription,
    validate_summary_text,
)


class TestMerge:
    """Tests for merge()."""

    def test_perfect_overlap(self):
        diarization = [DiarizationSegment("A", 0.0, 5.0)]
        transcription = [TranscriptionSegment("hello", 0.0, 5.0)]
        result = merge(diarization, transcription)
        assert result == [MergedSegment("A", "hello", 0.0, 5.0)]

    def test_partial_overlap_picks_best(self):
        diarization = [
            DiarizationSegment("A", 0.0, 3.0),
            DiarizationSegment("B", 2.0, 6.0),
        ]
        transcription = [TranscriptionSegment("hello", 1.0, 5.0)]
        result = merge(diarization, transcription)
        # B overlaps 2.0-5.0 = 3.0s, A overlaps 1.0-3.0 = 2.0s
        assert result[0].speaker == "B"

    def test_no_overlap_gives_unknown(self):
        diarization = [DiarizationSegment("A", 10.0, 15.0)]
        transcription = [TranscriptionSegment("hello", 0.0, 5.0)]
        result = merge(diarization, transcription)
        assert result[0].speaker == UNKNOWN_SPEAKER

    def test_empty_diarization(self):
        transcription = [TranscriptionSegment("hello", 0.0, 5.0)]
        result = merge([], transcription)
        assert result[0].speaker == UNKNOWN_SPEAKER

    def test_empty_transcription(self):
        diarization = [DiarizationSegment("A", 0.0, 5.0)]
        result = merge(diarization, [])
        assert result == []

    def test_both_empty(self):
        assert merge([], []) == []

    def test_multiple_speakers(self):
        diarization = [
            DiarizationSegment("A", 0.0, 5.0),
            DiarizationSegment("B", 5.0, 10.0),
        ]
        transcription = [
            TranscriptionSegment("first", 0.0, 4.0),
            TranscriptionSegment("second", 6.0, 9.0),
        ]
        result = merge(diarization, transcription)
        assert result[0].speaker == "A"
        assert result[1].speaker == "B"

    def test_preserves_timestamps(self):
        diarization = [DiarizationSegment("A", 0.0, 10.0)]
        transcription = [TranscriptionSegment("hi", 1.5, 3.7)]
        result = merge(diarization, transcription)
        assert result[0].start == 1.5
        assert result[0].end == 3.7


class TestFormatText:
    """Tests for format_text()."""

    def test_basic(self):
        segments = [MergedSegment("A", "hello world", 0.0, 2.0)]
        assert format_text(segments) == "A: hello world\n"

    def test_consecutive_same_speaker_merged(self):
        segments = [
            MergedSegment("A", "hello", 0.0, 1.0),
            MergedSegment("A", "world", 1.0, 2.0),
        ]
        assert format_text(segments) == "A: hello world\n"

    def test_speaker_change(self):
        segments = [
            MergedSegment("A", "hello", 0.0, 1.0),
            MergedSegment("B", "world", 1.0, 2.0),
        ]
        result = format_text(segments)
        assert "A: hello" in result
        assert "B: world" in result

    def test_blank_line_between_speakers(self):
        segments = [
            MergedSegment("A", "hello", 0.0, 1.0),
            MergedSegment("B", "world", 1.0, 2.0),
        ]
        result = format_text(segments)
        assert "\n\n" in result

    def test_empty(self):
        assert format_text([]) == ""

    def test_meeting_heading(self):
        segments = [MergedSegment("A", "hello world", 0.0, 2.0)]
        assert (
            format_text(segments, title="Sales and CS L10", meeting_date="2026-06-06")
            == "# Sales and CS L10 - 2026-06-06\n\nA: hello world\n"
        )


class TestFormatJson:
    """Tests for format_json()."""

    def test_basic_structure(self):
        segments = [MergedSegment("A", "hello", 0.531, 2.874)]
        data = json.loads(format_json(segments))
        assert data["speakers"] == ["A"]
        assert len(data["segments"]) == 1
        assert data["segments"][0]["speaker"] == "A"
        assert data["segments"][0]["text"] == "hello"
        assert data["segments"][0]["start"] == 0.531
        assert data["segments"][0]["end"] == 2.874

    def test_empty(self):
        data = json.loads(format_json([]))
        assert data["speakers"] == []
        assert data["segments"] == []

    def test_speakers_sorted(self):
        segments = [
            MergedSegment("B", "first", 0.0, 1.0),
            MergedSegment("A", "second", 1.0, 2.0),
        ]
        data = json.loads(format_json(segments))
        assert data["speakers"] == ["A", "B"]

    def test_timestamps_rounded(self):
        segments = [MergedSegment("A", "hi", 1.23456789, 2.98765432)]
        data = json.loads(format_json(segments))
        assert data["segments"][0]["start"] == 1.235
        assert data["segments"][0]["end"] == 2.988

    def test_metadata_when_titled(self):
        segments = [MergedSegment("A", "hi", 1.0, 2.0)]
        data = json.loads(format_json(segments, title="Ops L10", meeting_date="2026-06-06"))
        assert data["title"] == "Ops L10"
        assert data["date"] == "2026-06-06"


class TestSpeakerLabels:
    """Tests for speaker naming helpers."""

    def test_rename_speakers(self):
        segments = [
            MergedSegment("SPEAKER_00", "hello", 0.0, 1.0),
            MergedSegment("SPEAKER_01", "world", 1.0, 2.0),
        ]
        result = rename_speakers(segments, {"SPEAKER_00": "Rich"})
        assert result == [
            MergedSegment("Rich", "hello", 0.0, 1.0),
            MergedSegment("SPEAKER_01", "world", 1.0, 2.0),
        ]

    def test_select_speaker_samples_uses_longest_segment_and_caps_duration(self):
        diarization = [
            DiarizationSegment("SPEAKER_00", 0.0, 3.0),
            DiarizationSegment("SPEAKER_00", 10.0, 30.0),
            DiarizationSegment("SPEAKER_01", 40.0, 44.0),
        ]
        assert _select_speaker_samples(diarization, snippet_seconds=8.0) == [
            SpeakerSample("SPEAKER_00", 10.0, 18.0),
            SpeakerSample("SPEAKER_01", 40.0, 44.0),
        ]

    def test_prompt_speaker_labels_waits_before_playback(self, monkeypatch):
        prompts = []
        played_samples = []
        responses = iter(["", "Rich"])

        def fake_input(prompt):
            prompts.append(prompt)
            return next(responses)

        def fake_play(_audio_path, sample):
            played_samples.append(sample)

        monkeypatch.setattr("builtins.input", fake_input)
        monkeypatch.setattr("scribe._play_audio_sample", fake_play)

        result = prompt_speaker_labels(
            Path("recordings/meeting.wav"),
            [DiarizationSegment("SPEAKER_00", 10.0, 20.0)],
            snippet_seconds=5.0,
        )

        assert prompts == [
            "Press Enter when you are ready to identify speakers.",
            "Name for SPEAKER_00: ",
        ]
        assert played_samples == [SpeakerSample("SPEAKER_00", 10.0, 15.0)]
        assert result == {"SPEAKER_00": "Rich"}


class TestDefaultOutputPath:
    """Tests for default transcript output path selection."""

    def test_untitled_text_uses_input_stem(self):
        assert default_output_path(
            Path("recordings/meeting.m4a"),
            output_format="text",
            title=None,
            meeting_date="2026-06-06",
        ) == Path("recordings/meeting.txt")

    def test_titled_text_uses_markdown_transcript_name(self):
        assert default_output_path(
            Path("recordings/recording.m4a"),
            output_format="text",
            title="Sales and CS L10",
            meeting_date="2026-06-06",
        ) == Path("recordings/Sales-and-CS-L10_2026-06-06_Transcript.md")

    def test_titled_json_uses_json_transcript_name(self):
        assert default_output_path(
            Path("recordings/recording.m4a"),
            output_format="json",
            title="Sales and CS L10",
            meeting_date="2026-06-06",
        ) == Path("recordings/Sales-and-CS-L10_2026-06-06_Transcript.json")


class TestFormatTranscriptText:
    """Tests for format_transcript_text()."""

    def test_basic(self):
        segments = [TranscriptionSegment("hello world", 0.0, 2.0)]
        assert format_transcript_text(segments) == "hello world\n"

    def test_multiple_segments_joined(self):
        segments = [
            TranscriptionSegment("hello", 0.0, 1.0),
            TranscriptionSegment("world", 1.0, 2.0),
        ]
        assert format_transcript_text(segments) == "hello world\n"

    def test_empty(self):
        assert format_transcript_text([]) == ""


class TestFormatTranscriptJson:
    """Tests for format_transcript_json()."""

    def test_basic_structure(self):
        segments = [TranscriptionSegment("hello", 0.531, 2.874)]
        data = json.loads(format_transcript_json(segments))
        assert "speakers" not in data
        assert len(data["segments"]) == 1
        assert "speaker" not in data["segments"][0]
        assert data["segments"][0]["text"] == "hello"
        assert data["segments"][0]["start"] == 0.531
        assert data["segments"][0]["end"] == 2.874

    def test_empty(self):
        data = json.loads(format_transcript_json([]))
        assert data["segments"] == []

    def test_timestamps_rounded(self):
        segments = [TranscriptionSegment("hi", 1.23456789, 2.98765432)]
        data = json.loads(format_transcript_json(segments))
        assert data["segments"][0]["start"] == 1.235
        assert data["segments"][0]["end"] == 2.988


class TestFindFfmpeg:
    """Tests for _find_ffmpeg()."""

    def test_finds_ffmpeg(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/ffmpeg")
        assert _find_ffmpeg() == "/usr/local/bin/ffmpeg"

    def test_missing_ffmpeg_raises(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _: None)
        with pytest.raises(ScribeError, match="ffmpeg is required"):
            _find_ffmpeg()


class TestFindUvx:
    """Tests for _find_uvx()."""

    def test_finds_uvx(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/uvx")
        assert _find_uvx() == "/usr/local/bin/uvx"

    def test_missing_uvx_raises(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _: None)
        with pytest.raises(ScribeError, match="uvx not found"):
            _find_uvx()


@pytest.fixture
def _mock_ffmpeg(monkeypatch):
    """Stub out ffmpeg discovery and subprocess.run for _prepare_audio tests."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_a, **_kw: subprocess.CompletedProcess([], 0),
    )


class TestPrepareAudio:
    """Tests for _prepare_audio()."""

    @pytest.mark.usefixtures("_mock_ffmpeg")
    def test_wav_still_converts(self, tmp_path):
        wav = tmp_path / "test.wav"
        wav.touch()
        with _prepare_audio(wav) as result:
            assert result.suffix == ".wav"
            assert result != wav

    @pytest.mark.usefixtures("_mock_ffmpeg")
    def test_non_wav_converts(self, tmp_path):
        mp4 = tmp_path / "test.mp4"
        mp4.touch()
        with _prepare_audio(mp4) as result:
            assert result.suffix == ".wav"
            assert result != mp4

    @pytest.mark.usefixtures("_mock_ffmpeg")
    def test_temp_file_cleaned_up(self, tmp_path):
        mp4 = tmp_path / "test.mp4"
        mp4.touch()
        temp_path = None
        with _prepare_audio(mp4) as result:
            temp_path = result
        assert not temp_path.exists()

    def test_ffmpeg_failure_raises(self, tmp_path, monkeypatch):
        mp4 = tmp_path / "test.mp4"
        mp4.touch()
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ffmpeg")

        def fail_run(*_args, **_kwargs):
            raise subprocess.CalledProcessError(1, "ffmpeg", stderr="bad codec")

        monkeypatch.setattr("subprocess.run", fail_run)
        with pytest.raises(ScribeError, match="ffmpeg conversion failed"), _prepare_audio(mp4):
            pass


class TestCreateDiarizer:
    """Tests for _create_diarizer()."""

    def test_missing_senko_raises(self, monkeypatch):
        import builtins  # noqa: PLC0415

        real_import = builtins.__import__

        def block_senko(name, *args, **kwargs):
            if name == "senko":
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_senko)
        with pytest.raises(ScribeError, match="senko is not installed"):
            _create_diarizer()

    def test_init_failure_raises(self, monkeypatch):
        mock_senko = MagicMock()
        mock_senko.Diarizer.side_effect = RuntimeError("CoreML init failed")
        monkeypatch.setitem(sys.modules, "senko", mock_senko)
        with pytest.raises(ScribeError, match="Failed to initialize diarizer"):
            _create_diarizer()


class TestRunTranscription:
    """Tests for run_transcription()."""

    def _mock_parakeet(self, monkeypatch, json_data):
        """Set up mocks for run_transcription with given JSON output."""
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/uvx")

        def fake_run(cmd, **_kwargs):
            idx = cmd.index("--output-dir")
            out_dir = cmd[idx + 1]
            out_file = Path(out_dir) / "output.json"
            out_file.write_text(json.dumps(json_data))
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr("subprocess.run", fake_run)

    def test_segments_key(self, monkeypatch, tmp_path):
        data = {"segments": [{"text": "hello", "start": 0.0, "end": 1.0}]}
        self._mock_parakeet(monkeypatch, data)
        result = run_transcription(tmp_path / "audio.wav")
        assert len(result) == 1
        assert result[0].text == "hello"

    def test_sentences_key_fallback(self, monkeypatch, tmp_path):
        data = {"sentences": [{"text": "world", "start": 1.0, "end": 2.0}]}
        self._mock_parakeet(monkeypatch, data)
        result = run_transcription(tmp_path / "audio.wav")
        assert len(result) == 1
        assert result[0].text == "world"

    def test_empty_output(self, monkeypatch, tmp_path):
        data = {"segments": []}
        self._mock_parakeet(monkeypatch, data)
        result = run_transcription(tmp_path / "audio.wav")
        assert result == []

    def test_missing_fields_skipped(self, monkeypatch, tmp_path):
        data = {"segments": [{"text": "ok"}, {"text": "hi", "start": 0.0, "end": 1.0}]}
        self._mock_parakeet(monkeypatch, data)
        result = run_transcription(tmp_path / "audio.wav")
        assert len(result) == 1
        assert result[0].text == "hi"

    def test_invalid_json_raises(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/uvx")

        def fake_run(cmd, **_kwargs):
            idx = cmd.index("--output-dir")
            out_dir = cmd[idx + 1]
            out_file = Path(out_dir) / "output.json"
            out_file.write_text("{invalid")
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr("subprocess.run", fake_run)
        with pytest.raises(ScribeError, match="invalid JSON"):
            run_transcription(Path("/fake/audio.wav"))

    def test_subprocess_failure_raises(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/uvx")

        def fail_run(*_args, **_kwargs):
            raise subprocess.CalledProcessError(1, "parakeet-mlx", stderr="out of memory")

        monkeypatch.setattr("subprocess.run", fail_run)
        with pytest.raises(ScribeError, match="parakeet-mlx failed"):
            run_transcription(Path("/fake/audio.wav"))

    def test_no_json_output_raises(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/uvx")
        monkeypatch.setattr(
            "subprocess.run",
            lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0),
        )
        with pytest.raises(ScribeError, match="no JSON output"):
            run_transcription(Path("/fake/audio.wav"))


class TestRunDiarization:
    """Tests for run_diarization()."""

    def test_returns_segments(self):
        diarizer = MagicMock()
        diarizer.diarize.return_value = {
            "merged_segments": [
                {"speaker": "SPEAKER_01", "start": 0.0, "end": 2.5},
                {"speaker": "SPEAKER_02", "start": 2.5, "end": 5.0},
            ],
        }
        result = run_diarization(diarizer, Path("/fake/audio.wav"))
        assert len(result) == 2
        assert result[0] == DiarizationSegment("SPEAKER_01", 0.0, 2.5)
        assert result[1] == DiarizationSegment("SPEAKER_02", 2.5, 5.0)

    def test_none_result_returns_empty(self):
        diarizer = MagicMock()
        diarizer.diarize.return_value = None
        result = run_diarization(diarizer, Path("/fake/audio.wav"))
        assert result == []

    def test_empty_segments_returns_empty(self):
        diarizer = MagicMock()
        diarizer.diarize.return_value = {"merged_segments": []}
        result = run_diarization(diarizer, Path("/fake/audio.wav"))
        assert result == []

    def test_malformed_result_raises(self):
        diarizer = MagicMock()
        diarizer.diarize.return_value = {"wrong_key": []}
        with pytest.raises(ScribeError, match="Diarization failed"):
            run_diarization(diarizer, Path("/fake/audio.wav"))

    def test_wraps_diarizer_errors(self):
        diarizer = MagicMock()
        diarizer.diarize.side_effect = RuntimeError("boom")
        with pytest.raises(ScribeError, match="Diarization failed"):
            run_diarization(diarizer, Path("/fake/audio.wav"))


class TestSummaryGeneration:
    """Tests for API-backed summary helpers."""

    def test_default_summary_path_replaces_transcript_suffix(self):
        assert default_summary_path(Path("Sales_2026-06-27_Transcript.md")) == Path(
            "Sales_2026-06-27_Summary.md",
        )

    def test_default_summary_path_rejects_unknown_transcript_name(self):
        with pytest.raises(ScribeError, match=r"_Transcript\.md"):
            default_summary_path(Path("meeting.md"))

    def test_validate_summary_text_rejects_blank(self):
        with pytest.raises(ScribeError, match="empty summary"):
            validate_summary_text("  \n")

    def test_validate_summary_text_rejects_code_fence_wrapper(self):
        with pytest.raises(ScribeError, match="code fence"):
            validate_summary_text("```markdown\n# Summary\nBody\n```")

    def test_validate_summary_text_requires_heading(self):
        with pytest.raises(ScribeError, match="heading"):
            validate_summary_text("Summary\n\nBody")

    def test_validate_summary_text_allows_front_matter_before_heading(self):
        summary = "---\ntitle: Meeting\n---\n# Meeting Summary\n\nBody"
        assert validate_summary_text(summary) == f"{summary}\n"

    def test_validate_summary_text_rejects_unclosed_front_matter(self):
        with pytest.raises(ScribeError, match="front matter"):
            validate_summary_text("---\ntitle: Meeting\n# Meeting Summary\n")

    def test_generate_summary_missing_api_key_raises(self, tmp_path):
        transcript = tmp_path / "Meeting_2026-06-27_Transcript.md"
        transcript.write_text("# Meeting\n\nRich: hello\n")

        with pytest.raises(ScribeError, match="OPENAI_API_KEY"):
            generate_summary_from_transcript(
                transcript,
                metadata=MeetingSummaryMetadata("Meeting", "2026-06-27", "Other"),
                env={},
                meetings_root=tmp_path,
                request_summary=lambda *_a, **_kw: "# Should not run\n",
            )

    def test_generate_summary_missing_example_raises(self, tmp_path):
        transcript = tmp_path / "Meeting_2026-06-27_Transcript.md"
        transcript.write_text("# Meeting\n\nRich: hello\n")

        with pytest.raises(ScribeError, match="canonical example"):
            generate_summary_from_transcript(
                transcript,
                metadata=MeetingSummaryMetadata("Meeting", "2026-06-27", "Other"),
                env={"OPENAI_API_KEY": "key"},
                meetings_root=tmp_path,
                request_summary=lambda *_a, **_kw: "# Should not run\n",
            )

    def test_generate_summary_writes_valid_summary(self, tmp_path):
        meetings_root = tmp_path / "meetings"
        for meeting_type in MEETING_TYPES:
            (meetings_root / meeting_type).mkdir(parents=True)
        for meeting_type, relative_path in CANONICAL_SUMMARY_EXAMPLES.items():
            path = meetings_root / meeting_type / relative_path
            path.write_text(f"# {meeting_type} Example\n\nUseful notes.\n")

        transcript = tmp_path / "Meeting_2026-06-27_Transcript.md"
        transcript.write_text("# Meeting\n\nRich: hello\n")
        calls = []

        def fake_request(*, prompt, api_key, model):
            calls.append((prompt, api_key, model))
            return "# Meeting 2026-06-27 Summary\n\n## Executive Summary\n- Useful note.\n"

        summary_path = generate_summary_from_transcript(
            transcript,
            metadata=MeetingSummaryMetadata("Meeting", "2026-06-27", "Other"),
            env={"OPENAI_API_KEY": "key", "SCRIBE_OPENAI_MODEL": "test-model"},
            meetings_root=meetings_root,
            request_summary=fake_request,
        )

        assert summary_path == tmp_path / "Meeting_2026-06-27_Summary.md"
        assert "Useful note" in summary_path.read_text()
        assert calls
        assert calls[0][1:] == ("key", "test-model")
        assert "Rich: hello" in calls[0][0]

    def test_l10_prompt_uses_l10_specific_contract_and_primary_example(self):
        prompt = _build_summary_prompt(
            transcript_text="# Sales L10\n\nRich: Review rocks and IDS issues.",
            metadata=MeetingSummaryMetadata("Sales L10", "2026-06-27", "L10"),
            examples={
                "Customer": "# Customer Example\n\nCustomer format.",
                "L10": "# L10 Example\n\n## Meeting Rating\n- Rich rated it a 9.",
                "Other": "# Other Example\n\nOther format.",
            },
            revision_note=None,
            source_name="Sales-L10_2026-06-27_Transcript.md",
        )

        assert "Primary contract for L10 meetings" in prompt
        assert "YAML front matter" in prompt
        assert "WYSIWYG-safe Markdown" in prompt
        assert "no tables" in prompt
        assert "Sales-L10_2026-06-27_Transcript.md" in prompt
        assert "source must equal the exact source transcript filename" in prompt
        assert "To-Dos / Next Steps" in prompt
        assert "Cascading Messages" in prompt
        assert "Meeting Rating" in prompt
        assert prompt.index("## Primary L10 Example") < prompt.index(
            "## Supporting Customer Example",
        )

    def test_generate_summary_preserves_existing_summary_on_invalid_retry(self, tmp_path):
        meetings_root = tmp_path / "meetings"
        for meeting_type in MEETING_TYPES:
            (meetings_root / meeting_type).mkdir(parents=True)
        for meeting_type, relative_path in CANONICAL_SUMMARY_EXAMPLES.items():
            (meetings_root / meeting_type / relative_path).write_text("# Example\n\nBody\n")

        transcript = tmp_path / "Meeting_2026-06-27_Transcript.md"
        transcript.write_text("# Meeting\n\nRich: hello\n")
        summary = tmp_path / "Meeting_2026-06-27_Summary.md"
        summary.write_text("# Existing\n\nKeep me.\n")

        with pytest.raises(ScribeError, match="empty summary"):
            generate_summary_from_transcript(
                transcript,
                metadata=MeetingSummaryMetadata("Meeting", "2026-06-27", "Other"),
                env={"OPENAI_API_KEY": "key"},
                meetings_root=meetings_root,
                request_summary=lambda *_a, **_kw: "",
            )

        assert summary.read_text() == "# Existing\n\nKeep me.\n"


class TestFinalizeMeetingArtifacts:
    """Tests for safe meeting artifact finalization."""

    def test_send_to_trash_uses_alias_for_posix_path(self, tmp_path, monkeypatch):
        target = tmp_path / "recording.m4a"
        target.write_text("audio")
        run_mock = MagicMock()
        monkeypatch.setattr(subprocess, "run", run_mock)

        _send_to_trash(target)

        assert run_mock.call_args.args[0] == ["/usr/bin/osascript", "-", str(target)]
        assert "as alias" in run_mock.call_args.kwargs["input"]
        assert "delete targetFile" in run_mock.call_args.kwargs["input"]

    def test_finalize_stages_files_then_trashes_sources(self, tmp_path):
        root = tmp_path / "meetings"
        dest = root / "L10"
        dest.mkdir(parents=True)
        recording = tmp_path / "recording.m4a"
        transcript = tmp_path / "Meeting_2026-06-27_Transcript.md"
        summary = tmp_path / "Meeting_2026-06-27_Summary.md"
        log = tmp_path / "recording.scribe.log"
        for path in (recording, transcript, summary, log):
            path.write_text(path.name)
        trashed = []

        result = finalize_meeting_artifacts(
            recording=recording,
            transcript=transcript,
            summary=summary,
            log_file=log,
            meeting_type="L10",
            meetings_root=root,
            trash_file=trashed.append,
        )

        assert result.transcript == dest / transcript.name
        assert result.summary == dest / summary.name
        assert result.transcript.read_text() == transcript.name
        assert result.summary.read_text() == summary.name
        assert trashed == [recording, transcript, summary, log]

    def test_finalize_rejects_destination_collision(self, tmp_path):
        root = tmp_path / "meetings"
        dest = root / "Other"
        dest.mkdir(parents=True)
        recording = tmp_path / "recording.m4a"
        transcript = tmp_path / "Meeting_2026-06-27_Transcript.md"
        summary = tmp_path / "Meeting_2026-06-27_Summary.md"
        for path in (recording, transcript, summary):
            path.write_text(path.name)
        (dest / summary.name).write_text("existing")
        trashed = []

        with pytest.raises(ScribeError, match="already exists"):
            finalize_meeting_artifacts(
                recording=recording,
                transcript=transcript,
                summary=summary,
                log_file=None,
                meeting_type="Other",
                meetings_root=root,
                trash_file=trashed.append,
            )

        assert trashed == []
        assert transcript.exists()
        assert summary.exists()

    def test_finalize_rejects_missing_root(self, tmp_path):
        with pytest.raises(ScribeError, match="meetings folder not found"):
            finalize_meeting_artifacts(
                recording=tmp_path / "recording.m4a",
                transcript=tmp_path / "Transcript.md",
                summary=tmp_path / "Summary.md",
                log_file=None,
                meeting_type="Other",
                meetings_root=tmp_path / "missing",
                trash_file=lambda _path: None,
            )


class TestMain:
    """Tests for main() CLI entry point."""

    def test_file_not_found(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["scribe", "/nonexistent/audio.wav"])
        with pytest.raises(SystemExit, match="audio file not found"):
            main()

    def test_diarize_to_file(self, tmp_path, monkeypatch):
        audio = tmp_path / "meeting.wav"
        audio.touch()
        out = tmp_path / "meeting.txt"

        @contextmanager
        def fake_prepare(path):
            yield path

        monkeypatch.setattr("scribe._prepare_audio", fake_prepare)
        monkeypatch.setattr(
            "scribe._run_with_diarization",
            lambda *_a, **_kw: "SPEAKER_00: hello\n",
        )
        monkeypatch.setattr("sys.argv", ["scribe", str(audio), "-o", str(out)])
        main()
        assert out.read_text() == "SPEAKER_00: hello\n"

    def test_no_diarize_to_stdout(self, tmp_path, monkeypatch, capsys):
        audio = tmp_path / "meeting.wav"
        audio.touch()

        @contextmanager
        def fake_prepare(path):
            yield path

        monkeypatch.setattr("scribe._prepare_audio", fake_prepare)
        monkeypatch.setattr(
            "scribe._run_transcript_only",
            lambda *_a, **_kw: "hello world\n",
        )
        monkeypatch.setattr(
            "sys.argv",
            ["scribe", str(audio), "-o", "-", "--no-diarize"],
        )
        main()
        assert capsys.readouterr().out == "hello world\n"

    def test_default_output_path_json(self, tmp_path, monkeypatch):
        audio = tmp_path / "meeting.m4a"
        audio.touch()

        @contextmanager
        def fake_prepare(path):
            yield path

        monkeypatch.setattr("scribe._prepare_audio", fake_prepare)
        monkeypatch.setattr(
            "scribe._run_with_diarization",
            lambda *_a, **_kw: '{"speakers": []}\n',
        )
        monkeypatch.setattr(
            "sys.argv",
            ["scribe", str(audio), "--format", "json"],
        )
        main()
        expected = tmp_path / "meeting.json"
        assert expected.exists()
        assert expected.read_text() == '{"speakers": []}\n'

    def test_titled_default_output_path(self, tmp_path, monkeypatch):
        audio = tmp_path / "source.m4a"
        audio.touch()

        @contextmanager
        def fake_prepare(path):
            yield path

        monkeypatch.setattr("scribe._prepare_audio", fake_prepare)
        monkeypatch.setattr(
            "scribe._run_with_diarization",
            lambda *_a, **_kw: "# Sales and CS L10 - 2026-06-06\n\nRich: hello\n",
        )
        monkeypatch.setattr(
            "sys.argv",
            [
                "scribe",
                str(audio),
                "--title",
                "Sales and CS L10",
                "--date",
                "2026-06-06",
            ],
        )
        main()
        expected = tmp_path / "Sales-and-CS-L10_2026-06-06_Transcript.md"
        assert expected.read_text() == "# Sales and CS L10 - 2026-06-06\n\nRich: hello\n"

    def test_label_speakers_args_passed_to_diarization_runner(self, tmp_path, monkeypatch):
        audio = tmp_path / "meeting.wav"
        audio.touch()
        run_mock = MagicMock(return_value="Rich: hello\n")

        @contextmanager
        def fake_prepare(path):
            yield path

        monkeypatch.setattr("scribe._prepare_audio", fake_prepare)
        monkeypatch.setattr("scribe._run_with_diarization", run_mock)
        monkeypatch.setattr(
            "sys.argv",
            [
                "scribe",
                str(audio),
                "--label-speakers",
                "--snippet-seconds",
                "4",
            ],
        )
        main()
        speaker_labeling = run_mock.call_args.kwargs["speaker_labeling"]
        assert speaker_labeling.enabled is True
        assert speaker_labeling.snippet_seconds == 4

    def test_generate_summary_cli(self, tmp_path, monkeypatch):
        transcript = tmp_path / "Meeting_2026-06-27_Transcript.md"
        transcript.write_text("# Meeting\n\nRich: hello\n")
        run_mock = MagicMock(return_value=tmp_path / "Meeting_2026-06-27_Summary.md")
        monkeypatch.setattr("scribe.generate_summary_from_transcript", run_mock)
        monkeypatch.setattr(
            "sys.argv",
            [
                "scribe",
                str(transcript),
                "--generate-summary",
                "--title",
                "Meeting",
                "--date",
                "2026-06-27",
                "--meeting-type",
                "Other",
                "--revision-note",
                "shorter",
            ],
        )

        main()

        assert run_mock.call_args.args == (transcript,)
        assert run_mock.call_args.kwargs["metadata"] == MeetingSummaryMetadata(
            "Meeting",
            "2026-06-27",
            "Other",
        )
        assert run_mock.call_args.kwargs["revision_note"] == "shorter"

    def test_finalize_meeting_cli(self, tmp_path, monkeypatch):
        recording = tmp_path / "recording.m4a"
        transcript = tmp_path / "Meeting_2026-06-27_Transcript.md"
        summary = tmp_path / "Meeting_2026-06-27_Summary.md"
        log = tmp_path / "recording.scribe.log"
        for path in (recording, transcript, summary, log):
            path.write_text(path.name)
        run_mock = MagicMock(
            return_value=FinalizedArtifacts(
                tmp_path / "filed_transcript.md",
                tmp_path / "filed_summary.md",
            ),
        )
        monkeypatch.setattr("scribe.finalize_meeting_artifacts", run_mock)
        monkeypatch.setattr(
            "sys.argv",
            [
                "scribe",
                str(recording),
                "--finalize-meeting",
                "--meeting-type",
                "L10",
                "--transcript",
                str(transcript),
                "--summary",
                str(summary),
                "--log-file",
                str(log),
            ],
        )

        main()

        assert run_mock.call_args.kwargs["recording"] == recording
        assert run_mock.call_args.kwargs["transcript"] == transcript
        assert run_mock.call_args.kwargs["summary"] == summary
        assert run_mock.call_args.kwargs["log_file"] == log
        assert run_mock.call_args.kwargs["meeting_type"] == "L10"

    def test_scribe_error_exits(self, tmp_path, monkeypatch):
        audio = tmp_path / "meeting.wav"
        audio.touch()

        @contextmanager
        def fake_prepare(path):
            yield path

        monkeypatch.setattr("scribe._prepare_audio", fake_prepare)
        monkeypatch.setattr(
            "scribe._run_with_diarization",
            MagicMock(side_effect=ScribeError("diarization broke")),
        )
        monkeypatch.setattr("sys.argv", ["scribe", str(audio)])
        with pytest.raises(SystemExit, match="diarization broke"):
            main()
