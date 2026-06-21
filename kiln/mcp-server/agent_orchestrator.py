"""
agent_orchestrator.py - Orchestrates agent invocations based on channel messages.

Monitors the channel for incoming messages and spawns agents when they have work.
Each agent runs as an independent Claude Code invocation and communicates via
the shared channel (SQLite + OS file-system watching).

Usage (from PowerShell):
    python kiln/mcp-server/agent_orchestrator.py <db_path> <role> <claude_code_cmd>

Example:
    python kiln/mcp-server/agent_orchestrator.py \
        .kiln/messages.db \
        coder \
        'claude code --mcp-config .mcp.json'
"""

import sys
import json
import subprocess
import threading
from pathlib import Path
from channel import Channel, Message

def run_agent(role: str, claude_cmd: str, msg: Message):
    """Run a Claude Code agent to process a message."""
    # Pass the message ID to the agent so it knows what to process
    msg_content = msg.payload.get("content", "")

    prompt = f"""
You received a message from {msg.sender}:

{msg_content}

Process this message and respond appropriately using the send_message tool if needed.
The message ID for marking as complete is: {msg.id}
"""

    cmd = f'{claude_cmd} --prompt "{prompt}"'
    print(f"[Orchestrator] Spawning {role}: {cmd}")

    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            print(f"[Orchestrator] {role} completed successfully")
        else:
            print(f"[Orchestrator] {role} exited with code {result.returncode}")
            if result.stderr:
                print(f"  Error: {result.stderr}")
    except subprocess.TimeoutExpired:
        print(f"[Orchestrator] {role} timed out (5 min limit)")
    except Exception as e:
        print(f"[Orchestrator] Failed to run {role}: {e}")

def main():
    if len(sys.argv) < 4:
        print("Usage: agent_orchestrator.py <db_path> <role> <claude_code_cmd>")
        sys.exit(1)

    db_path = sys.argv[1]
    role = sys.argv[2]
    claude_cmd = sys.argv[3]

    print(f"[Orchestrator] Starting for role: {role}")
    print(f"[Orchestrator] Database: {db_path}")

    if not Path(db_path).exists():
        print(f"[Orchestrator] Error: Database not found: {db_path}")
        sys.exit(1)

    ch = Channel(db_path)

    def handle_message(msg: Message):
        """Called when a message arrives for this role."""
        print(f"[Orchestrator] Message arrived for {role}: id={msg.id[:8]} from {msg.sender}")
        # Spawn agent in background so watcher thread doesn't block
        thread = threading.Thread(
            target=run_agent,
            args=(role, claude_cmd, msg),
            daemon=False
        )
        thread.start()

    print(f"[Orchestrator] Subscribing to channel for role: {role}")
    ch.subscribe(role, handle_message)

    print(f"[Orchestrator] Listening for messages... (press Ctrl+C to stop)")
    try:
        # Keep the orchestrator alive
        import signal
        signal.pause()
    except KeyboardInterrupt:
        print(f"[Orchestrator] Shutting down...")
        ch.stop()
        sys.exit(0)

if __name__ == "__main__":
    main()
