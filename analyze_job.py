"""
Analyze a Harbor job run and report aggregate metrics.

Usage:
    python analyze_job.py jobs/2026-03-31__10-47-03
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional


def _parse_offer_from_text(text):
    """Extract a single offer's parameters from a chunk of text."""
    def _find_number(patterns):
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                val = m.group(1).replace(",", "")
                try:
                    return float(val)
                except ValueError:
                    continue
        return None

    _CUR = r"(?:€|EUR)\s*"  # matches € or EUR followed by optional space

    offered_amount = _find_number([
        r"(?:offered\s*amount|loan\s*amount|principal)[:\s]*" + _CUR + r"?([\d,\.]+)",
        _CUR + r"([\d,\.]+)\s*(?:loan|amount)",
    ])
    monthly_cost = _find_number([
        r"(?:monthly\s*(?:cost|payment|repayment|installment))[:\s]*" + _CUR + r"?([\d,\.]+)",
        _CUR + r"([\d,\.]+)\s*(?:per\s*month|monthly|/\s*month)",
    ])
    num_terms = _find_number([
        r"(?:number\s*of\s*terms|term|duration|repayment\s*period)[:\s]*(\d+)\s*(?:months?)?",
        r"(\d+)\s*months?\s*(?:term|duration|repayment)",
    ])
    first_withdrawal = _find_number([
        r"(?:first\s*withdrawal(?:\s*amount)?|initial\s*(?:withdrawal|disbursement))[:\s]*" + _CUR + r"?([\d,\.]+)",
    ])

    if offered_amount is not None and monthly_cost is not None:
        return {
            "offered_amount": offered_amount,
            "monthly_cost": monthly_cost,
            "number_of_terms": int(num_terms) if num_terms else None,
            "first_withdrawal_amount": first_withdrawal,
        }
    return None


def extract_all_offers_from_email(body):
    """Extract all offers from an email with multiple labeled options."""
    pattern = r"(?=(?:Option|Offer|Revised\s+Offer|Loan\s+Option)\s*[A-Z0-9]+\b)"
    sections = re.split(pattern, body, flags=re.IGNORECASE)

    offers = []
    for section in sections:
        label_match = re.match(
            r"((?:Option|Offer|Revised\s+Offer|Loan\s+Option)\s*[A-Z0-9]+)",
            section.strip(), re.IGNORECASE,
        )
        label = label_match.group(1).strip() if label_match else None
        offer = _parse_offer_from_text(section)
        if offer:
            offer["_label"] = label
            offers.append(offer)

    if not offers:
        offer = _parse_offer_from_text(body)
        if offer:
            offers.append(offer)

    return offers


def load_ground_truth(task_dir):
    """Load ground truth offer from the task's config.json -> task_data.json."""
    config_file = task_dir / "config.json"
    if not config_file.exists():
        return None
    try:
        config = json.loads(config_file.read_text())
        task_path = Path(config["task"]["path"])
        # task_data.json is in the environment dir
        task_data_file = task_path / "environment" / "task_data.json"
        if not task_data_file.exists():
            return None
        task_data = json.loads(task_data_file.read_text())
        return task_data.get("ground_truth", {})
    except (json.JSONDecodeError, KeyError):
        return None


def extract_last_agent_offers(trajectory):
    """Find all offers from the agent's last offer-containing email.

    Returns (last_offers_list, total_offer_emails) or (None, 0).
    """
    try:
        data = json.loads(trajectory.read_text())
    except (json.JSONDecodeError, KeyError):
        return None, 0

    last_offers = None
    total_offer_emails = 0
    for s in data.get("steps", []):
        for tc in (s.get("tool_calls") or []):
            name = tc.get("function_name", "")
            if "send_email" in name or "reply_email" in name:
                args = tc.get("arguments", {})
                body = args.get("body", "") if isinstance(args, dict) else ""
                offers = extract_all_offers_from_email(body)
                if offers:
                    last_offers = offers
                    total_offer_emails += 1
    return last_offers, total_offer_emails


