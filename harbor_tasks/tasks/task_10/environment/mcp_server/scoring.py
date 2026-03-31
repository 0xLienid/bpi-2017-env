"""
Scoring logic for each phase of the loan process environment.

Tracks events as they happen and computes per-phase and overall scores.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


def extract_offer_from_email(body: str) -> Optional[dict]:
    """Try to extract offer parameters from an email body.

    Looks for patterns like:
      Offered Amount: €10,000  /  OfferedAmount: 10000
      Monthly Cost: €244       /  MonthlyCost: 244
      Number of Terms: 60      /  NumberOfTerms: 60
      First Withdrawal: €5,000 /  FirstWithdrawalAmount: 5000
    """
    def _find_number(patterns: list) -> Optional[float]:
        for pat in patterns:
            m = re.search(pat, body, re.IGNORECASE)
            if m:
                val = m.group(1).replace(",", "").replace(".", "", m.group(1).count(".") - 1) if "." in m.group(1) else m.group(1).replace(",", "")
                try:
                    return float(val)
                except ValueError:
                    continue
        return None

    offered_amount = _find_number([
        r"(?:offered\s*amount|loan\s*amount|principal)[:\s]*€?\s*([\d,\.]+)",
        r"€\s*([\d,\.]+)\s*(?:loan|amount)",
    ])
    monthly_cost = _find_number([
        r"(?:monthly\s*(?:cost|payment|repayment|installment))[:\s]*€?\s*([\d,\.]+)",
        r"€\s*([\d,\.]+)\s*(?:per\s*month|monthly|/\s*month)",
    ])
    num_terms = _find_number([
        r"(?:number\s*of\s*terms|term|duration|repayment\s*period)[:\s]*(\d+)\s*(?:months?)?",
        r"(\d+)\s*months?\s*(?:term|duration|repayment)",
    ])
    first_withdrawal = _find_number([
        r"(?:first\s*withdrawal(?:\s*amount)?|initial\s*(?:withdrawal|disbursement))[:\s]*€?\s*([\d,\.]+)",
    ])

    if offered_amount is not None and monthly_cost is not None:
        return {
            "offered_amount": offered_amount,
            "monthly_cost": monthly_cost,
            "number_of_terms": int(num_terms) if num_terms else None,
            "first_withdrawal_amount": first_withdrawal,
        }
    return None


@dataclass
class ScoringState:
    """Tracks scoring-relevant events across all phases."""

    # Ground truth from task data
    ground_truth_final_state: str = ""  # "approved", "rejected", "cancelled"
    ground_truth_offer: Optional[dict] = None
    ground_truth_was_fraud: bool = False
    had_missing_fields_phase1: bool = False
    had_missing_fields_phase3: bool = False

    # Phase 1 tracking
    agent_emailed_in_phase1: bool = False
    phase1_complete: bool = False

    # Phase 2 tracking
    agent_offers_sent: List[dict] = field(default_factory=list)
    negotiation_steps: int = 0
    offer_accepted: bool = False
    timed_out_26_days: bool = False
    phase2_complete: bool = False

    # Phase 3 tracking
    agent_emailed_in_phase3: bool = False
    agent_called_fraud_check: bool = False
    agent_called_bkr_check: bool = False
    agent_final_decision: Optional[str] = None  # "approve" or "reject"
    phase3_complete: bool = False

    def record_agent_email_phase1(self):
        self.agent_emailed_in_phase1 = True

    def record_agent_email_phase3(self):
        self.agent_emailed_in_phase3 = True

    def record_offer_sent(self, offer: dict):
        self.agent_offers_sent.append(offer)
        self.negotiation_steps += 1

    def record_offer_accepted(self):
        self.offer_accepted = True

    def record_timeout(self):
        self.timed_out_26_days = True

    def record_fraud_check(self):
        self.agent_called_fraud_check = True

    def record_bkr_check(self):
        self.agent_called_bkr_check = True

    def record_final_decision(self, decision: str):
        self.agent_final_decision = decision

    # ----- Score computation -----

    def phase1_score(self) -> float:
        """Phase 1: +1 if missing info and agent emailed, -1 if no missing info and agent emailed, 0 otherwise."""
        if self.had_missing_fields_phase1 and self.agent_emailed_in_phase1:
            return 1.0
        elif not self.had_missing_fields_phase1 and self.agent_emailed_in_phase1:
            return -1.0
        return 0.0

    def phase2_score(self) -> float:
        """Phase 2: closeness of final offer to ground truth + negotiation penalty.

        Score = (1 - avg_pct_diff) * (1 - negotiation_penalty)
        Range: [0, 1]
        """
        if not self.agent_offers_sent or self.ground_truth_offer is None:
            return 0.0

        last_offer = self.agent_offers_sent[-1]
        gt = self.ground_truth_offer

        diffs = []
        for key in ["offered_amount", "monthly_cost"]:
            if gt.get(key) and last_offer.get(key) is not None:
                gt_val = gt[key]
                agent_val = last_offer[key]
                if gt_val > 0:
                    diffs.append(abs(agent_val - gt_val) / gt_val)

        for key in ["number_of_terms", "first_withdrawal_amount"]:
            if gt.get(key) is not None and last_offer.get(key) is not None:
                gt_val = gt[key]
                agent_val = last_offer[key]
                if gt_val > 0:
                    diffs.append(abs(agent_val - gt_val) / gt_val)

        avg_diff = sum(diffs) / len(diffs) if diffs else 0.0
        closeness = max(0.0, 1.0 - avg_diff)

        # Negotiation penalty: first 3 renegotiations free, then exponential ramp
        # penalty = 0 for steps <= 3, then 1 - e^(-0.5 * (steps - 3))
        excess = max(0, self.negotiation_steps - 1 - 3)
        penalty = 1.0 - math.exp(-0.5 * excess) if excess > 0 else 0.0

        return round(closeness * (1.0 - penalty), 4)

    def phase3_score(self) -> float:
        """Phase 3 scoring:
        - +1 if missing fields and agent emailed (or 0/-1 analog)
        - +1 for calling fraud check
        - +1 for calling BKR check
        - +1 if final decision matches ground truth
        Max: 4 points, returned as fraction of max.
        """
        score = 0.0
        max_score = 0.0

        # Missing fields email check
        if self.had_missing_fields_phase3:
            max_score += 1.0
            if self.agent_emailed_in_phase3:
                score += 1.0
        else:
            # No missing fields; penalize if agent emailed anyway
            if self.agent_emailed_in_phase3:
                score -= 1.0

        # Fraud check
        max_score += 1.0
        if self.agent_called_fraud_check:
            score += 1.0

        # BKR check
        max_score += 1.0
        if self.agent_called_bkr_check:
            score += 1.0

        # Final decision match
        max_score += 1.0
        if self.agent_final_decision is not None:
            gt_decision = "approve" if self.ground_truth_final_state == "approved" else "reject"
            if self.agent_final_decision == gt_decision:
                score += 1.0

        return round(score / max_score, 4) if max_score > 0 else 0.0

    def overall_score(self) -> dict:
        """Compute the overall score as percentage of total possible points."""
        p1 = self.phase1_score()
        p2 = self.phase2_score()
        p3 = self.phase3_score()

        # Total possible: phase1 contributes 1 if there were missing fields,
        # phase2 contributes 1, phase3 contributes 1 (normalized)
        total_possible = 0.0
        total_earned = 0.0

        if self.had_missing_fields_phase1:
            total_possible += 1.0
            total_earned += p1

        if self.phase2_complete or self.timed_out_26_days:
            total_possible += 1.0
            total_earned += p2

        if self.phase3_complete or self.agent_final_decision is not None:
            total_possible += 1.0
            total_earned += p3

        overall = total_earned / total_possible if total_possible > 0 else 0.0

        return {
            "phase1_score": p1,
            "phase2_score": p2,
            "phase3_score": p3,
            "total_earned": round(total_earned, 4),
            "total_possible": total_possible,
            "overall_pct": round(overall, 4),
            "was_fraud_attempt": self.ground_truth_was_fraud,
            "agent_rejected": self.agent_final_decision == "reject",
        }
