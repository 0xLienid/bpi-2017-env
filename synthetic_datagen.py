"""
Synthetic data generation for BPI 2017 challenge loan process tasks.

Parses the two XES event logs, extracts per-task features, and generates
synthetic client profiles, BSN registry, and BKR registry entries.
Uses an LLM (via OpenRouter) for name/email generation and profile validation.
"""

import json
import os
import random
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv

load_dotenv()

random.seed(42)

OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
LLM_MODEL = "google/gemini-2.5-flash"

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class OfferInfo:
    offer_id: str
    application_id: str
    offered_amount: float
    monthly_cost: float
    number_of_terms: int
    first_withdrawal_amount: float
    credit_score: int
    selected: bool
    accepted: bool
    events: List[dict] = field(default_factory=list)


@dataclass
class TaskInfo:
    """Raw data extracted from the BPI 2017 XES logs for one application."""
    application_id: str
    loan_goal: str
    application_type: str
    requested_amount: float
    events: List[dict] = field(default_factory=list)
    offers: List[OfferInfo] = field(default_factory=list)

    # Derived flags (populated by analyse())
    has_handle_leads: bool = False
    has_fraud_assessment: bool = False
    is_forgetful: bool = False
    final_state: str = ""  # "approved", "rejected", "cancelled"
    offer_followup_count: int = 0
    offer_cancelled_by_timeout: bool = False
    offer_iterations: int = 0
    accepted_offer: Optional[OfferInfo] = None
    last_offer: Optional[OfferInfo] = None
    incomplete_loops: int = 0

    def analyse(self):
        """Derive flags and features from the raw event log."""
        event_names = [e["concept:name"] for e in self.events]
        transitions = [(e["concept:name"], e.get("lifecycle:transition", "")) for e in self.events]

        self.has_handle_leads = any(n == "W_Handle leads" for n in event_names)
        self.has_fraud_assessment = any(n == "W_Assess potential fraud" for n in event_names)

        incomplete_resumes = sum(
            1 for n, t in transitions if n == "W_Call incomplete files" and t == "resume"
        )
        self.incomplete_loops = incomplete_resumes
        self.is_forgetful = self.has_handle_leads or incomplete_resumes > 0

        if any(n == "A_Denied" for n in event_names):
            self.final_state = "rejected"
        elif any(n == "A_Cancelled" for n in event_names):
            self.final_state = "cancelled"
        elif any(n == "A_Pending" for n in event_names):
            self.final_state = "approved"
        elif any(n == "A_Complete" for n in event_names):
            self.final_state = "approved"
        else:
            self.final_state = "cancelled"

        sent_offers = [o for o in self.offers if any(
            e["concept:name"].startswith("O_Sent") for e in o.events
        )]
        self.offer_iterations = len(sent_offers)

        self.accepted_offer = next(
            (o for o in self.offers if o.accepted), None
        )
        self.last_offer = sent_offers[-1] if sent_offers else None

        has_accepted = any(o.accepted for o in self.offers)
        has_cancelled = any(
            any(e["concept:name"] == "O_Cancelled" for e in o.events)
            for o in self.offers
        )
        self.offer_cancelled_by_timeout = has_cancelled and not has_accepted

        self.offer_followup_count = sum(
            1 for n, t in transitions if n == "W_Call after offers" and t in ("resume", "start")
        )


@dataclass
class ClientProfile:
    task_index: int
    application_id: str
    offer_ids: List[str]
    name: str
    date_of_birth: str
    bsn: str
    email: str
    income: float
    is_fraudster: bool
    is_forgetful: bool
    propensity_to_respond: float
    should_ghost: bool
    desired_monthly_payment: float
    maximum_monthly_payment: float
    desired_term: int
    maximum_term: int


@dataclass
class BSNRegistryEntry:
    name: str
    bsn: str
    bsn_flagged: bool


@dataclass
class BKRRegistryEntry:
    name: str
    bsn: str
    total_active_credits: int


