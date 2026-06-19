#!/usr/bin/env zsh
# WezTerm adapter for Kiln (Unix)
# Requires: wezterm CLI in PATH
# Detection: $WEZTERM_PANE is set when running inside WezTerm, or Kiln_TERMINAL=wezterm

terminal_backend_label() {
  echo "WezTerm"
}

terminal_backend_can_open_sessions() {
  return 0
}

terminal_backend_tracks_windows() {
  # wezterm cli list returns pane IDs — we can track them
  return 0
}

terminal_window_exists() {
  local pane_id="$1"
  [[ -n "$pane_id" ]] || return 1

  wezterm cli list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$pane_id"
}

terminal_open_session() {
  local session="$1"
  local title="$2"
  local sibling_id="${3:-}"

  local cmd="cd $(printf '%q' "$WORKING_DIR") && exec tmux -S $(printf '%q' "$TMUX_SOCKET") attach-session -t $(printf '%q' "$session")"

  local pane_id
  if [[ -n "$sibling_id" ]]; then
    # Open tab in the same WezTerm window as the sibling pane
    pane_id="$(wezterm cli spawn --pane-id "$sibling_id" -- zsh -lc "$cmd" 2>/dev/null)"
  else
    # Open a new tab in the current/front window, or a new window if none
    pane_id="$(wezterm cli spawn -- zsh -lc "$cmd" 2>/dev/null)"
  fi

  if [[ -n "$pane_id" ]]; then
    wezterm cli set-tab-title --pane-id "$pane_id" "$title" 2>/dev/null || true
    echo "$pane_id"
  fi
}

terminal_close_window() {
  local pane_id="$1"
  [[ -n "$pane_id" ]] || return 0

  wezterm cli kill-pane --pane-id "$pane_id" 2>/dev/null || true
}

