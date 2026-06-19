#!/usr/bin/env zsh

TERMINAL_ADAPTERS_DIR="${SCRIPT_DIR:-$(cd "$(dirname "$0")" && pwd)}/terminal-adapters"

normalize_terminal_backend() {
  local backend="${1:l}"

  case "$backend" in
    terminal|terminal-app|terminal.app)
      echo "terminal-app"
      ;;
    windows|windows-terminal|wt)
      echo "windows-terminal"
      ;;
    wezterm|wez)
      echo "wezterm"
      ;;
    none|current|fallback)
      echo "none"
      ;;
    *)
      echo "$backend"
      ;;
  esac
}

detect_terminal_backend() {
  if [[ -n "${Kiln_TERMINAL:-}" ]]; then
    normalize_terminal_backend "$Kiln_TERMINAL"
    return
  fi

  # WezTerm sets $WEZTERM_PANE when running inside it
  if [[ -n "${WEZTERM_PANE:-}" ]] && has_command wezterm; then
    echo "wezterm"
    return
  fi

  if has_command osascript; then
    echo "terminal-app"
    return
  fi

  if has_command wt.exe; then
    echo "windows-terminal"
    return
  fi

  echo "none"
}

load_terminal_backend() {
  local backend="$1"
  local adapter_file="$TERMINAL_ADAPTERS_DIR/$backend.sh"

  if [[ ! -r "$adapter_file" ]]; then
    echo "Unknown terminal backend '$backend'. Expected adapter file: $adapter_file" >&2
    return 1
  fi

  source "$adapter_file"
}

