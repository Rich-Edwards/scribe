#!/bin/zsh

set -euo pipefail

PROJECT_DIR="/Users/edwards/Documents/Git/scribe"
MEETINGS_ROOT="/Users/edwards/Documents/Git/Armen OS/07-meetings/Meeting Transcripts and Summaries"
UV="/opt/homebrew/bin/uv"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

load_project_env() {
  local env_file="$PROJECT_DIR/.env"
  if [[ ! -f "$env_file" ]]; then
    return
  fi

  set -a
  source "$env_file"
  set +a
}

close_own_terminal_window() {
  if [[ "${TERM_PROGRAM:-}" != "Apple_Terminal" || ! -t 0 ]]; then
    return
  fi

  local current_tty
  current_tty=$(basename "$(tty)")

  (
    sleep 0.5
    osascript - "$current_tty" <<'APPLESCRIPT' >/dev/null 2>&1
on run argv
  set targetTTY to item 1 of argv
  tell application "Terminal"
    repeat with terminalWindow in windows
      repeat with terminalTab in tabs of terminalWindow
        if tty of terminalTab is targetTTY then
          if (count of tabs of terminalWindow) is 1 then
            close terminalWindow
          else
            close terminalTab
          end if
          return
        end if
      end repeat
    end repeat
  end tell
end run
APPLESCRIPT
  ) >/dev/null 2>&1 &
}

choose_meeting_type() {
  osascript <<'APPLESCRIPT'
try
  set l10Result to display dialog "Meeting type: is this an L10?" buttons {"Cancel", "No", "L10"} default button "L10" cancel button "Cancel" with icon note
  if button returned of l10Result is "L10" then
    return "L10"
  end if

  set otherResult to display dialog "Choose non-L10 meeting type." buttons {"Cancel", "Other", "Customer"} default button "Customer" cancel button "Cancel" with icon note
  return button returned of otherResult
on error number -128
  return "__SCRIBE_CANCELLED__"
end try
APPLESCRIPT
}

prompt_text() {
  local prompt="$1"
  local default_answer="$2"
  osascript - "$prompt" "$default_answer" <<'APPLESCRIPT'
on run argv
  set promptText to item 1 of argv
  set defaultAnswer to item 2 of argv
  try
    set dialogResult to display dialog promptText default answer defaultAnswer buttons {"Cancel", "Continue"} default button "Continue"
    return text returned of dialogResult
  on error number -128
    return "__SCRIBE_CANCELLED__"
  end try
end run
APPLESCRIPT
}

review_summary_action() {
  osascript <<'APPLESCRIPT'
try
  set dialogResult to display dialog "Review the Markdown summary. If this is an L10, paste/export it to Strety before finalizing." buttons {"Cancel", "Retry Summary", "Finalize"} default button "Finalize" cancel button "Cancel" with icon note
  return button returned of dialogResult
on error number -128
  return "Cancel"
end try
APPLESCRIPT
}

confirm_strety_ready() {
  osascript <<'APPLESCRIPT'
try
  set dialogResult to display dialog "Confirm Strety handoff is complete or intentionally skipped. Finalize will file transcript + summary and move leftovers to Trash." buttons {"Cancel", "Finalize"} default button "Finalize" cancel button "Cancel" with icon caution
  return button returned of dialogResult
on error number -128
  return "Cancel"
end try
APPLESCRIPT
}

stop_for_destination_collision() {
  local existing_path="$1"
  osascript - "$existing_path" <<'APPLESCRIPT'
on run argv
  set existingPath to item 1 of argv
  display dialog "A filed meeting artifact already exists. Choose a different title/date or remove the existing file before rerunning." & return & return & existingPath buttons {"OK"} default button "OK" with icon stop
end run
APPLESCRIPT
}

echo "Scribe meeting workflow launcher"
echo "Project: $PROJECT_DIR"
echo "Meetings: $MEETINGS_ROOT"
echo ""

if [[ ! -d "$PROJECT_DIR" ]]; then
  osascript -e 'display dialog "Scribe project folder not found." buttons {"OK"} default button "OK" with icon stop'
  exit 1
fi

if [[ ! -d "$MEETINGS_ROOT" ]]; then
  osascript -e 'display dialog "Meetings folder not found. Fix the configured Armen OS meetings path before continuing." buttons {"OK"} default button "OK" with icon stop'
  exit 1
fi

if [[ ! -x "$UV" ]]; then
  osascript -e 'display dialog "uv was not found at /opt/homebrew/bin/uv." buttons {"OK"} default button "OK" with icon stop'
  exit 1
fi

load_project_env

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  osascript -e 'display dialog "OPENAI_API_KEY is required for summary generation. Set it before running this workflow." buttons {"OK"} default button "OK" with icon stop'
  exit 1
fi

INPUT_FILE=$(osascript <<'APPLESCRIPT'
try
  set chosenFile to choose file with prompt "Choose an audio or video file to transcribe with Scribe:"
  return POSIX path of chosenFile
on error number -128
  return "__SCRIBE_CANCELLED__"
end try
APPLESCRIPT
)

if [[ "$INPUT_FILE" == "__SCRIBE_CANCELLED__" || -z "$INPUT_FILE" ]]; then
  echo "No file selected."
  exit 0
fi

