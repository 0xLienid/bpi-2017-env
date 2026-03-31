# Synthetic Client Data Generation

## Overview

We are generating synthetic additional data about each client who requested a loan in the BPI 2017 challenge data. Additionally we are creating synthetic registries to simulate fraud risk checks against a BSN (Dutch SSN) and credit checks against the BKR (Dutch credit bureau).

## Existing Data (Per Client)

For each client we have:

- Loan Type (new credit vs limit raise)
- Loan goal
- Amount requested
- Actually generated offers (parsed from full loan process log)
- Actually accepted offers (parsed from full loan process log)
- Offer approved/rejected (parsed from full loan process log)
- Full loan process log

## Data to Generate

For each client we need to generate:

- **Name**
- **Date of Birth**
- **Unique BSN**
- **Email**
- **Income**
- **Fraudster** – A flag denoting whether the LLM roleplaying as the client should be attempting to mislead the bank in some way (giving a false name or a BSN that doesn't match their name, mis-stating their income, etc)
- **Is Forgetful** – When asked for documents or information that may be missing from their application, this denotes the extent to which the LLM roleplaying as the client should forget to include all of the requested information, triggering looping behavior
- **Propensity to Respond** – A value representing the probability that the client simulation should respond to any given email from the agent
- **Should Ghost**
- **Desired Monthly Payment**
- **Maximum Monthly Payment**
- **Desired Term**
- **Maximum Term**

## Generation Rules

### Fraudster

Set to `True` if there are any events in the process log related to failing client verification or fraud checks. If there are no such events, then some base probability should be used (very low) that randomly selects clients who end up with a final rejection to be fraudsters.

### Is Forgetful

Set to `True` if `W_handle leads` is in the event log or there is any looping around the final document checks/verification. This signifies that for whatever reason the real client left out documents or took multiple requests before properly sending all documents.

### Propensity to Respond

Specified by how many follow-ups the real loan officer had to send in the offer phase.

### Should Ghost

If the offers the client was sent end up cancelled (because they never responded to the offer in the 26-day period), set to `True`.

### Desired/Maximum Monthly Payment and Term

Set based on the number of offer iterations the client goes through before accepting (or ceasing to respond), plus the values of the final offer they accepted.

## Storage Requirements

For each task in the BPI 2017 dataset, with its associated application info and offer info, generate the additional data above and store the client info alongside its task index, application ID, and offer IDs.

Additionally, store the client's name, BSN, and "is fraudster" flag (as `bsn_flagged`) in a **global BSN Registry**.

Create a **BKR Registry** entry for each client with their name, BSN, and `total_active_credits` (<6).

### Rejected Non-Fraudster Handling

If the client ended up in a final rejected state and was *not* marked as a fraudster, you must either:

- Make their income low relative to the amount they requested in their application, **or**
- Change their BKR registry `total_active_credits` to be ≥6.