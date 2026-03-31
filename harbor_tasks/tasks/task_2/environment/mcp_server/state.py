"""
Environment state management for a single loan process task.

Manages phase transitions, simulated clock, email routing,
client simulation triggers, and scoring state.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from .client_sim import ClientSimulator
from .email import Email, EmailInbox, EmailServer
from .scoring import ScoringState, extract_offer_from_email


class Phase(str, Enum):
    APPLICATION = "application"
    OFFER = "offer"
    VERIFICATION = "verification"
    FINALIZED = "finalized"


# Application fields that can be masked if W_Handle leads occurred
MASKABLE_FIELDS = ["bsn", "loan_goal", "amount_requested", "loan_type"]
REQUIRED_FIELDS = ["name", "email"]


class EnvironmentState:
    """Full environment state for one task execution."""

    AGENT_EMAIL = "loan.officer@dutchbank.nl"
    WAIT_HOURS = 12
    TIMEOUT_DAYS = 26

    def __init__(self, task_data: dict, bsn_registry: list, bkr_registry: list):
        self.task_data = task_data
        self.profile = task_data["profile"]
        self.ground_truth = task_data["ground_truth"]
        self.bsn_registry = {e["bsn"]: e for e in bsn_registry}
        self.bkr_registry = {e["bsn"]: e for e in bkr_registry}

        # Phase
        self.phase = Phase.APPLICATION

        # Simulated clock — start at a fixed date
        self.current_time = datetime(2016, 1, 15, 9, 0, 0)
        self.phase2_start_time: Optional[datetime] = None

        # Email system
        self.email_server = EmailServer()
        self.agent_inbox = self.email_server.register_inbox(self.AGENT_EMAIL)
        self.client_inbox = self.email_server.register_inbox(self.profile["email"])

        # Client simulator
        self.client_sim = ClientSimulator(self.profile)

        # Scoring
        self.scoring = ScoringState()
        self._init_scoring()

        # Application fields — determine what's visible vs masked
        self.application_fields = self._build_application_fields()
        self.missing_fields_phase1 = self._get_missing_fields()
        self.scoring.had_missing_fields_phase1 = len(self.missing_fields_phase1) > 0

        # Track fields still missing after phase 1
        self.fields_collected_phase1: Dict[str, str] = {}

        # Phase 3 missing fields (income + anything still missing from phase 1)
        self.missing_fields_phase3: List[str] = []

        # Finalized flag
        self.finalized = False

        # State log for the test harness
        self.state_log: List[dict] = []

    def _init_scoring(self):
        gt = self.ground_truth
        self.scoring.ground_truth_final_state = gt["final_state"]
        self.scoring.ground_truth_was_fraud = self.profile.get("is_fraudster", False)

        if gt.get("accepted_offer"):
            self.scoring.ground_truth_offer = gt["accepted_offer"]
        elif gt.get("last_offer"):
            self.scoring.ground_truth_offer = gt["last_offer"]

    def _build_application_fields(self) -> dict:
        """Build the application data, masking some fields if is_forgetful."""
        fields = {
            "application_id": self.profile["application_id"],
            "name": self.profile["name"],
            "email": self.profile["email"],
            "bsn": self.profile["bsn"],
            "loan_goal": self.ground_truth.get("loan_goal", "Unknown"),
            "amount_requested": self.ground_truth.get("requested_amount", 0),
            "loan_type": self.ground_truth.get("application_type", "Unknown"),
        }

        # If the real process had W_Handle leads, mask some fields
        if self.ground_truth.get("has_handle_leads", False):
            num_to_mask = random.randint(1, len(MASKABLE_FIELDS))
            to_mask = random.sample(MASKABLE_FIELDS, num_to_mask)
            for f in to_mask:
                fields[f] = None

        return fields

    def _get_missing_fields(self) -> list:
        """Return list of field names that are None/missing."""
        return [k for k, v in self.application_fields.items() if v is None]

    def get_initial_prompt(self) -> str:
        """Build the initial task instruction shown to the agent."""
        lines = [
            "# New Loan Application",
            "",
            "A new loan application has been submitted. Here are the details:",
            "",
        ]

        field_labels = {
            "application_id": "Application ID",
            "name": "Applicant Name",
            "email": "Applicant Email",
            "bsn": "BSN (Social Security Number)",
            "loan_goal": "Loan Goal",
            "amount_requested": "Amount Requested (EUR)",
            "loan_type": "Application Type",
        }

        for key, label in field_labels.items():
            val = self.application_fields.get(key)
            if val is not None:
                if key == "amount_requested":
                    lines.append(f"- **{label}**: €{val:,.2f}")
                else:
                    lines.append(f"- **{label}**: {val}")
            else:
                lines.append(f"- **{label}**: _Not provided_")

        lines.append("")

        # Automated check note
        missing = self.missing_fields_phase1
        if missing:
            labels = [field_labels.get(f, f) for f in missing]
            lines.append("---")
            lines.append("**Automated Application Check:**")
            lines.append(f"⚠ Missing information: {', '.join(labels)}")
            lines.append("")
            lines.append("The applicant may need to be contacted to provide the missing information.")
        else:
            lines.append("---")
            lines.append("**Automated Application Check:** ✓ All required fields are present.")

        lines.append("")
        lines.append("You are the loan officer handling this application. "
                      "Use the available tools (email, fraud check, BKR check, wait, finalize decision) "
                      "to process it through to a final approve/reject decision.")

        return "\n".join(lines)

    # ---- Tool implementations ----

    def check_inbox(self) -> dict:
        return self.agent_inbox.check_inbox()

    def read_email(self, message_id: str) -> dict:
        result = self.agent_inbox.read_email(message_id)
        if result is None:
            return {"error": f"No email found with message_id {message_id}"}
        return result

    def send_email(self, to: str, subject: str, body: str) -> dict:
        result = self.agent_inbox.send(to, subject, body, self.current_time.isoformat())

        self._log_event("agent_send_email", {"to": to, "subject": subject, "body": body})

        # Track scoring events based on phase
        if self.phase == Phase.APPLICATION:
            self.scoring.record_agent_email_phase1()
        elif self.phase == Phase.VERIFICATION:
            self.scoring.record_agent_email_phase3()

        # Check if email contains an offer (phase 2)
        if self.phase == Phase.OFFER:
            offer = extract_offer_from_email(body)
            if offer:
                self.scoring.record_offer_sent(offer)

        # Trigger client response if appropriate
        if to == self.profile["email"]:
            self._handle_client_response(body)

        return result

    def reply_email(self, message_id: str, body: str) -> dict:
        # Find the original to get the recipient
        original = None
        for msg in self.agent_inbox.messages:
            if msg.message_id == message_id:
                original = msg
                break

        if original is None:
            return {"error": f"No email found with message_id {message_id}"}

        result = self.agent_inbox.reply(message_id, body, self.current_time.isoformat())

        self._log_event("agent_reply_email", {
            "in_reply_to": message_id,
            "to": original.from_addr,
            "body": body,
        })

        if self.phase == Phase.APPLICATION:
            self.scoring.record_agent_email_phase1()
        elif self.phase == Phase.VERIFICATION:
            self.scoring.record_agent_email_phase3()

        if self.phase == Phase.OFFER:
            offer = extract_offer_from_email(body)
            if offer:
                self.scoring.record_offer_sent(offer)

        if original.from_addr == self.profile["email"]:
            self._handle_client_response(body)

        return result if result else {"error": "Failed to send reply"}

    def fraud_check(self, bsn: str) -> dict:
        self.scoring.record_fraud_check()
        self._log_event("fraud_check", {"bsn": bsn})

        entry = self.bsn_registry.get(bsn)
        if entry is None:
            return {"bsn": bsn, "found": False, "flagged": False}
        return {"bsn": bsn, "found": True, "flagged": entry["bsn_flagged"]}

    def bkr_check(self, bsn: str) -> dict:
        self.scoring.record_bkr_check()
        self._log_event("bkr_check", {"bsn": bsn})

        entry = self.bkr_registry.get(bsn)
        if entry is None:
            return {"bsn": bsn, "found": False, "total_active_credits": 0}
        return {"bsn": bsn, "found": True, "total_active_credits": entry["total_active_credits"]}

    def wait(self) -> dict:
        """Advance simulated time by WAIT_HOURS. Check for timeout and client responses."""
        self.current_time += timedelta(hours=self.WAIT_HOURS)
        self._log_event("wait", {"new_time": self.current_time.isoformat()})

        # Check 26-day timeout in offer phase
        if self.phase == Phase.OFFER and self.phase2_start_time:
            elapsed = (self.current_time - self.phase2_start_time).days
            if elapsed >= self.TIMEOUT_DAYS:
                self.scoring.record_timeout()
                self.scoring.phase2_complete = True
                self.phase = Phase.FINALIZED
                self.finalized = True
                self._log_event("timeout_26_days", {})
                return {
                    "time": self.current_time.isoformat(),
                    "event": "TIMEOUT: 26 days have elapsed. The offer period has expired and the application is automatically cancelled.",
                }

        # During offer phase, re-check if a silent client will now respond
        if self.phase == Phase.OFFER:
            if self.client_sim.should_respond():
                # Client decides to respond to the most recent agent email
                last_agent_email = self._get_last_agent_email_to_client()
                if last_agent_email and not self._client_already_replied(last_agent_email):
                    response = self.client_sim.generate_response(last_agent_email.body)
                    self._deliver_client_email(
                        subject=f"Re: {last_agent_email.subject}",
                        body=response,
                        in_reply_to=last_agent_email.message_id,
                    )
                    return {
                        "time": self.current_time.isoformat(),
                        "event": "Time has passed. You have a new email in your inbox.",
                    }

        return {
            "time": self.current_time.isoformat(),
            "event": "Time has passed. No new activity.",
        }

    def finalize_decision(self, application_id: str, decision: str) -> dict:
        """Finalize the loan decision. Ends the task."""
        if self.finalized:
            return {"error": "Task has already been finalized."}

        decision = decision.lower().strip()
        if decision not in ("approve", "reject"):
            return {"error": f"Invalid decision '{decision}'. Must be 'approve' or 'reject'."}

        self.scoring.record_final_decision(decision)
        self.scoring.phase3_complete = True
        self.finalized = True
        self.phase = Phase.FINALIZED

        self._log_event("finalize_decision", {
            "application_id": application_id,
            "decision": decision,
        })

        scores = self.scoring.overall_score()
        self._save_state()

        return {
            "status": "finalized",
            "decision": decision,
            "application_id": application_id,
        }

    def advance_phase(self, to_phase: Phase):
        """Explicitly advance to the next phase."""
        self._log_event("phase_advance", {"from": self.phase.value, "to": to_phase.value})
        self.phase = to_phase
        if to_phase == Phase.OFFER:
            self.phase2_start_time = self.current_time
            self.scoring.phase1_complete = True
        elif to_phase == Phase.VERIFICATION:
            self.scoring.phase2_complete = True
            # Determine missing fields for phase 3
            still_missing = [f for f in self.missing_fields_phase1 if f not in self.fields_collected_phase1]
            # Income is always required in phase 3
            self.missing_fields_phase3 = still_missing + ["income"]
            self.scoring.had_missing_fields_phase3 = len(self.missing_fields_phase3) > 0

    # ---- Internal helpers ----

    def _handle_client_response(self, agent_email_body: str):
        """Determine if/how the client responds to an agent email."""
        if not self.client_sim.should_respond():
            return

        if self.phase == Phase.APPLICATION:
            # Client responds with missing info
            if self.missing_fields_phase1:
                response = self.client_sim.generate_document_response(
                    agent_email_body, self.missing_fields_phase1
                )
                self._deliver_client_email(
                    subject="Re: Application Information",
                    body=response,
                )
                # Mark some fields as collected (forgetful clients may not provide all)
                if self.profile.get("is_forgetful"):
                    num_provide = max(1, len(self.missing_fields_phase1) - 1)
                    provided = random.sample(self.missing_fields_phase1, num_provide)
                else:
                    provided = self.missing_fields_phase1[:]
                for f in provided:
                    self.fields_collected_phase1[f] = "provided"
            else:
                response = self.client_sim.generate_response(agent_email_body)
                self._deliver_client_email(
                    subject="Re: Loan Application",
                    body=response,
                )

            # Auto-advance to offer phase after client interaction in phase 1
            self.advance_phase(Phase.OFFER)

        elif self.phase == Phase.OFFER:
            response = self.client_sim.generate_response(agent_email_body)
            self._deliver_client_email(
                subject="Re: Loan Offer",
                body=response,
            )
            # Check if client accepted
            if self._response_indicates_acceptance(response):
                self.scoring.record_offer_accepted()
                self.advance_phase(Phase.VERIFICATION)

        elif self.phase == Phase.VERIFICATION:
            if self.missing_fields_phase3:
                response = self.client_sim.generate_document_response(
                    agent_email_body, self.missing_fields_phase3
                )
                self._deliver_client_email(
                    subject="Re: Document Request",
                    body=response,
                )
                # Forgetful clients omit some fields
                if self.profile.get("is_forgetful") and len(self.missing_fields_phase3) > 1:
                    num_provide = max(1, len(self.missing_fields_phase3) - 1)
                    provided = random.sample(self.missing_fields_phase3, num_provide)
                    self.missing_fields_phase3 = [f for f in self.missing_fields_phase3 if f not in provided]
                else:
                    self.missing_fields_phase3 = []
            else:
                # Assume this is about signing
                response = self.client_sim.generate_signature_response(agent_email_body)
                self._deliver_client_email(
                    subject="Re: Loan Agreement Signature",
                    body=response,
                )

    def _response_indicates_acceptance(self, response: str) -> bool:
        """Simple heuristic to check if a client response accepts an offer."""
        accept_phrases = [
            "i accept", "i agree", "sounds good", "i'll take it",
            "let's proceed", "i'm happy with", "looks good",
            "i would like to accept", "please proceed",
            "that works", "deal", "accepted",
        ]
        lower = response.lower()
        return any(phrase in lower for phrase in accept_phrases)

    def _deliver_client_email(self, subject: str, body: str, in_reply_to: str = None):
        """Send an email from the client to the agent."""
        email = Email(
            message_id=__import__("uuid").uuid4().hex,
            to=self.AGENT_EMAIL,
            from_addr=self.profile["email"],
            subject=subject,
            body=body,
            timestamp=self.current_time.isoformat(),
            in_reply_to=in_reply_to,
        )
        self.email_server.deliver(email)
        self._log_event("client_email", {"subject": subject, "body": body})

    def _get_last_agent_email_to_client(self) -> Optional[Email]:
        """Get the most recent email the agent sent to the client."""
        client_email = self.profile["email"]
        for msg in reversed(self.agent_inbox.messages):
            if msg.from_addr == self.AGENT_EMAIL and msg.to == client_email:
                return msg
        return None

    def _client_already_replied(self, agent_email: Email) -> bool:
        """Check if the client already replied to a specific agent email."""
        for msg in self.agent_inbox.messages:
            if msg.in_reply_to == agent_email.message_id and msg.from_addr == self.profile["email"]:
                return True
        return False

    def _log_event(self, event_type: str, data: dict):
        self.state_log.append({
            "time": self.current_time.isoformat(),
            "phase": self.phase.value,
            "event": event_type,
            "data": data,
        })

    def _save_state(self):
        """Save full state to disk for the test harness to read."""
        state = {
            "scoring": self.scoring.overall_score(),
            "state_log": self.state_log,
            "phase": self.phase.value,
            "finalized": self.finalized,
        }
        Path("/shared").mkdir(parents=True, exist_ok=True)
        Path("/shared/state.json").write_text(json.dumps(state, indent=2))

    def save_state_if_needed(self):
        """Called periodically to persist state for debugging."""
        self._save_state()
