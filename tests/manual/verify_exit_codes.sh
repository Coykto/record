#!/usr/bin/env bash
# Verify CLI exit codes for Slice 6 of the
# "Mixed Mic + System Audio Capture" spec (tech spec §2.4).
#
# This script walks through the automatable verification scenarios from
# context/spec/001-mixed-mic-system-audio-capture/tasks.md.
# Scenario 4 (permission denial) requires manual interaction with
# System Settings and is documented at the bottom.
#
# Usage:
#   ./tests/manual/verify_exit_codes.sh
#
# Re-runnable: cleans up before AND after each scenario.

set -u

# ---------------------------------------------------------------------------
# Paths & globals
# ---------------------------------------------------------------------------

PID_FILE="$HOME/Library/Application Support/record/capture.pid"
STATE_FILE="$HOME/Library/Application Support/record/capture-state.json"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BINARY="$REPO_ROOT/src/record/bin/record-capture"
BINARY_BACKUP="$REPO_ROOT/src/record/bin/record-capture.bak"

TOTAL=0
PASSED=0
FAILED=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

cleanup() {
  # Best-effort: stop any active capture and remove leftover state.
  record stop >/dev/null 2>&1 || true
  # If a PID file points at a still-alive supervisor (e.g. after kill -9 of
  # the swift binary), kill the supervisor too.
  if [ -f "$PID_FILE" ]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
      sleep 1
      kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$PID_FILE" "$STATE_FILE"
  # If we left a backup of the binary, restore it.
  if [ -f "$BINARY_BACKUP" ] && [ ! -f "$BINARY" ]; then
    mv "$BINARY_BACKUP" "$BINARY"
  fi
}

assert_exit() {
  # assert_exit <expected> <actual> <label>
  local expected="$1"
  local actual="$2"
  local label="$3"
  TOTAL=$((TOTAL + 1))
  if [ "$expected" = "$actual" ]; then
    PASSED=$((PASSED + 1))
    echo "  PASS: $label (exit $actual)"
  else
    FAILED=$((FAILED + 1))
    echo "  FAIL: $label (expected $expected, got $actual)"
  fi
}

header() {
  echo ""
  echo "=========================================================="
  echo "$1"
  echo "=========================================================="
}

# Trap so unexpected exits still tidy up.
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Pre-flight: clean slate.
# ---------------------------------------------------------------------------

header "Pre-flight cleanup"
cleanup
echo "  cleaned existing PID/state files"

# Confirm `record` is on PATH; otherwise none of the scenarios are runnable.
if ! command -v record >/dev/null 2>&1; then
  echo "FATAL: 'record' CLI not on PATH. Run 'make install' first." >&2
  exit 99
fi

# ---------------------------------------------------------------------------
# Scenario 1: `record start` twice in a row -> second exits 1
# ---------------------------------------------------------------------------

header "Scenario 1: double start -> second exits 1"

echo "  starting first capture..."
record start >/dev/null 2>&1
first_exit=$?
echo "  first start exit: $first_exit"
assert_exit 0 "$first_exit" "first start succeeds"

# Give the supervisor a moment to fully come up.
sleep 1

echo "  starting second capture (should be rejected)..."
second_output="$(record start 2>&1)"
second_exit=$?
echo "  second start exit: $second_exit"
echo "  second start output: $second_output"
assert_exit 1 "$second_exit" "second start exits 1"

if echo "$second_output" | grep -qi "capture already in progress"; then
  PASSED=$((PASSED + 1))
  TOTAL=$((TOTAL + 1))
  echo "  PASS: message contains 'capture already in progress'"
else
  FAILED=$((FAILED + 1))
  TOTAL=$((TOTAL + 1))
  echo "  FAIL: message did not contain 'capture already in progress'"
fi

# Clean up after scenario.
record stop >/dev/null 2>&1 || true
sleep 1
cleanup

# ---------------------------------------------------------------------------
# Scenario 2: kill -9 supervisor PID, then `record stop` -> exits 1
# ---------------------------------------------------------------------------

header "Scenario 2: stale PID file -> stop exits 1"

echo "  starting capture..."
record start >/dev/null 2>&1
sleep 1

if [ ! -f "$PID_FILE" ]; then
  echo "  FAIL: PID file not created"
  FAILED=$((FAILED + 1))
  TOTAL=$((TOTAL + 1))
