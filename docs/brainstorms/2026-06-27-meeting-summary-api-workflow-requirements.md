---
date: 2026-06-27
topic: meeting-summary-api-workflow
---

# Meeting Summary API Workflow

## Problem Frame
The current meeting workflow produces a usable transcript with named speakers, but the summary step still depends on manual work inside ChatGPT and manual file cleanup afterward. The next version should keep the existing summary quality and review flow while automating transcript-to-summary generation, final filing into the Armen OS meetings structure, and cleanup after approval. Existing transcript and summary examples already live in the Armen OS meetings folder and should be treated as the bounded reference set for output shape and filing behavior.

## Requirements

**Workflow Entry and Routing**
- R1. The workflow starts from a voice memo or another recording file already supported by the existing transcription flow.
- R2. The workflow must collect the meeting metadata needed to generate and file the transcript and summary, including meeting title, meeting date, and meeting type, through explicit user confirmation rather than inferred classification.
- R3. Meeting type must route the finalized transcript and summary into one of the existing Armen OS subfolders under `07-meetings/Meeting Transcripts and Summaries`: `L10`, `Customer`, or `Other`.
- R4. The workflow must treat `/Users/edwards/Documents/Git/Armen OS/07-meetings/Meeting Transcripts and Summaries` as the fixed required destination root for this version.
- R5. If that root or the selected subfolder is unavailable, the workflow must stop with an explicit error instead of guessing a fallback location.

**Transcript and Summary Generation**
- R6. The workflow must continue producing a transcript with named speakers before summary generation begins.
- R7. The workflow must generate exactly one summary markdown file from the transcript.
- R8. V1 must use one shared summary contract across all meeting types rather than separate summary templates by type.
- R9. The shared summary contract must be bounded by these canonical examples:
  - `L10`: `Sales-and-CS-L10_2026-06-08_Summary.md`
  - `Customer`: `Numa-Armen Stone Monthly Review - 6.5.26.txt`
  - `Other`: `1x1-with-Caroline-Massey_2026-06-17_Summary_and_Transcript.md`
- R10. Summary generation must use those files as acceptance references for tone, structure, and level of detail, but not require exact wording replication.
- R11. The generated summary must be usable in the current Armen OS review-and-Strety workflow without material rewriting beyond normal user review edits.
- R12. Summary generation must use an API-based flow rather than depend on the ChatGPT web UI as the primary path.
- R13. V1 must use a single OpenAI-backed summary integration with repo-local configuration by environment variables rather than introducing multiple provider paths.

**Review and Retry**
- R14. The workflow must prompt the user to identify speakers during transcription, then generate the labeled transcript before starting summary generation.
- R15. After the summary file is generated, the workflow must attempt to open that markdown file for review in the user's normal markdown editor workflow, which is currently Byword.
- R16. If opening the summary file fails, the workflow must show an explicit error and leave all artifacts untouched in their current location.
- R17. After a successful open, the workflow must wait for explicit user confirmation before any filing or trash operations are allowed.
- R18. The review confirmation step must present three actions: `Finalize`, `Retry Summary`, and `Cancel`.
- R19. `Retry Summary` must prompt for a short revision note and regenerate the summary using the same transcript plus that note.
- R20. `Retry Summary` must replace the existing summary file in place rather than create numbered draft variants, then reopen the updated summary for review.
- R21. `Cancel`, editor dismissal, or review-dialog dismissal must exit the workflow without moving or trashing any files.
- R22. `Finalize` must require explicit user confirmation that Strety handoff is complete or intentionally skipped for this meeting before filing and trash can proceed.

