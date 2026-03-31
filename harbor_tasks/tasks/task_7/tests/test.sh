#!/bin/bash
set -e

# The MCP server writes state to /shared/state.json (shared volume).
# This script reads it and writes reward.json to /logs/verifier/.

STATE_FILE="/shared/state.json"
REWARD_FILE="/logs/verifier/reward.json"

if [ ! -f "$STATE_FILE" ]; then
    echo '{"overall_pct": 0.0}' > "$REWARD_FILE"
    exit 0
fi

python3 /tests/test_scoring.py

exit 0
