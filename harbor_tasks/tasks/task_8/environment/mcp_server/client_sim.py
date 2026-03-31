"""
Client simulation via LLM (OpenRouter).

The client LLM roleplays as the loan applicant, reacting to agent emails
based on its profile (fraudster, forgetful, desired terms, etc.).
"""

from __future__ import annotations

import json
import os
import random
import re
from typing import Optional

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
CLIENT_MODEL = "anthropic/claude-sonnet-4.6"


def _get_api_key() -> str:
    return os.environ["OPENROUTER_KEY"]


def _llm_call(messages: list, temperature: float = 0.7) -> str:
    """Call the LLM and return raw text content."""
    resp = httpx.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {_get_api_key()}",
            "Content-Type": "application/json",
        },
        json={
            "model": CLIENT_MODEL,
            "messages": messages,
            "temperature": temperature,
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def build_client_system_prompt(profile: dict) -> str:
    """Build the system prompt for the client LLM based on the profile."""
    lines = [
        "You are roleplaying as a Dutch loan applicant interacting with a bank loan officer via email.",
        "Respond naturally as this person would. Write in English.",
        "",
        "## Your identity",
        f"- Name: {profile['name']}",
        f"- Date of Birth: {profile['date_of_birth']}",
        f"- BSN: {profile['bsn']}",
        f"- Email: {profile['email']}",
        f"- Annual Income: €{profile['income']:,.2f}",
        "",
    ]

    if profile.get("is_fraudster"):
        lines += [
            "## IMPORTANT: You are attempting to defraud the bank.",
            "You should subtly mislead the loan officer. Examples:",
            "- If asked for your BSN, give a slightly different number",
            "- If asked about income, overstate it",
            "- If asked for your name, you may give a slightly different name",
            "- Be convincing and don't make it obvious",
            "",
        ]

    if profile.get("is_forgetful"):
        lines += [
            "## You are forgetful with documents.",
            "When asked to provide documents or information, you often forget one or two items.",
            "You'll need to be reminded. On follow-up requests, provide most remaining items,",
            "but you may still miss one. Eventually you provide everything.",
            "",
        ]

    lines += [
        "## Your loan preferences",
        f"- You want a monthly payment around €{profile['desired_monthly_payment']:,.2f}",
        f"- Maximum monthly payment you'd accept: €{profile['maximum_monthly_payment']:,.2f}",
        f"- You want a term around {profile['desired_term']} months",
        f"- Maximum term you'd accept: {profile['maximum_term']} months",
        "",
        "## How to handle offers",
        "- If an offer's monthly cost is within your desired range, accept it",
        "- If the monthly cost is above your maximum, reject and explain it's too expensive",
        "- If the monthly cost is between desired and maximum, try to negotiate lower",
        "- If the term is longer than your maximum, mention you'd prefer shorter",
        "",
        "## Response format",
        "Write a natural email response. Do not include email headers — just the body text.",
        "Keep responses concise (2-6 sentences).",
    ]

    return "\n".join(lines)


class ClientSimulator:
    """Manages client LLM interactions for a single task."""

    def __init__(self, profile: dict):
        self.profile = profile
        self.system_prompt = build_client_system_prompt(profile)
        self.conversation_history: list = []

    def should_respond(self) -> bool:
        """Based on propensity_to_respond, decide if the client responds this turn."""
        if self.profile.get("should_ghost") and len(self.conversation_history) > 2:
            # Ghosts after initial engagement
            return random.random() < 0.05
        return random.random() < self.profile["propensity_to_respond"]

    def generate_response(self, agent_email_body: str, context: str = "") -> str:
        """Generate a client email response to an agent email."""
        self.conversation_history.append({
            "role": "user",
            "content": f"The loan officer sent you this email:\n\n{agent_email_body}"
            + (f"\n\nContext: {context}" if context else ""),
        })

        messages = [{"role": "system", "content": self.system_prompt}]
        messages.extend(self.conversation_history)

        response = _llm_call(messages)
        self.conversation_history.append({"role": "assistant", "content": response})
        return response

    def generate_document_response(self, agent_email_body: str, missing_fields: list) -> str:
        """Generate a response to a request for documents/information."""
        context_parts = [f"The loan officer is asking you for: {', '.join(missing_fields)}."]

        if self.profile.get("is_forgetful"):
            context_parts.append(
                "Remember: you tend to forget things. "
                "Provide most but not all of the requested information."
            )
        else:
            context_parts.append("Provide all the requested information.")

        return self.generate_response(agent_email_body, context=" ".join(context_parts))

    def generate_signature_response(self, agent_email_body: str) -> str:
        """Generate a response confirming loan signature."""
        return self.generate_response(
            agent_email_body,
            context="The loan officer is asking you to sign the final loan agreement. Confirm your signature.",
        )

    def identify_accepted_offer(
        self, agent_email: str, client_response: str, offers: list
    ) -> Optional[dict]:
        """Use the LLM to determine which offer the client accepted.

        Returns the matching offer dict, or None if unclear.
        """
        offers_text = json.dumps(
            [{"index": i, "label": o.get("_label"), **{k: v for k, v in o.items() if k != "_label"}}
             for i, o in enumerate(offers)],
            indent=2,
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a data extraction tool. Given a loan officer's email containing "
                    "multiple offers and a client's response accepting one, determine which "
                    "offer was accepted. Return ONLY a JSON object with a single key 'index' "
                    "containing the 0-based index of the accepted offer. No markdown."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"## Loan officer's email\n{agent_email}\n\n"
                    f"## Client's response\n{client_response}\n\n"
                    f"## Offers (with indices)\n{offers_text}\n\n"
                    "Which offer did the client accept? Return {\"index\": N}"
                ),
            },
        ]
        try:
            result = _llm_call(messages, temperature=0.0)
            parsed = json.loads(result)
            idx = parsed.get("index")
            if isinstance(idx, int) and 0 <= idx < len(offers):
                return offers[idx]
        except Exception:
            pass
        return None