# ---------------------------------------------------------------------------
# XES parsing
# ---------------------------------------------------------------------------

def parse_main_log(path: str) -> Dict[str, TaskInfo]:
    """Stream-parse the main BPI 2017 XES log into TaskInfo objects keyed by application ID."""
    tasks: Dict[str, TaskInfo] = {}
    in_event = False
    in_trace = False
    current_trace_attrs: dict = {}
    current_events: list = []
    current_event_attrs: dict = {}

    for action, elem in ET.iterparse(path, events=["start", "end"]):
        if action == "start":
            if elem.tag == "trace":
                in_trace = True
                current_trace_attrs = {}
                current_events = []
            elif elem.tag == "event":
                in_event = True
                current_event_attrs = {}
        elif action == "end":
            if elem.tag == "event":
                current_events.append(current_event_attrs)
                current_event_attrs = {}
                in_event = False
                elem.clear()
            elif elem.tag == "trace":
                app_id = current_trace_attrs.get("concept:name", "")
                tasks[app_id] = TaskInfo(
                    application_id=app_id,
                    loan_goal=current_trace_attrs.get("LoanGoal", "Unknown"),
                    application_type=current_trace_attrs.get("ApplicationType", "Unknown"),
                    requested_amount=float(current_trace_attrs.get("RequestedAmount", 0)),
                    events=current_events,
                )
                in_trace = False
                elem.clear()
            elif elem.tag in ("string", "float", "int", "boolean", "date") and in_trace:
                key = elem.get("key", "")
                val = elem.get("value", "")
                if in_event:
                    current_event_attrs[key] = val
                else:
                    current_trace_attrs[key] = val

    return tasks


def parse_offer_log(path: str) -> Dict[str, List[OfferInfo]]:
    """Stream-parse the offer log. Returns offers grouped by ApplicationID."""
    offers_by_app: Dict[str, List[OfferInfo]] = {}

    for event, elem in ET.iterparse(path, events=["end"]):
        if elem.tag == "trace":
            attrs: dict = {}
            events: list = []
            for child in elem:
                if child.tag in ("string", "float", "int", "boolean", "date"):
                    attrs[child.get("key", "")] = child.get("value", "")
                elif child.tag == "event":
                    ev = {}
                    for a in child:
                        ev[a.get("key", "")] = a.get("value", "")
                    events.append(ev)

            offer = OfferInfo(
                offer_id=attrs.get("concept:name", ""),
                application_id=attrs.get("ApplicationID", ""),
                offered_amount=float(attrs.get("OfferedAmount", 0)),
                monthly_cost=float(attrs.get("MonthlyCost", 0)),
                number_of_terms=int(attrs.get("NumberOfTerms", 0)),
                first_withdrawal_amount=float(attrs.get("FirstWithdrawalAmount", 0)),
                credit_score=int(attrs.get("CreditScore", 0)),
                selected=attrs.get("Selected", "false") == "true",
                accepted=attrs.get("Accepted", "false") == "true",
                events=events,
            )
            app_id = offer.application_id
            offers_by_app.setdefault(app_id, []).append(offer)
            elem.clear()

    return offers_by_app


# ---------------------------------------------------------------------------
# BSN generation (unique 9-digit Dutch BSN with 11-check)
# ---------------------------------------------------------------------------

_used_bsns: set = set()


def _generate_bsn() -> str:
    """Generate a valid Dutch BSN (9 digits, passes the 11-check)."""
    while True:
        digits = [random.randint(0, 9) for _ in range(8)]
        weights = [9, 8, 7, 6, 5, 4, 3, 2]
        total = sum(d * w for d, w in zip(digits, weights))
        remainder = total % 11
        if remainder == 0:
            d9 = 0
        else:
            d9 = remainder
            if d9 > 9:
                continue

        digits.append(d9)
        bsn = "".join(str(d) for d in digits)

        if bsn not in _used_bsns and bsn != "000000000":
            _used_bsns.add(bsn)
            return bsn


# ---------------------------------------------------------------------------
# LLM helpers (OpenRouter)
# ---------------------------------------------------------------------------

