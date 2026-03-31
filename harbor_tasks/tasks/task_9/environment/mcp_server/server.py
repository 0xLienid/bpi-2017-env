"""
MCP server for the BPI 2017 loan officer environment.

Exposes tools: check_inbox, read_email, send_email, reply_email,
fraud_check, bkr_check, wait, finalize_decision.

Launched inside the Docker container and accessed by the agent via
streamable-http transport.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .state import EnvironmentState, Phase

# Load task data from the container filesystem
TASK_DATA_PATH = os.environ.get("TASK_DATA_PATH", "/app/task_data.json")
BSN_REGISTRY_PATH = os.environ.get("BSN_REGISTRY_PATH", "/app/data/bsn_registry.json")
BKR_REGISTRY_PATH = os.environ.get("BKR_REGISTRY_PATH", "/app/data/bkr_registry.json")


def _load_json(path: str) -> dict | list:
    return json.loads(Path(path).read_text())


# Initialize environment state
task_data = _load_json(TASK_DATA_PATH)
bsn_registry = _load_json(BSN_REGISTRY_PATH)
bkr_registry = _load_json(BKR_REGISTRY_PATH)
env = EnvironmentState(task_data, bsn_registry, bkr_registry)

# Create MCP server
mcp = FastMCP(
    "BPI 2017 Loan Officer Environment",
    instructions=env.get_initial_prompt(),
    host="0.0.0.0",
    port=8000,
)


@mcp.tool()
def check_inbox() -> str:
    """Check your email inbox. Returns a summary of unread and read messages with sender, subject, and timestamp."""
    result = env.check_inbox()
    env.save_state_if_needed()
    return json.dumps(result, indent=2)


@mcp.tool()
def read_email(message_id: str) -> str:
    """Read a specific email by its message ID. Returns the full email content including sender, subject, body, and timestamp."""
    result = env.read_email(message_id)
    env.save_state_if_needed()
    return json.dumps(result, indent=2)


@mcp.tool()
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email to the specified address. Use this to communicate with the loan applicant, send offers, request documents, etc."""
    result = env.send_email(to, subject, body)
    env.save_state_if_needed()
    return json.dumps(result, indent=2)


@mcp.tool()
def reply_email(message_id: str, body: str) -> str:
    """Reply to an existing email by its message ID. The reply is sent to the original sender with 'Re:' prepended to the subject."""
    result = env.reply_email(message_id, body)
    env.save_state_if_needed()
    return json.dumps(result, indent=2)


@mcp.tool()
def fraud_check(bsn: str) -> str:
    """Run a fraud check against the BSN registry. Input is a BSN (Dutch social security number). Returns whether the BSN is found and if it is flagged for potential fraud."""
    result = env.fraud_check(bsn)
    env.save_state_if_needed()
    return json.dumps(result, indent=2)


@mcp.tool()
def bkr_check(bsn: str) -> str:
    """Run a credit check against the BKR registry. Input is a BSN. Returns the number of total active credits the BSN holder has. 6 or more active credits is considered high risk."""
    result = env.bkr_check(bsn)
    env.save_state_if_needed()
    return json.dumps(result, indent=2)


@mcp.tool()
def wait() -> str:
    """Wait and let time pass (advances the simulated clock by 12 hours). Use this when waiting for a client response. If the client responds during this time, you will see a new email in your inbox."""
    result = env.wait()
    env.save_state_if_needed()
    return json.dumps(result, indent=2)


@mcp.tool()
def finalize_decision(application_id: str, decision: str) -> str:
    """Finalize your loan decision. This MUST be called exactly once to end the task.

    Args:
        application_id: The application ID from the initial application data.
        decision: Either 'approve' or 'reject'.

    Once called, no further actions can be taken. The decision is compared against
    the ground truth outcome for scoring.
    """
    result = env.finalize_decision(application_id, decision)
    env.save_state_if_needed()
    return json.dumps(result, indent=2)


def main():
    """Run the MCP server."""
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