def format_offer(offer):
    """Format an offer dict for display."""
    if not offer:
        return "none"
    parts = []
    if offer.get("offered_amount") is not None:
        parts.append("amt=%.0f" % offer["offered_amount"])
    if offer.get("monthly_cost") is not None:
        parts.append("monthly=%.2f" % offer["monthly_cost"])
    if offer.get("number_of_terms") is not None:
        parts.append("terms=%d" % offer["number_of_terms"])
    if offer.get("first_withdrawal_amount") is not None:
        parts.append("1st=%.0f" % offer["first_withdrawal_amount"])
    return ", ".join(parts) if parts else "none"


def offer_diff_pct(agent_offer, gt_offer):
    """Compute average percentage difference between agent and ground truth offers."""
    if not agent_offer or not gt_offer:
        return None
    diffs = []
    for key in ["offered_amount", "monthly_cost", "number_of_terms", "first_withdrawal_amount"]:
        a = agent_offer.get(key)
        g = gt_offer.get(key)
        if a is not None and g is not None and g > 0:
            diffs.append(abs(a - g) / g)
    return sum(diffs) / len(diffs) if diffs else None


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
    offer_comparisons = []

    for task_dir in task_dirs:
        task_name = task_dir.name

        # Score breakdown
        stdout_file = task_dir / "verifier" / "test-stdout.txt"
        breakdown = parse_score_breakdown(stdout_file) if stdout_file.exists() else None

        # Trajectory
        trajectory_file = task_dir / "agent" / "trajectory.json"
        traj = analyze_trajectory(trajectory_file) if trajectory_file.exists() else None

        # Offer comparison
        agent_offers, n_offer_emails = extract_last_agent_offers(trajectory_file) if trajectory_file.exists() else (None, 0)
        gt = load_ground_truth(task_dir)
        gt_offer = None
        if gt:
            gt_offer = gt.get("accepted_offer") or gt.get("last_offer")

        if not breakdown:
            failed_tasks.append(task_name)
            continue

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

        # Find the closest agent offer to ground truth (best case for the agent)
        best_agent_offer = None
        best_diff = None
        if agent_offers and gt_offer:
            for ao in agent_offers:
                d = offer_diff_pct(ao, gt_offer)
                if d is not None and (best_diff is None or d < best_diff):
                    best_diff = d
                    best_agent_offer = ao
        elif agent_offers:
            best_agent_offer = agent_offers[-1]

        if agent_offers or gt_offer:
            offer_comparisons.append({
                "task": task_name,
                "agent_offers": agent_offers or [],
                "best_agent_offer": best_agent_offer,
                "ground_truth": gt_offer,
                "diff_pct": best_diff,
                "n_offer_emails": n_offer_emails,
            })

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
        print("Skipped (no score): %d (%s)" % (len(failed_tasks), ", ".join(failed_tasks)))

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

    # --- Offer comparison ---
    if offer_comparisons:
        print()
        print("Offer comparison (agent's best offer vs ground truth):")
        diffs_with_values = []
        for oc in offer_comparisons:
            n_offers = len(oc["agent_offers"])
            best_str = format_offer(oc["best_agent_offer"])
            gt_str = format_offer(oc["ground_truth"])
            diff = oc["diff_pct"]
            diff_str = "%.1f%% diff" % (diff * 100) if diff is not None else "n/a"
            print("  %s:" % oc["task"])
            if n_offers > 1:
                print("    agent:  %s  (%d offers in last email, %d emails total)" % (
                    best_str, n_offers, oc["n_offer_emails"]))
            elif n_offers == 1:
                print("    agent:  %s  (%d email%s)" % (
                    best_str, oc["n_offer_emails"],
                    "s" if oc["n_offer_emails"] != 1 else ""))
            else:
                print("    agent:  none")
            print("    truth:  %s" % gt_str)
            print("    diff:   %s" % diff_str)
            if diff is not None:
                diffs_with_values.append(diff)

        if diffs_with_values:
            avg_diff = sum(diffs_with_values) / len(diffs_with_values)
            print()
            print("  Avg offer diff: %.1f%% (across %d tasks with offers)" % (
                avg_diff * 100, len(diffs_with_values)))

    print("=" * 60)


if __name__ == "__main__":
    main()
