# 2026-06-06 Meeting Workflow Session

This note captures the Codex/ChatGPT session that added the first usable meeting-transcription workflow around Scribe. It is intended as project context for future work, especially the summarization branch of the workflow.

## Starting Point

- Prior setup came from a ChatGPT shared chat: https://chatgpt.com/share/6a24143a-a03c-8329-bdad-fdee8104931b
- The shared chat page was reachable, but the browsable text did not expose the conversation body, so the local repo became the source of truth.
- Local project: `/Users/edwards/Documents/Git/scribe`
- Existing pipeline: `senko` diarization plus `parakeet-mlx` transcription, with `scribe_picker.zsh` as the macOS picker launcher.

## User Requirements Captured

- After transcription and diarization, play a short snippet for each detected speaker.
- Prompt for each speaker's actual name and use those names in the transcript.
- Pause before the first speaker sample so the user can get ready to listen.
- Ask for a meeting title and date.
- Use the title/date in the transcript filename.
- Put the meeting title/date as the first line of the transcript.
- Keep future summarization as a later requirement.
- Keep Codex work inside project folders under `/Users/edwards/Documents/Git`, not under `/Users/edwards/Documents/Codex` or directly under `/Users/edwards/Documents`.

## Implemented Behavior

CLI options added:

```sh
scribe recording.m4a --title "Sales and CS L10" --date 2026-06-06 --label-speakers
```

Text output with `--title` now defaults to:

```text
Sales-and-CS-L10_2026-06-06_Transcript.md
```

The first line is:

```md
# Sales and CS L10 - 2026-06-06
```

Speaker labeling flow:

1. Diarization and transcription complete.
2. Scribe prints `Press Enter when you are ready to identify speakers.`
3. After Enter, Scribe plays one representative snippet per speaker.
4. User enters the speaker name or leaves it blank to keep `SPEAKER_00`, etc.
5. The final transcript uses the entered names.

The picker script now:

- Opens a file picker.
- Asks for a meeting title.
- Asks for a meeting date when a title is provided.
- Runs Scribe with `--label-speakers`.
- Writes titled transcripts as Markdown.
- Opens/reveals the transcript when complete.
- Closes only its own Apple Terminal tab/window after successful completion.
- Leaves error runs open so failures remain visible.

## Commits

- `c3533d0 feat: add interactive meeting transcript workflow`
  - Fast-forwarded into `main`.
  - Local `main` is ahead of `origin/main` by this commit until pushed.

## Verification

Commands run successfully:

```sh
uv run pytest -q
uv run ruff check
uv run ty check
zsh -n scribe_picker.zsh
uv run scribe --help
```

## Files Changed

- `.gitignore`
- `README.md`
- `scribe.py`
- `scribe_picker.zsh`
- `tests/test_scribe.py`

## Local Artifacts Not Tracked

These were intentionally kept out of Git:

- `.venv/`
- `Armen Stone 2.qta`
- `test-transcript.txt`

Reason: `Armen Stone 2.qta` is a 397 MB QuickTime media file, and `test-transcript.txt` appears to contain real meeting transcript content. Both are local test/input artifacts, not project source.

## Future Summarization Requirement

The user has a ChatGPT chat that creates specific formatted Markdown summary notes for:

- Pasting into EOS software Strety.
- Archiving for future agent/project context.

Future work should treat summarization as a branching workflow after transcript generation. Likely next-step design questions:

- Whether summaries are generated locally, through ChatGPT, or through an OpenAI API workflow.
- Whether the user chooses among outputs such as transcript only, Strety summary, archive note, or both summary formats.
- Where summary files should live and how they should be named.
- Whether speaker labels and meeting title/date metadata should feed the summary prompt directly.

Do not assume the summary format. Recover or request the user's existing summary prompt/chat before implementing that part.