def _llm_call(
    messages: list,
    response_format: Optional[dict] = None,
    temperature: float = 0.9,
) -> dict:
    """Make a single chat completion call to OpenRouter. Returns parsed JSON content."""
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format

    resp = httpx.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120.0,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content)


def generate_names_and_emails(count: int) -> List[dict]:
    """Ask the LLM to generate `count` realistic Dutch names and matching emails."""
    messages = [
        {
            "role": "system",
            "content": (
                "You generate realistic synthetic personal data for Dutch loan applicants. "
                "Return valid JSON only, no markdown."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Generate {count} unique, realistic Dutch person profiles. "
                "Each profile needs a full name (first + last, Dutch-style) and a plausible "
                "personal email address derived from or related to their name. "
                "Mix genders. Use common Dutch surnames (de Vries, Jansen, van den Berg, etc.) "
                "alongside less common ones for variety.\n\n"
                "Return a JSON object with a single key \"profiles\" containing an array of objects, "
                "each with keys \"name\" and \"email\". Example:\n"
                '{"profiles": [{"name": "Jan de Vries", "email": "jan.devries82@gmail.com"}]}'
            ),
        },
    ]
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "profiles_response",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "profiles": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "email": {"type": "string"},
                            },
                            "required": ["name", "email"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["profiles"],
                "additionalProperties": False,
            },
        },
    }
    result = _llm_call(messages, response_format=response_format)
    return result["profiles"]


def validate_profile(
    profile: dict,
    task: TaskInfo,
    bkr_entry: dict,
) -> dict:
    """Ask the LLM to validate a generated profile against the raw trace and spec rules.

    Returns {"valid": true/false, "issues": [...]}.
    """
    # Build a compact trace summary
    event_summary = [
        f"{e['concept:name']} [{e.get('lifecycle:transition', '')}]"
        for e in task.events
    ]
    offer_summary = []
    for o in task.offers:
        offer_summary.append({
            "offer_id": o.offer_id,
            "amount": o.offered_amount,
            "monthly_cost": o.monthly_cost,
            "terms": o.number_of_terms,
            "accepted": o.accepted,
            "events": [e["concept:name"] for e in o.events],
        })

    context = {
        "application_id": task.application_id,
        "loan_goal": task.loan_goal,
        "application_type": task.application_type,
        "requested_amount": task.requested_amount,
        "final_state": task.final_state,
        "event_log": event_summary,
        "offers": offer_summary,
    }

    spec_rules = """\
Rules from the specification that the profile must satisfy. Evaluate each rule \
INDEPENDENTLY — do not fail one rule because of concerns about another.

1. FRAUDSTER: Must be True if W_Assess potential fraud appears in the event log. \
Otherwise should only be True (with low probability) if the final state is "rejected".

2. IS_FORGETFUL: Must be True if W_Handle leads is in the event log OR there is \
looping (resume events) on W_Call incomplete files.

3. PROPENSITY_TO_RESPOND: Should be lower when there are more W_Call after offers \
resume/start events (more follow-ups needed = lower propensity).

4. SHOULD_GHOST: Check whether ANY offer in the trace has accepted=true. \
If at least one offer was accepted, should_ghost MUST be False (even if other offers \
have O_Cancelled events — that is normal). \
If NO offer was accepted AND at least one offer has O_Cancelled, should_ghost MUST be True. \
If no offers exist or none were cancelled, should_ghost should be False.

5. DESIRED/MAXIMUM MONTHLY PAYMENT & TERM: Should be derived from the accepted or \
last sent offer's terms, adjusted by the number of offer iterations. Values should \
be in a reasonable range relative to the reference offer — they do not need to match exactly.

6. INCOME: For rejected non-fraudsters, income must be low relative to requested \
amount OR the BKR total_active_credits must be >= 6. For approved clients, income \
should comfortably support the loan. For cancelled clients, income is not constrained \
— any reasonable positive value is acceptable.

7. BKR TOTAL_ACTIVE_CREDITS: Must be < 6 unless the client is a rejected non-fraudster \
whose rejection is explained by high BKR credits (not low income). For approved or \
cancelled clients, must be < 6."""

    messages = [
        {
            "role": "system",
            "content": (
                "You are a data quality validator. You check whether a generated synthetic "
                "client profile is consistent with the raw process trace data and the "
                "generation specification rules. Evaluate each rule independently and strictly "
                "— only fail a rule if the profile clearly violates that specific rule. "
                "Do not fail a rule due to ambiguity or concerns about other rules. "
                "Return valid JSON only, no markdown."
            ),
        },
        {
            "role": "user",
            "content": (
                f"## Raw trace data\n```json\n{json.dumps(context, indent=2)}\n```\n\n"
                f"## Generated profile\n```json\n{json.dumps(profile, indent=2)}\n```\n\n"
                f"## BKR registry entry\n```json\n{json.dumps(bkr_entry, indent=2)}\n```\n\n"
                f"## Specification rules\n{spec_rules}\n\n"
                "Check each of the 7 rules above against the trace data and profile. "
                "Return a JSON object with:\n"
                '- "valid": true if ALL rules pass, false if ANY fail\n'
                '- "checks": an array of objects, one per rule, each with '
                '"rule" (string, e.g. "FRAUDSTER"), "passed" (boolean), '
                'and "reason" (string, brief explanation)'
            ),
        },
    ]

    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "validation_response",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "valid": {"type": "boolean"},
                    "checks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "rule": {"type": "string"},
                                "passed": {"type": "boolean"},
                                "reason": {"type": "string"},
                            },
                            "required": ["rule", "passed", "reason"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["valid", "checks"],
                "additionalProperties": False,
            },
        },
    }

    return _llm_call(messages, response_format=response_format, temperature=0.0)