else
  supervisor_pid="$(cat "$PID_FILE")"
  echo "  supervisor PID: $supervisor_pid"
  echo "  kill -9 $supervisor_pid"
  kill -9 "$supervisor_pid" 2>/dev/null || true
  sleep 1

  if kill -0 "$supervisor_pid" 2>/dev/null; then
    echo "  WARN: supervisor still alive after kill -9; skipping rest of scenario"
  else
    echo "  supervisor confirmed dead; running record stop..."
    stop_output="$(record stop 2>&1)"
    stop_exit=$?
    echo "  stop exit: $stop_exit"
    echo "  stop output: $stop_output"
    assert_exit 1 "$stop_exit" "stop on stale PID exits 1"

    if echo "$stop_output" | grep -q "no capture running"; then
      PASSED=$((PASSED + 1))
      TOTAL=$((TOTAL + 1))
      echo "  PASS: message starts with 'no capture running'"
    else
      FAILED=$((FAILED + 1))
      TOTAL=$((TOTAL + 1))
      echo "  FAIL: message did not contain 'no capture running'"
    fi
  fi
fi

cleanup

# ---------------------------------------------------------------------------
# Scenario 3: move bundled binary -> `record start` exits 3
# ---------------------------------------------------------------------------

header "Scenario 3: binary missing -> start exits 3"

if [ ! -f "$BINARY" ]; then
  echo "  WARN: binary $BINARY not found before scenario; skipping"
else
  echo "  moving $BINARY out of the way..."
  mv "$BINARY" "$BINARY_BACKUP"

  start_output="$(record start 2>&1)"
  start_exit=$?
  echo "  start exit: $start_exit"
  echo "  start output: $start_output"
  assert_exit 3 "$start_exit" "start with missing binary exits 3"

  if echo "$start_output" | grep -q "make install"; then
    PASSED=$((PASSED + 1))
    TOTAL=$((TOTAL + 1))
    echo "  PASS: message mentions 'make install'"
  else
    FAILED=$((FAILED + 1))
    TOTAL=$((TOTAL + 1))
    echo "  FAIL: message did not mention 'make install'"
  fi

  echo "  restoring binary..."
  mv "$BINARY_BACKUP" "$BINARY"
fi

cleanup

# ---------------------------------------------------------------------------
# Scenario 5: `record stop` with no PID file -> exits 1
# (This is the no-PID-file variant of sub-task 3, complementing scenario 2.)
# ---------------------------------------------------------------------------

header "Scenario 5: stop with no PID file -> exits 1"

rm -f "$PID_FILE" "$STATE_FILE"
stop_output="$(record stop 2>&1)"
stop_exit=$?
echo "  stop exit: $stop_exit"
echo "  stop output: $stop_output"
assert_exit 1 "$stop_exit" "stop with no PID file exits 1"

if echo "$stop_output" | grep -q "no capture running"; then
  PASSED=$((PASSED + 1))
  TOTAL=$((TOTAL + 1))
  echo "  PASS: message contains 'no capture running'"
else
  FAILED=$((FAILED + 1))
  TOTAL=$((TOTAL + 1))
  echo "  FAIL: message did not contain 'no capture running'"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

header "Summary"
echo "  total checks: $TOTAL"
echo "  passed:       $PASSED"
echo "  failed:       $FAILED"

# ---------------------------------------------------------------------------
# MANUAL: Scenario 4 — permission denied -> exit 2
# ---------------------------------------------------------------------------
#
# This scenario cannot be scripted because revoking macOS TCC permissions
# requires interactive UI in System Settings (no public CLI exists).
#
# Steps:
#   1. Open System Settings -> Privacy & Security -> Microphone (or
#      Screen & System Audio Recording) and revoke access for your
#      terminal app (or for the bundled `record-capture` binary).
#   2. Run: record start
#   3. Within ~3 s the CLI should exit with status 2 and print a message like:
#        "microphone permission denied — grant access in System Settings →
#         Privacy & Security → Microphone"
#      or the screen-recording equivalent.
#   4. Confirm with: echo $?  (should print 2)
#   5. Confirm that ~/Library/Application Support/record/capture.pid does NOT
#      exist after the failed start (the CLI cleans up).
#   6. Re-grant access in System Settings to restore the happy path.
#
# Repeat with `record stop` after a permission_denied event lands in
# capture-state.json (e.g. start a capture during the brief window the
# permission is denied and let the supervisor mark the state): `record stop`
# should likewise exit 2 with the same System Settings message.
#
# ---------------------------------------------------------------------------

if [ "$FAILED" -gt 0 ]; then
  exit 1
fi
exit 0
