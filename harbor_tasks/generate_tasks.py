"""
Generate Harbor task directories from synthetic client profiles.

Each task has a clean agent container (main) and an isolated MCP server
container (mcp-server) connected via Docker Compose networking. The agent
can only interact through MCP tools — no privileged data is on its filesystem.

Usage:
    uv run python harbor_tasks/generate_tasks.py [--max-tasks N]
"""

import argparse
import json
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
HARBOR_DIR = Path(__file__).resolve().parent
BASE_ENV = HARBOR_DIR / "base_environment"
BASE_TESTS = HARBOR_DIR / "base_tests"
MCP_SERVER = HARBOR_DIR / "mcp_server"


def load_profiles() -> list:
    return json.loads((DATA_DIR / "client_profiles.json").read_text())


def build_task_data(profile: dict) -> dict:
    return {
        "profile": profile,
        "ground_truth": {
            "final_state": _infer_final_state(profile),
            "loan_goal": profile.get("_loan_goal", "Unknown"),
            "application_type": profile.get("_application_type", "Unknown"),
            "requested_amount": profile.get("_requested_amount", 0),
            "has_handle_leads": profile.get("is_forgetful", False),
            "accepted_offer": profile.get("_accepted_offer"),
            "last_offer": profile.get("_last_offer"),
        },
    }


def _infer_final_state(profile: dict) -> str:
    if profile.get("_final_state"):
        return profile["_final_state"]
    if profile.get("is_fraudster"):
        return "rejected"
    if profile.get("should_ghost"):
        return "cancelled"
    return "approved"


def build_instruction(profile: dict, task_data: dict) -> str:
    return """# Loan Officer Task

You are a loan officer at a Dutch bank. A new loan application has arrived and you must
process it through to a final decision.

## Your responsibilities

1. **Review the application** — Check the submitted information for completeness.
   If any fields are missing, email the applicant to request the missing information.

2. **Generate and send loan offers** — Create 4-6 loan offers with appropriate terms
   (Offered Amount, Monthly Cost, Number of Terms, First Withdrawal Amount).
   Send your best offer to the client via email. Handle negotiation or silence.

3. **Verify and finalize** — Once an offer is accepted, collect any remaining documents
   and information (including income verification). Run fraud and credit checks.
   Make your final approve/reject decision.

## Tools available

- `check_inbox` — View your email inbox (unread/read messages)
- `read_email(message_id)` — Read a specific email
- `send_email(to, subject, body)` — Send an email
- `reply_email(message_id, body)` — Reply to an email
- `fraud_check(bsn)` — Check if a BSN is flagged for fraud
- `bkr_check(bsn)` — Check total active credits for a BSN (≥6 is high risk)
- `wait()` — Let 12 hours pass (use when waiting for client response)
- `finalize_decision(application_id, decision)` — End the task with 'approve' or 'reject'

## Important notes

- You MUST call `finalize_decision` exactly once to complete the task
- If the client doesn't respond to offers, follow up and use `wait()`. After 26 simulated
  days of no response, the application is automatically cancelled
- When sending an offer via email, clearly state the terms:
  Offered Amount, Monthly Cost, Number of Terms, and First Withdrawal Amount
- Before approving, you should run both fraud_check and bkr_check
- If approving, email the client about the approval and request their signature
  before calling finalize_decision

The application details will be provided by the environment when you start.
"""


def build_task_toml(task_index: int) -> str:
    return f"""version = "1.0"

[metadata]
dataset = "bpi-2017-loan-officer"
task_index = {task_index}
category = "agentic-workflow"
tags = ["loan-processing", "email", "decision-making", "bpi-2017"]

[verifier]
timeout_sec = 300.0

[agent]
timeout_sec = 600.0

[environment]
build_timeout_sec = 300.0
cpus = 1
memory_mb = 2048
storage_mb = 4096
allow_internet = true

[[environment.mcp_servers]]
name = "loan-processing-env"
transport = "streamable-http"
url = "http://mcp-server:8000/mcp"
"""


def build_docker_compose() -> str:
    return """services:
  main:
    depends_on:
      mcp-server:
        condition: service_healthy
    volumes:
      - shared:/shared

  mcp-server:
    build:
      context: .
      dockerfile: Dockerfile.mcp
    healthcheck:
      test: ["CMD", "python", "-c", "import httpx; httpx.get('http://localhost:8000/mcp', timeout=2)"]
      interval: 5s
      timeout: 5s
      retries: 15
      start_period: 10s
    volumes:
      - shared:/shared
    environment:
      OPENROUTER_KEY: ${OPENROUTER_KEY}

volumes:
  shared:
"""


def generate_task(task_index: int, profile: dict, output_dir: Path):
    task_dir = output_dir / f"task_{task_index}"
    env_dir = task_dir / "environment"
    tests_dir = task_dir / "tests"

    if task_dir.exists():
        shutil.rmtree(task_dir)
    env_dir.mkdir(parents=True)
    tests_dir.mkdir(parents=True)

    task_data = build_task_data(profile)

    # instruction.md
    (task_dir / "instruction.md").write_text(build_instruction(profile, task_data))

    # task.toml — no secrets, no env vars for the agent
    (task_dir / "task.toml").write_text(build_task_toml(task_index))

    # environment/ — agent container files
    shutil.copy(BASE_ENV / "Dockerfile", env_dir / "Dockerfile")

    # environment/ — MCP server container files (agent never sees these at runtime)
    shutil.copy(BASE_ENV / "Dockerfile.mcp", env_dir / "Dockerfile.mcp")
    shutil.copy(BASE_ENV / "requirements.txt", env_dir / "requirements.txt")
    shutil.copytree(MCP_SERVER, env_dir / "mcp_server")
    data_dir = env_dir / "data"
    data_dir.mkdir()
    shutil.copy(DATA_DIR / "bsn_registry.json", data_dir / "bsn_registry.json")
    shutil.copy(DATA_DIR / "bkr_registry.json", data_dir / "bkr_registry.json")
    (env_dir / "task_data.json").write_text(json.dumps(task_data, indent=2))

    # docker-compose.yaml — defines the mcp-server service
    (env_dir / "docker-compose.yaml").write_text(build_docker_compose())

    # tests/
    shutil.copy(BASE_TESTS / "test.sh", tests_dir / "test.sh")
    shutil.copy(BASE_TESTS / "test_scoring.py", tests_dir / "test_scoring.py")

    return task_dir


def main():
    parser = argparse.ArgumentParser(description="Generate Harbor tasks from client profiles")
    parser.add_argument("--max-tasks", type=int, default=None, help="Max tasks to generate")
    args = parser.parse_args()

    profiles = load_profiles()
    if args.max_tasks:
        profiles = profiles[: args.max_tasks]

    output_dir = HARBOR_DIR / "tasks"
    output_dir.mkdir(exist_ok=True)

    print(f"Generating {len(profiles)} Harbor tasks...")

    for idx, profile in enumerate(profiles):
        task_dir = generate_task(idx, profile, output_dir)
        print(f"  task_{idx}: {task_dir}")

    print(f"\nDone. Tasks written to {output_dir}/")


if __name__ == "__main__":
    main()
