#!/bin/bash
# Profile Loader Module for Kiln
# Loads YAML profiles and extracts terminal/agent configuration

set -euo pipefail

# Load a Kiln configuration profile
load_kiln_profile() {
    local project_root="$1"
    local profile_name="${2:-dev}"
    local config_path="${3:-}"

    # Find profiles.yaml if not provided
    if [[ -z "$config_path" ]]; then
        local search_paths=(
            "$project_root/kiln.profiles.yaml"
            "$project_root/kiln/profiles.yaml"
            "$project_root/.kiln/profiles.yaml"
            "$HOME/.kiln/profiles.yaml"
            "/etc/kiln/profiles.yaml"
        )

        for path in "${search_paths[@]}"; do
            if [[ -f "$path" ]]; then
                config_path="$path"
                echo "Found profiles.yaml at: $config_path" >&2
                break
            fi
        done

        if [[ -z "$config_path" ]]; then
            echo "ERROR: Could not find profiles.yaml" >&2
            return 1
        fi
    fi

    # Parse YAML and load profile
    _parse_yaml_profile "$config_path" "$profile_name"
}

# Parse YAML and extract profile data
_parse_yaml_profile() {
    local config_path="$1"
    local profile_name="$2"

    # Use Python for robust YAML parsing
    python3 << PYTHON_PROFILE_LOADER
import yaml
import json
import sys

try:
    with open('$config_path', 'r') as f:
        config = yaml.safe_load(f)

    if 'profiles' not in config or '$profile_name' not in config['profiles']:
        available = list(config.get('profiles', {}).keys())
        print(f"ERROR: Profile '$profile_name' not found. Available: {', '.join(available)}", file=sys.stderr)
        sys.exit(1)

    profile = config['profiles']['$profile_name']
    profile_data = {
        'name': '$profile_name',
        'description': profile.get('description', ''),
        'terminals': profile.get('terminals', []),
        'messageBackend': profile.get('messageBackend', 'sqlite'),
        'logLevel': profile.get('logLevel', 'info'),
        'env': profile.get('env', {})
    }

    # Output as bash-compatible variables
    print(f"PROFILE_NAME='{profile_data['name']}'")
    print(f"PROFILE_DESCRIPTION='{profile_data['description']}'")
    print(f"PROFILE_BACKEND='{profile_data['messageBackend']}'")
    print(f"PROFILE_LOG_LEVEL='{profile_data['logLevel']}'")

    # Output terminals as array
    for i, terminal in enumerate(profile_data['terminals']):
        role = terminal.get('role', '')
        agent = terminal.get('agent', 'claude')
        worktree = terminal.get('worktree', '@current')
        title = terminal.get('title', f'kiln {role}')
        print(f"TERMINAL_{i}_ROLE='{role}'")
        print(f"TERMINAL_{i}_AGENT='{agent}'")
        print(f"TERMINAL_{i}_WORKTREE='{worktree}'")
        print(f"TERMINAL_{i}_TITLE='{title}'")

    print(f"TERMINAL_COUNT={len(profile_data['terminals'])}")

    # Output environment variables
    for key, value in profile_data['env'].items():
        print(f"PROFILE_ENV_{key}='{value}'")

except Exception as e:
    print(f"ERROR: Failed to parse profile: {e}", file=sys.stderr)
    sys.exit(1)
PYTHON_PROFILE_LOADER
}

# List all available profiles
get_available_profiles() {
    local project_root="$1"
    local config_path="$project_root/kiln/kiln.profiles.yaml"

    if [[ ! -f "$config_path" ]]; then
        return
    fi

    python3 << PYTHON_LIST_PROFILES
import yaml
import sys

try:
    with open('$config_path', 'r') as f:
        config = yaml.safe_load(f)

    profiles = config.get('profiles', {})
    for name, profile in profiles.items():
        desc = profile.get('description', '')
        print(f"{name}: {desc}")

except Exception as e:
    print(f"ERROR: Failed to list profiles: {e}", file=sys.stderr, flush=True)
    sys.exit(1)
PYTHON_LIST_PROFILES
}

# Export functions for sourcing
export -f load_kiln_profile
export -f get_available_profiles

