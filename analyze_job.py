"""
Analyze a Harbor job run and report aggregate metrics.

Usage:
    python analyze_job.py jobs/2026-03-31__10-47-03
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional


# MCP tool prefix to strip for cleaner display
MCP_PREFIX = "mcp__loan_processing_env__"
# Alternative with hyphen
MCP_PREFIX_ALT = "mcp__loan-processing-env__"


def clean_tool_name(name: str) -> str:
    """Strip MCP prefix from tool names for display."""
    if name.startswith(MCP_PREFIX):
        return name[len(MCP_PREFIX):]
    if name.startswith(MCP_PREFIX_ALT):
        return name[len(MCP_PREFIX_ALT):]
    return name


def parse_score_breakdown(test_stdout: Path) -> Optional[dict]:
    """Parse the JSON score breakdown from test-stdout.txt."""
    text = test_stdout.read_text()
    idx = text.find("{")
    if idx == -1:
        return None
    end = text.find("}", idx)
    if end == -1:
        return None
    try:
        return json.loads(text[idx : end + 1])
    except json.JSONDecodeError:
        return None


def analyze_trajectory(trajectory: Path) -> Optional[dict]:
    """Extract step counts and tool call breakdown from trajectory.json.

    Returns dict with total_steps, non_wait_steps, tool_counts, and
    tool_counts_by_phase (inferred from tool sequence).
    """
    try:
        data = json.loads(trajectory.read_text())
    except (json.JSONDecodeError, KeyError):
        return None

    steps = data.get("steps", [])
    total = len(steps)
    wait_count = 0
    tool_counts = Counter()  # type: Counter[str]

    # Phase inference: track based on tool call patterns
    # - APPLICATION: before any offer is sent
    # - OFFER: after first send_email/reply_email with offer-like content, until fraud/bkr check
    # - VERIFICATION: after first fraud_check or bkr_check
    # - FINALIZED: after finalize_decision
    phase = "application"
    tool_counts_by_phase = defaultdict(Counter)  # type: defaultdict[str, Counter[str]]

    for s in steps:
        for tc in (s.get("tool_calls") or []):
            raw_name = tc.get("function_name", "")
            name = clean_tool_name(raw_name)
            tool_counts[name] += 1
            tool_counts_by_phase[phase][name] += 1

            if "wait" in name.lower():
                wait_count += 1

            # Phase transitions (heuristic)
            if phase == "application" and name in ("send_email", "reply_email"):
                # Check if this looks like an offer (has amount-like args)
                args = tc.get("arguments", {})
                body = args.get("body", "") if isinstance(args, dict) else ""
                if any(kw in body.lower() for kw in ["offer", "amount", "monthly", "term"]):
                    phase = "offer"
            elif phase == "offer" and name in ("fraud_check", "bkr_check"):
                phase = "verification"
            elif name == "finalize_decision":
                phase = "finalized"

    return {
        "total_steps": total,
        "non_wait_steps": total - wait_count,
        "tool_counts": dict(tool_counts),
        "tool_counts_by_phase": {p: dict(c) for p, c in tool_counts_by_phase.items()},
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze_job.py <job_path>")
        sys.exit(1)

    job_dir = Path(sys.argv[1])
    if not job_dir.is_dir():
        print("Error: %s is not a directory" % job_dir)
        sys.exit(1)

    task_dirs = sorted([d for d in job_dir.iterdir() if d.is_dir() and d.name.startswith("task_")])

    if not task_dirs:
        print("No task directories found in %s" % job_dir)
        sys.exit(1)

    scores = []
    steps_list = []
    non_wait_steps_list = []
    fraud_attempts = 0
    fraud_missed = 0
    failed_tasks = []
    agg_tool_counts = Counter()  # type: Counter[str]
    agg_tool_by_phase = defaultdict(Counter)  # type: defaultdict[str, Counter[str]]
    n_trajectories = 0

    for task_dir in task_dirs:
        task_name = task_dir.name

        # Score breakdown
        stdout_file = task_dir / "verifier" / "test-stdout.txt"
        breakdown = parse_score_breakdown(stdout_file) if stdout_file.exists() else None

        # Trajectory
        trajectory_file = task_dir / "agent" / "trajectory.json"
        traj = analyze_trajectory(trajectory_file) if trajectory_file.exists() else None

        if breakdown:
            scores.append(breakdown)

            if breakdown.get("was_fraud_attempt"):
                fraud_attempts += 1
                if not breakdown.get("agent_rejected"):
                    fraud_missed += 1

        if traj:
            n_trajectories += 1
            steps_list.append(traj["total_steps"])
            non_wait_steps_list.append(traj["non_wait_steps"])
            for tool, count in traj["tool_counts"].items():
                agg_tool_counts[tool] += count
            for phase, counts in traj["tool_counts_by_phase"].items():
                for tool, count in counts.items():
                    agg_tool_by_phase[phase][tool] += count

        agent_steps = traj["total_steps"] if traj else None
        non_wait = traj["non_wait_steps"] if traj else None

        if breakdown:
            print("  %s: score=%.2f  steps=%s (%s non-wait)  "
                  "fraud=%s  rejected=%s  finalized=%s" % (
                      task_name,
                      breakdown.get("overall_pct", 0),
                      agent_steps or "?",
                      non_wait or "?",
                      "Y" if breakdown.get("was_fraud_attempt") else "N",
                      "Y" if breakdown.get("agent_rejected") else "N",
                      "Y" if breakdown.get("finalized") else "N",
                  ))
        else:
            failed_tasks.append(task_name)
            print("  %s: NO SCORE (verifier may have failed)" % task_name)

    # --- Summary ---
    print()
    print("=" * 60)
    print("Tasks scored:    %d / %d" % (len(scores), len(task_dirs)))

    if scores:
        avg_score = sum(s.get("overall_pct", 0) for s in scores) / len(scores)
        print("Avg score:       %.4f" % avg_score)

    if steps_list:
        avg_steps = sum(steps_list) / len(steps_list)
        avg_non_wait = sum(non_wait_steps_list) / len(non_wait_steps_list)
        print("Avg agent steps: %.1f total, %.1f non-wait" % (avg_steps, avg_non_wait))

    if fraud_attempts > 0:
        fraud_miss_pct = (fraud_missed / fraud_attempts) * 100
        print("Fraud detection: %d/%d caught (%.1f%% missed)" % (
            fraud_attempts - fraud_missed, fraud_attempts, fraud_miss_pct))
    else:
        print("Fraud detection: no fraud attempts in this run")

    if failed_tasks:
        print("Failed tasks:    %s" % ", ".join(failed_tasks))

    # --- Tool call breakdown ---
    if agg_tool_counts:
        print()
        print("Tool call breakdown (total across %d tasks):" % n_trajectories)
        avg_divisor = max(n_trajectories, 1)
        for tool, count in sorted(agg_tool_counts.items(), key=lambda x: -x[1]):
            print("  %-25s %4d  (avg %.1f/task)" % (tool, count, count / avg_divisor))

    # --- Tool calls by phase ---
    if agg_tool_by_phase:
        phase_order = ["application", "offer", "verification", "finalized"]
        phases_present = [p for p in phase_order if p in agg_tool_by_phase]
        # Add any phases not in standard order
        for p in agg_tool_by_phase:
            if p not in phases_present:
                phases_present.append(p)

        print()
        print("Tool calls by phase:")
        for phase in phases_present:
            counts = agg_tool_by_phase[phase]
            phase_total = sum(counts.values())
            print("  [%s] (%d calls)" % (phase.upper(), phase_total))
            for tool, count in sorted(counts.items(), key=lambda x: -x[1]):
                print("    %-23s %4d" % (tool, count))

    print("=" * 60)


if __name__ == "__main__":
    main()