INPUT_DIR=$(dirname "$INPUT_FILE")
FILENAME=$(basename "$INPUT_FILE")
BASENAME="${FILENAME%.*}"

MEETING_TITLE=$(prompt_text "Meeting title?" "$BASENAME")
if [[ "$MEETING_TITLE" == "__SCRIBE_CANCELLED__" ]]; then
  echo "Cancelled."
  exit 0
fi
MEETING_TITLE="${MEETING_TITLE#"${MEETING_TITLE%%[![:space:]]*}"}"
MEETING_TITLE="${MEETING_TITLE%"${MEETING_TITLE##*[![:space:]]}"}"
if [[ -z "$MEETING_TITLE" ]]; then
  MEETING_TITLE="$BASENAME"
fi

MEETING_DATE=$(prompt_text "Meeting date?" "$(date +%F)")
if [[ "$MEETING_DATE" == "__SCRIBE_CANCELLED__" ]]; then
  echo "Cancelled."
  exit 0
fi

MEETING_TYPE=$(choose_meeting_type)
if [[ "$MEETING_TYPE" == "__SCRIBE_CANCELLED__" ]]; then
  echo "Cancelled."
  exit 0
fi

SAFE_TITLE=$(printf '%s' "$MEETING_TITLE" | sed -E 's/[^A-Za-z0-9]+/-/g; s/^-+//; s/-+$//')
if [[ -z "$SAFE_TITLE" ]]; then
  SAFE_TITLE="Meeting"
fi

OUTPUT_FILE="$INPUT_DIR/${SAFE_TITLE}_${MEETING_DATE}_Transcript.md"
SUMMARY_FILE="$INPUT_DIR/${SAFE_TITLE}_${MEETING_DATE}_Summary.md"
LOG_FILE="$INPUT_DIR/$BASENAME.scribe.log"
DEST_DIR="$MEETINGS_ROOT/$MEETING_TYPE"
FINAL_TRANSCRIPT="$DEST_DIR/$(basename "$OUTPUT_FILE")"
FINAL_SUMMARY="$DEST_DIR/$(basename "$SUMMARY_FILE")"

for existing_destination in "$FINAL_TRANSCRIPT" "$FINAL_SUMMARY"; do
  if [[ -e "$existing_destination" ]]; then
    echo "Destination already exists: $existing_destination"
    stop_for_destination_collision "$existing_destination"
    exit 1
  fi
done

echo "Input:      $INPUT_FILE"
echo "Transcript: $OUTPUT_FILE"
echo "Summary:    $SUMMARY_FILE"
echo "Type:       $MEETING_TYPE"
echo "Log:        $LOG_FILE"
echo ""

cd "$PROJECT_DIR"

{
  echo "Started: $(date)"
  echo "Input: $INPUT_FILE"
  echo "Transcript: $OUTPUT_FILE"
  echo "Summary: $SUMMARY_FILE"
  echo "Meeting type: $MEETING_TYPE"
  echo ""
  "$UV" run scribe "$INPUT_FILE" --label-speakers --title "$MEETING_TITLE" --date "$MEETING_DATE" -o "$OUTPUT_FILE"
  echo ""
  echo "Transcript finished: $(date)"
  "$UV" run scribe "$OUTPUT_FILE" --generate-summary --title "$MEETING_TITLE" --date "$MEETING_DATE" --meeting-type "$MEETING_TYPE"
  echo ""
  echo "Summary finished: $(date)"
} 2>&1 | tee "$LOG_FILE"

open -R "$SUMMARY_FILE"
open "$SUMMARY_FILE"

while true; do
  ACTION=$(review_summary_action)
  case "$ACTION" in
    "Cancel")
      echo "Cancelled after summary review. Files were left in place:"
      echo "Transcript: $OUTPUT_FILE"
      echo "Summary: $SUMMARY_FILE"
      exit 0
      ;;
    "Retry Summary")
      REVISION_NOTE=$(prompt_text "What should change in the regenerated summary?" "")
      if [[ "$REVISION_NOTE" == "__SCRIBE_CANCELLED__" ]]; then
        continue
      fi
      echo ""
      echo "Regenerating summary..."
      "$UV" run scribe "$OUTPUT_FILE" --generate-summary --title "$MEETING_TITLE" --date "$MEETING_DATE" --meeting-type "$MEETING_TYPE" --revision-note "$REVISION_NOTE" 2>&1 | tee -a "$LOG_FILE"
      open "$SUMMARY_FILE"
      ;;
    "Finalize")
      if [[ "$(confirm_strety_ready)" != "Finalize" ]]; then
        continue
      fi
      echo ""
      echo "Finalizing meeting artifacts..."
      "$UV" run scribe "$INPUT_FILE" --finalize-meeting --meeting-type "$MEETING_TYPE" --transcript "$OUTPUT_FILE" --summary "$SUMMARY_FILE" --log-file "$LOG_FILE"
      open -R "$FINAL_SUMMARY"
      osascript -e 'display notification "Transcript and summary filed. Leftover files moved to Trash." with title "Scribe meeting workflow complete"'
      echo ""
      echo "Done."
      echo "Transcript: $FINAL_TRANSCRIPT"
      echo "Summary: $FINAL_SUMMARY"
      close_own_terminal_window
      exit 0
      ;;
  esac
done
