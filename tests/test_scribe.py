"""Tests for the scribe module."""

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scribe import (
    UNKNOWN_SPEAKER,
    DiarizationSegment,
    MergedSegment,
    ScribeError,
    TranscriptionSegment,
    _find_ffmpeg,
    _find_uvx,
    _prepare_audio,
    format_json,
    format_text,
    format_transcript_json,
    format_transcript_text,
    load_pipeline,
    main,
    merge,
    run_diarization,
    run_transcription,
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


class TestLoadPipeline:
    """Tests for load_pipeline()."""

    def test_missing_hf_token_raises(self, monkeypatch):
        monkeypatch.delenv("HF_TOKEN", raising=False)
        with pytest.raises(ScribeError, match="HF_TOKEN"):
            load_pipeline()

    def test_missing_pyannote_raises(self, monkeypatch):
        monkeypatch.setenv("HF_TOKEN", "hf_fake")
        import builtins  # noqa: PLC0415

        real_import = builtins.__import__

        def block_pyannote(name, *args, **kwargs):
            if "pyannote" in name or "torch" in name:
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_pyannote)
        with pytest.raises(ScribeError, match=r"pyannote\.audio is not installed"):
            load_pipeline()


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

    def test_wraps_pipeline_errors(self):
        pipeline = MagicMock(side_effect=RuntimeError("boom"))
        with pytest.raises(ScribeError, match="Diarization failed"):
            run_diarization(pipeline, Path("/fake/audio.wav"))


class TestMain:
    """Tests for main() CLI entry point."""

    def test_file_not_found(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv", ["scribe", "/nonexistent/audio.wav"]
        )
        with pytest.raises(SystemExit, match="audio file not found"):
            main()

    def test_num_speakers_zero(self, tmp_path, monkeypatch):
        audio = tmp_path / "test.wav"
        audio.touch()
        monkeypatch.setattr(
            "sys.argv", ["scribe", str(audio), "--num-speakers", "0"]
        )
        with pytest.raises(SystemExit, match="--num-speakers must be >= 1"):
            main()

    def test_min_greater_than_max(self, tmp_path, monkeypatch):
        audio = tmp_path / "test.wav"
        audio.touch()
        monkeypatch.setattr(
            "sys.argv",
            ["scribe", str(audio), "--min-speakers", "5", "--max-speakers", "2"],
        )
        with pytest.raises(SystemExit, match="--min-speakers must be <= --max-speakers"):
            main()

    def test_num_with_min_speakers(self, tmp_path, monkeypatch):
        audio = tmp_path / "test.wav"
        audio.touch()
        monkeypatch.setattr(
            "sys.argv",
            ["scribe", str(audio), "--num-speakers", "2", "--min-speakers", "1"],
        )
        with pytest.raises(SystemExit, match="--num-speakers cannot be combined"):
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
        monkeypatch.setattr(
            "sys.argv", ["scribe", str(audio), "-o", str(out)]
        )
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