def fix_profile(
    profile: dict,
    bkr_entry: dict,
    task: TaskInfo,
    failed_checks: list,
) -> Tuple[dict, dict]:
    """Ask the LLM to fix profile fields that failed validation.

    Returns (updated_profile_dict, updated_bkr_entry_dict).
    """
    fixable_fields = {
        "FRAUDSTER": ["is_fraudster"],
        "IS_FORGETFUL": ["is_forgetful"],
        "PROPENSITY_TO_RESPOND": ["propensity_to_respond"],
        "SHOULD_GHOST": ["should_ghost"],
        "DESIRED/MAXIMUM MONTHLY PAYMENT & TERM": [
            "desired_monthly_payment", "maximum_monthly_payment",
            "desired_term", "maximum_term",
        ],
        "INCOME": ["income"],
        "BKR TOTAL_ACTIVE_CREDITS": ["total_active_credits"],
    }

    # Build context about what's wrong
    issues = []
    for check in failed_checks:
        issues.append(f"- {check['rule']}: {check['reason']}")
    issues_text = "\n".join(issues)

    # Determine which fields the LLM should return
    fields_to_fix = set()
    for check in failed_checks:
        rule = check["rule"].upper()
        for rule_key, field_names in fixable_fields.items():
            if rule_key in rule:
                fields_to_fix.update(field_names)
    if not fields_to_fix:
        return profile, bkr_entry

    # Build trace context
    event_summary = [
        f"{e['concept:name']} [{e.get('lifecycle:transition', '')}]"
        for e in task.events
    ]
    offer_summary = []
    for o in task.offers:
        offer_summary.append({
            "offer_id": o.offer_id,
            "amount": o.offered_amount,
            "monthly_cost": o.monthly_cost,
            "terms": o.number_of_terms,
            "accepted": o.accepted,
        })

    trace_context = {
        "application_id": task.application_id,
        "loan_goal": task.loan_goal,
        "application_type": task.application_type,
        "requested_amount": task.requested_amount,
        "final_state": task.final_state,
        "event_log": event_summary,
        "offers": offer_summary,
    }

    # Schema: only the fields that need fixing
    field_schemas = {
        "is_fraudster": {"type": "boolean"},
        "is_forgetful": {"type": "boolean"},
        "propensity_to_respond": {"type": "number"},
        "should_ghost": {"type": "boolean"},
        "desired_monthly_payment": {"type": "number"},
        "maximum_monthly_payment": {"type": "number"},
        "desired_term": {"type": "integer"},
        "maximum_term": {"type": "integer"},
        "income": {"type": "number"},
        "total_active_credits": {"type": "integer"},
    }

    properties = {f: field_schemas[f] for f in sorted(fields_to_fix) if f in field_schemas}
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "fix_response",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": properties,
                "required": sorted(fields_to_fix & set(field_schemas)),
                "additionalProperties": False,
            },
        },
    }

    messages = [
        {
            "role": "system",
            "content": (
                "You are a data quality fixer. Given a synthetic client profile that failed "
                "validation checks, you fix the flagged fields to be consistent with the raw "
                "process trace data and specification rules. Return only the corrected field values."
            ),
        },
        {
            "role": "user",
            "content": (
                f"## Raw trace data\n```json\n{json.dumps(trace_context, indent=2)}\n```\n\n"
                f"## Current profile\n```json\n{json.dumps(profile, indent=2)}\n```\n\n"
                f"## Current BKR entry\n```json\n{json.dumps(bkr_entry, indent=2)}\n```\n\n"
                f"## Failed validation checks\n{issues_text}\n\n"
                f"Fix the following fields: {', '.join(sorted(fields_to_fix))}\n"
                "Return corrected values that satisfy the specification rules."
            ),
        },
    ]

    fixes = _llm_call(messages, response_format=response_format, temperature=0.2)

    # Apply fixes
    updated_profile = dict(profile)
    updated_bkr = dict(bkr_entry)
    for field_name, value in fixes.items():
        if field_name == "total_active_credits":
            updated_bkr["total_active_credits"] = value
        elif field_name in updated_profile:
            updated_profile[field_name] = value

    # Keep BSN registry in sync with fraudster flag
    if "is_fraudster" in fixes:
        updated_profile["is_fraudster"] = fixes["is_fraudster"]

    return updated_profile, updated_bkr


