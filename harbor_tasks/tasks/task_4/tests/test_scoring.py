"""
Test scoring script for the Harbor verifier.

Reads the environment state persisted by the MCP server and writes
reward.json to /logs/verifier/ with per-phase and overall scores.
"""

import json
from pathlib import Path

STATE_FILE = Path("/shared/state.json")
REWARD_FILE = Path("/logs/verifier/reward.json")


def main():
    if not STATE_FILE.exists():
        REWARD_FILE.write_text(json.dumps({
            "overall_pct": 0.0,
        }))
        return

    state = json.loads(STATE_FILE.read_text())
    scoring = state.get("scoring", {})

    # Harbor's mean metric expects exactly one key in reward.json.
    # Print the full breakdown to stdout (captured in verifier logs).
    details = {
        "phase1_score": float(scoring.get("phase1_score", 0.0)),
        "phase2_score": float(scoring.get("phase2_score", 0.0)),
        "phase3_score": float(scoring.get("phase3_score", 0.0)),
        "total_earned": float(scoring.get("total_earned", 0.0)),
        "total_possible": float(scoring.get("total_possible", 0.0)),
        "overall_pct": float(scoring.get("overall_pct", 0.0)),
        "was_fraud_attempt": scoring.get("was_fraud_attempt", False),
        "agent_rejected": scoring.get("agent_rejected", False),
        "finalized": state.get("finalized", False),
    }
    print("Score breakdown:")
    print(json.dumps(details, indent=2))

    reward = {"overall_pct": details["overall_pct"]}
    REWARD_FILE.write_text(json.dumps(reward, indent=2))
    print(f"\nReward written: {reward}")


if __name__ == "__main__":
    main()