**Finalize and Cleanup**
- R23. `Finalize` must move both the transcript and summary into the selected meeting-type folder.
- R24. `Finalize` must send the original recording file to the macOS Trash after the user has reviewed the summary.
- R25. `Finalize` must send only the defined transient artifacts to the macOS Trash so that only the transcript and summary remain as durable outputs in the final meeting folder.
- R26. `Finalize` must sequence move and trash operations so that the recording is not trashed unless the transcript and summary have already been filed successfully.
- R27. If file move, trash, or overwrite operations fail during `Finalize`, the workflow must stop with an explicit error and must not continue to later cleanup steps.
- R28. If transcript generation is empty, summary generation returns empty content, API authentication is missing, API/network/server response fails, or the API response is malformed, the workflow must show an explicit error, preserve existing files, and disable `Finalize` until a valid summary exists.
- R29. The artifact lifecycle for v1 must be:
  - Source recording: selected at start, survives review and retry, trashed only on successful `Finalize`
  - Transcript file: created before summary generation, survives review and retry, moved on successful `Finalize`, preserved on `Cancel`
  - Summary file: created after API summary generation, overwritten on retry, moved on successful `Finalize`, preserved on `Cancel`
  - Log file (`*.scribe.log`): created during the run, preserved on `Cancel`, trashed on successful `Finalize`
  - Normalized temp WAV and other temporary process files: removed automatically during processing or treated as transient artifacts that never become durable outputs
  - Any API scratch or retry-local state files: preserved only as long as needed for the current run, preserved on `Cancel` if materialized, trashed on successful `Finalize`

## Success Criteria
- The user can start from a recording file and reach a reviewable summary without using the ChatGPT web UI.
- The user can review the summary in their normal markdown-editor workflow, paste it into Strety when needed, and then choose whether to finalize, retry, or cancel.
- Finalization leaves the correct meeting-type folder with exactly the transcript and summary as the durable artifacts.
- The workflow never trashes the source recording before the user explicitly finalizes.
- The generated summary is trusted enough to use in the current Armen OS review-and-Strety workflow without material rewriting beyond normal review edits.
- The generated summary and transcript are recognizably aligned with the canonical examples listed in `R9`.

## Scope Boundaries
- The workflow does not write directly into Strety in this version.
- The workflow does not generate multiple summary variants or separate archive and Strety outputs.
- The workflow does not preserve retry-history files for summaries in this version.
- The workflow does not attempt silent or automatic cleanup before the user review gate.
- The workflow does not infer meeting type automatically in this version.
- The workflow does not attempt fallback filing locations if the expected Armen OS meetings root is unavailable.
- The workflow does not implement separate meeting-type-specific summary templates in v1.
- The workflow does not support multiple summary providers or non-OpenAI fallback providers in v1.

## Key Decisions
- API-first summary generation: the goal is to remove ChatGPT UI dependency from the core workflow.
- Single summary artifact: the current user workflow only needs one summary markdown file.
- Existing filed examples are the baseline: current transcript and summary pairs in the Armen OS meetings folder replace the earlier dependency on recovering a specific ChatGPT chat artifact.
- One shared v1 summary shape: routing still varies by meeting type, but summary generation is bounded to one shared contract for the first implementation.
- Manual Strety handoff remains: the user still exports/pastes the reviewed summary into Strety before finalizing because the note must be attached during the live meeting workflow and direct Strety write automation is not part of this version.
- Explicit post-review confirmation remains required: the workflow must wait for a user decision after opening the summary, but the exact confirmation mechanism can be chosen during planning as long as it preserves `Finalize`, `Retry Summary`, and `Cancel`.
- In-place retries: replacing the summary file avoids clutter in the pre-filing workspace.
- Trash is the safety model: source recordings and transient files are moved to Trash rather than permanently deleted.

## Dependencies / Assumptions
- The Armen OS meetings folder structure remains `L10`, `Customer`, and `Other`.
- The Armen OS meetings folder path on this machine is stable and intentionally fixed for this workflow.
- The user continues to perform the final Strety paste manually during review.
- Existing filed summaries and transcripts in the Armen OS meetings folder provide enough examples to define the target output shape for v1.
- OpenAI credentials and model settings will be provided through local environment configuration on this machine.

## Outstanding Questions

### Deferred to Planning
- [Affects R13][Technical] Which OpenAI API surface and model should back summary generation for the best balance of output quality, latency, and implementation simplicity?
- [Affects R15][Technical] Which concrete `open` and confirmation mechanism is most reliable on this machine while preserving the required review-state behavior?
- [Affects R28][Technical] How should API failure details and retry affordances be surfaced to the user in the picker flow?

## Next Steps
-> /ce:plan for structured implementation planning