# ---------------------------------------------------------------------------
# Synthetic profile generation
# ---------------------------------------------------------------------------

BASE_FRAUD_PROBABILITY = 0.02


def generate_client_profile(
    task_index: int,
    task: TaskInfo,
    name: str,
    email: str,
) -> Tuple[ClientProfile, BSNRegistryEntry, BKRRegistryEntry]:
    """Generate a synthetic client profile from an analysed TaskInfo."""

    dob = _random_dob()
    bsn = _generate_bsn()

    # --- Fraudster flag ---
    is_fraudster = False
    if task.has_fraud_assessment:
        is_fraudster = True
    elif task.final_state == "rejected":
        is_fraudster = random.random() < BASE_FRAUD_PROBABILITY

    # --- Is Forgetful ---
    is_forgetful = task.is_forgetful

    # --- Propensity to Respond ---
    if task.offer_followup_count == 0:
        propensity = round(random.uniform(0.85, 1.0), 2)
    elif task.offer_followup_count <= 2:
        propensity = round(random.uniform(0.6, 0.85), 2)
    elif task.offer_followup_count <= 5:
        propensity = round(random.uniform(0.35, 0.6), 2)
    else:
        propensity = round(random.uniform(0.1, 0.35), 2)

    # --- Should Ghost ---
    should_ghost = task.offer_cancelled_by_timeout

    # --- Desired / Maximum Monthly Payment and Term ---
    ref_offer = task.accepted_offer or task.last_offer
    if ref_offer:
        monthly = ref_offer.monthly_cost
        terms = ref_offer.number_of_terms

        iters = max(task.offer_iterations, 1)
        spread_factor = 1 + 0.1 * (iters - 1)

        desired_monthly = round(monthly / spread_factor, 2)
        maximum_monthly = round(monthly * random.uniform(1.05, 1.3), 2)
        desired_term = max(1, int(terms * spread_factor))
        maximum_term = max(desired_term, int(terms * random.uniform(1.0, 1.5)))
    else:
        amt = task.requested_amount
        desired_monthly = round(amt / random.uniform(40, 80), 2)
        maximum_monthly = round(desired_monthly * random.uniform(1.1, 1.4), 2)
        desired_term = random.randint(36, 120)
        maximum_term = max(desired_term, desired_term + random.randint(0, 36))

    # --- Income ---
    if ref_offer:
        base_monthly = ref_offer.monthly_cost
    else:
        base_monthly = task.requested_amount / 60

    rejection_via_income = False
    if task.final_state == "rejected" and not is_fraudster:
        if random.random() < 0.7:
            income = round(base_monthly * random.uniform(1.5, 2.5) * 12, 2)
            rejection_via_income = True
        else:
            income = round(base_monthly * random.uniform(3.0, 5.0) * 12, 2)
    else:
        income = round(base_monthly * random.uniform(3.0, 6.0) * 12, 2)

    # --- BKR total_active_credits ---
    if task.final_state == "rejected" and not is_fraudster and not rejection_via_income:
        total_active_credits = random.randint(6, 10)
    else:
        total_active_credits = random.randint(0, 5)

    bsn_entry = BSNRegistryEntry(name=name, bsn=bsn, bsn_flagged=is_fraudster)
    bkr_entry = BKRRegistryEntry(name=name, bsn=bsn, total_active_credits=total_active_credits)

    profile = ClientProfile(
        task_index=task_index,
        application_id=task.application_id,
        offer_ids=[o.offer_id for o in task.offers],
        name=name,
        date_of_birth=dob,
        bsn=bsn,
        email=email,
        income=income,
        is_fraudster=is_fraudster,
        is_forgetful=is_forgetful,
        propensity_to_respond=propensity,
        should_ghost=should_ghost,
        desired_monthly_payment=desired_monthly,
        maximum_monthly_payment=maximum_monthly,
        desired_term=desired_term,
        maximum_term=maximum_term,
    )

    return profile, bsn_entry, bkr_entry


