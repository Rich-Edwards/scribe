#!/bin/zsh

set -euo pipefail

PROJECT_DIR="/Users/edwards/Documents/Git/scribe"

UV="/opt/homebrew/bin/uv"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

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

          close terminalTab

          return

        end if

      end repeat

    end repeat

  end tell

end run

APPLESCRIPT

  ) >/dev/null 2>&1 &

}

echo "Scribe transcription launcher"

echo "Project: $PROJECT_DIR"

echo ""

if [[ ! -d "$PROJECT_DIR" ]]; then

  osascript -e 'display dialog "Scribe project folder not found." buttons {"OK"} default button "OK" with icon stop'

  exit 1

fi

if [[ ! -x "$UV" ]]; then

  osascript -e 'display dialog "uv was not found at /opt/homebrew/bin/uv." buttons {"OK"} default button "OK" with icon stop'

  exit 1

fi

INPUT_FILE=$(osascript <<'APPLESCRIPT'

set chosenFile to choose file with prompt "Choose an audio or video file to transcribe with Scribe:"

return POSIX path of chosenFile

APPLESCRIPT

)

if [[ -z "$INPUT_FILE" ]]; then

  echo "No file selected."

  exit 0

fi

INPUT_DIR=$(dirname "$INPUT_FILE")

FILENAME=$(basename "$INPUT_FILE")

BASENAME="${FILENAME%.*}"

MEETING_TITLE=$(osascript <<'APPLESCRIPT'

try

  set dialogResult to display dialog "Meeting title? Leave blank to use the source filename." default answer "" buttons {"Cancel", "Continue"} default button "Continue"

  return text returned of dialogResult

on error number -128

  return "__SCRIBE_CANCELLED__"

end try

APPLESCRIPT

)

if [[ "$MEETING_TITLE" == "__SCRIBE_CANCELLED__" ]]; then

  echo "Cancelled."

  exit 0

fi

SCRIBE_ARGS=("$INPUT_FILE" "--label-speakers")

if [[ -n "$MEETING_TITLE" ]]; then

  MEETING_DATE=$(osascript <<'APPLESCRIPT'

try

  set defaultDate to do shell script "date +%F"

  set dialogResult to display dialog "Meeting date?" default answer defaultDate buttons {"Cancel", "Continue"} default button "Continue"

  return text returned of dialogResult

on error number -128

  return "__SCRIBE_CANCELLED__"

end try

APPLESCRIPT

)

  if [[ "$MEETING_DATE" == "__SCRIBE_CANCELLED__" ]]; then

    echo "Cancelled."

    exit 0

  fi

  SAFE_TITLE=$(printf '%s' "$MEETING_TITLE" | sed -E 's/[^A-Za-z0-9]+/-/g; s/^-+//; s/-+$//')

  if [[ -z "$SAFE_TITLE" ]]; then

    SAFE_TITLE="Meeting"

  fi

  OUTPUT_FILE="$INPUT_DIR/${SAFE_TITLE}_${MEETING_DATE}_Transcript.md"

  SCRIBE_ARGS+=("--title" "$MEETING_TITLE" "--date" "$MEETING_DATE")

else

  OUTPUT_FILE="$INPUT_DIR/$BASENAME.scribe.txt"

fi

SCRIBE_ARGS+=("-o" "$OUTPUT_FILE")

LOG_FILE="$INPUT_DIR/$BASENAME.scribe.log"

echo "Input:  $INPUT_FILE"

echo "Output: $OUTPUT_FILE"

echo "Log:    $LOG_FILE"

echo ""

echo "Starting transcription..."

echo "First run may take longer because models may download."

echo ""

cd "$PROJECT_DIR"

{

  echo "Started: $(date)"

  echo "Input: $INPUT_FILE"

  echo "Output: $OUTPUT_FILE"

  echo ""

  "$UV" run scribe "${SCRIBE_ARGS[@]}"

  echo ""

  echo "Finished: $(date)"

} 2>&1 | tee "$LOG_FILE"

open -R "$OUTPUT_FILE"

open "$OUTPUT_FILE"

osascript -e 'display notification "Transcript written beside the source file." with title "Scribe transcription complete"'

echo ""

echo "Done."

echo "Transcript: $OUTPUT_FILE"

close_own_terminal_window