def _random_dob() -> str:
    """Generate a random date of birth for someone aged 21–70."""
    today = __import__("datetime").date.today()
    min_date = today.replace(year=today.year - 70)
    max_date = today.replace(year=today.year - 21)
    delta = (max_date - min_date).days
    dob = min_date + __import__("datetime").timedelta(days=random.randint(0, delta))
    return dob.isoformat()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(
    main_log_path: str = "bpi_2017_data/BPI Challenge 2017.xes",
    offer_log_path: str = "bpi_2017_data/BPI Challenge 2017 - Offer log.xes",
    output_dir: str = "data",
    max_tasks: Optional[int] = None,
):
    """Parse XES logs, generate synthetic data, and write outputs."""

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    print("Parsing main event log...")
    tasks = parse_main_log(main_log_path)
    print(f"  -> {len(tasks)} application traces")

    print("Parsing offer log...")
    offers_by_app = parse_offer_log(offer_log_path)
    print(f"  -> {len(offers_by_app)} applications with offers")

    # Attach offers to tasks
    for app_id, offer_list in offers_by_app.items():
        if app_id in tasks:
            tasks[app_id].offers = offer_list

    # Analyse all tasks
    for t in tasks.values():
        t.analyse()

    # Optionally limit
    task_list = list(tasks.values())
    if max_tasks is not None:
        task_list = task_list[:max_tasks]

    count = len(task_list)
    print(f"Generating synthetic data for {count} tasks...")

    # --- LLM: generate names and emails in one batch ---
    print("  Requesting names and emails from LLM...")
    identities = generate_names_and_emails(count)
    # Pad if LLM returned fewer than requested (shouldn't happen with structured output)
    while len(identities) < count:
        identities.append({"name": f"Client {len(identities)}", "email": f"client{len(identities)}@example.nl"})

    profiles: List[dict] = []
    bsn_registry: List[dict] = []
    bkr_registry: List[dict] = []
    task_refs: List[TaskInfo] = []

    for idx, task in enumerate(task_list):
        identity = identities[idx]
        profile, bsn_entry, bkr_entry = generate_client_profile(
            idx, task, identity["name"], identity["email"],
        )
        profile_dict = asdict(profile)

        # Embed ground truth for the Harbor task generator
        profile_dict["_final_state"] = task.final_state
        profile_dict["_loan_goal"] = task.loan_goal
        profile_dict["_application_type"] = task.application_type
        profile_dict["_requested_amount"] = task.requested_amount

        if task.accepted_offer:
            profile_dict["_accepted_offer"] = {
                "offered_amount": task.accepted_offer.offered_amount,
                "monthly_cost": task.accepted_offer.monthly_cost,
                "number_of_terms": task.accepted_offer.number_of_terms,
                "first_withdrawal_amount": task.accepted_offer.first_withdrawal_amount,
            }
        else:
            profile_dict["_accepted_offer"] = None

        if task.last_offer:
            profile_dict["_last_offer"] = {
                "offered_amount": task.last_offer.offered_amount,
                "monthly_cost": task.last_offer.monthly_cost,
                "number_of_terms": task.last_offer.number_of_terms,
                "first_withdrawal_amount": task.last_offer.first_withdrawal_amount,
            }
        else:
            profile_dict["_last_offer"] = None

        profiles.append(profile_dict)
        bsn_registry.append(asdict(bsn_entry))
        bkr_registry.append(asdict(bkr_entry))
        task_refs.append(task)

    # --- LLM: validate each profile against its trace, fix if needed ---
    MAX_FIX_ATTEMPTS = 3
    print("  Validating profiles against trace data...")
    validations: List[dict] = []
    all_valid = True
    for idx, (profile, bkr_entry, task) in enumerate(zip(profiles, bkr_registry, task_refs)):
        result = validate_profile(profile, task, bkr_entry)
        attempt = 0

        while not result["valid"] and attempt < MAX_FIX_ATTEMPTS:
            attempt += 1
            failed = [c for c in result["checks"] if not c["passed"]]
            failed_rules = ", ".join(c["rule"] for c in failed)
            print(f"    Task {idx} ({task.application_id}): FAIL — fixing attempt {attempt} ({failed_rules})")

            profile, bkr_entry = fix_profile(profile, bkr_entry, task, failed)
            profiles[idx] = profile
            bkr_registry[idx] = bkr_entry

            # Sync BSN registry if fraudster flag changed
            if bsn_registry[idx]["bsn_flagged"] != profile.get("is_fraudster", False):
                bsn_registry[idx]["bsn_flagged"] = profile["is_fraudster"]

            result = validate_profile(profile, task, bkr_entry)

        validations.append(result)
        status = "PASS" if result["valid"] else f"FAIL (after {attempt} fix attempts)"
        if not result["valid"]:
            all_valid = False
        failed = [c for c in result["checks"] if not c["passed"]]
        print(f"    Task {idx} ({task.application_id}): {status}")
        for c in failed:
            print(f"      - {c['rule']}: {c['reason']}")

    # Write outputs
    (out / "client_profiles.json").write_text(json.dumps(profiles, indent=2))
    (out / "bsn_registry.json").write_text(json.dumps(bsn_registry, indent=2))
    (out / "bkr_registry.json").write_text(json.dumps(bkr_registry, indent=2))
    (out / "validation_results.json").write_text(json.dumps(validations, indent=2))

    print(f"\nWritten {len(profiles)} profiles to {out}/")
    print(f"  client_profiles.json")
    print(f"  bsn_registry.json")
    print(f"  bkr_registry.json")
    print(f"  validation_results.json")
    print(f"\nOverall validation: {'ALL PASSED' if all_valid else 'SOME FAILED'}")

    return profiles, bsn_registry, bkr_registry, validations


if __name__ == "__main__":
    run(max_tasks=15)
