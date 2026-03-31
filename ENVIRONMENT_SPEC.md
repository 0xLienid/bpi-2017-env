# Environment Overview

This environment is meant to simulate the work of a loan officer. It will be based on data from BPI 2017, a process mining dataset tracking the steps that go into making a decision on loan applications at a Dutch bank. All tasks will be created in the Harbor format. Since this is Harbor format, it means we need to define the tools, and the state that underlies them (current date and time, BSN Registry, BKR Registry, Client Simulation) as an MCP.

There are two primary actors in the environment:

- **The agent** – roleplays the loan officer. This is who we wish to evaluate
- **The client** – roleplays the person applying for a loan. We don't need to evaluate the actor, just simulate them sufficiently accurately

## Client Data

The following is an outline for the simulated "client" data — each client profile should feature the following information:

There are bits of background information for each task (or for the overall environment set). Note that much of this information relies on one another. For example, whether a client should be attempting to defraud the bank depends on whether the real process actually ended in a rejected (by the bank) offer state, whether the client should have >6 open credit entries or anything in arrears should depend on whether the real process actually ended in a rejected (by the bank) offer state, the desired and max monthly cost is determined by the number of offer renegotiation steps and the user's final accepted offer, what the client's income should be should depend on the requested amount and whether they ended up with an approved offer, etc. These things of course then also determine what the client's BSN should be. It's a very interconnected process:

- Loan Type (new credit vs limit raise) — from the BPI data directly
- Loan goal — from the BPI data directly
- Amount requested — from the BPI data directly
- Actually generated offers
- Actually accepted offers
- Offer approved/rejected
- Full process log

### BSN Registry (For the overall environment set)

- Name
- BSN
- BSN Flagged (Y/N)

### BKR Registry (For the overall environment set)

- Name
- BSN
- Total Active Credits

### Client Profile

- Name
- DoB
- BSN
- Email
- Income
- **Fraudster** – A flag denoting whether the LLM roleplaying as the client should be attempting to mislead the bank in some way (giving a false name or a BSN that doesn't match their name, mis-stating their income, etc)
- **Is Forgetful** – When asked for documents or information that may be missing from their application, this denotes the extent to which the LLM roleplaying as the client should forget to include all of the requested information, triggering looping behavior
- **Propensity to Respond** – A value representing the probability that the client simulation should respond to any given email from the agent
- **Should Ghost**
- **Desired Monthly Payment**
- **Maximum Monthly Payment**
- **Desired Term**
- **Maximum Term**

---

## Tools

### Email

Even though really only "send email" and "reply to email" are the necessary functionality in this environment, it makes sense to include the full scope of functions that are exposed by gmail MCPs (i.e. [Gmail-MCP-Server](https://github.com/ArtyMcLabin/Gmail-MCP-Server) or similar).

**Two stateful classes:**

- **Email Server**
- **Email Inbox**

**One data class:**

- **Email**
  - Message ID
  - To
  - From
  - Subject
  - Body

**Email Server** owns pointers to a set of inboxes (email address → inbox instance) and has one function:

- **Send**
  - To
  - Data: Message ID, Last Message ID, From, Subject, Body

**Email Inbox** has email server as a state variable, has "messages" as a state variable (also tracks if a message has been read), and has the following functions:

- Read Email
- Send
- Reply
- Check Inbox — Shows high level data (from, date + time, subject) about messages, stratified by read/unread

### Fraud Check

This is a synchronous tool. Input is a BSN. Output is a boolean of whether the client is flagged for being a likely fraud (based on BSN matching a BSN in the fraud database).

### BKR Check

This is a synchronous tool. Input is a BSN. Output is an integer reflecting the number of outstanding credits the BSN holder has.

### Wait

This tool allows us to simulate the passage of time in the environment. It's a no-op tool that allows the current date time value for the task to jump forward some number of hours (maybe 12).

### Finalize Decision

This is a synchronous tool. Input is the ApplicationID and a decision: `approve` or `reject`. This tool must be called exactly once per task and marks the end of the task. Once called, no further agent actions are taken. The environment uses this tool call as the agent's final decision for scoring purposes — specifically, comparing the decision against the ground truth outcome from the real data. If the agent never calls this tool (e.g., the task times out due to 26-day auto-cancellation), the decision is treated as neither approve nor rejected and score is 0.

---

## Loan Process Phases

### Phase 1: Application

The task begins with a system message containing the application data as structured text, followed by an automated check note listing missing fields. The agent's inbox starts empty. The initial state is when an application is submitted. The application can have all of, or some subset of the following information. Based on the actual process logs where we know if the "W_handle leads" event occurred (signals loan officer had to reach out for additional information) we know whether or not any of the information should be masked (what information is to be selected at random):

- Account ID
- Name (Required)
- Email (Required)
- BSN
- Loan Goal
- Amount Requested
- Loan Type (new credit or limit raise)

As part of the initial state of the environment we simulate an automated check that the Dutch bank would run which just checks the fields and reports "Missing Application Info: xyz" — we append a note to the end of the initial prompt stating which information is missing (and which is completed).

The agent must then decide if it needs to ask the client for additional information or not.

> The Dutch bank data is weird — 100% of applications end up approved through this step, so we'll only have the agent ask once for missing data. After the client responds here it just goes straight to approved even if there's still missing information (it'll be handled later).

**Intermediate reward at the end of this phase:**

- **+1** if there was missing application information and the agent emailed the client
- **-1** if there was no missing application information and the agent emailed the client
- **0** otherwise

Application is "approved."

### Phase 2: Offer

The agent must now generate a set of loan offers for the client. It creates several offers (which are just kept in context) with the following parameters:

- Offered Amount
- Monthly Cost
- Number of Terms
- First Withdrawal Amount

Once the agent generates a set of offers (4–6), it sends one to the client. The client (based on their intrinsic desires about monthly payment and terms) can accept, attempt to negotiate a new offer, or simply ignore the offer.

If the client attempts to renegotiate, the agent can send a different offer from the originally generated set, create a new offer to send, or remain firm on the current offer based on its personality as defined by its prompt.

> **Offer creation is freeform, not tool-based.** The agent generates offers as natural language in its response text — there is no `create_offer` tool. The agent reasons about what terms to propose (amount, monthly cost, number of terms, first withdrawal amount) and composes them however it sees fit. To deliver an offer to the client, the agent calls the `send_email` tool with the offer details written into the email body. The environment parses the most recent email sent by the agent to extract the four offer parameters (OfferedAmount, MonthlyCost, NumberOfTerms, FirstWithdrawalAmount) using regex or structured extraction for scoring purposes. If the environment cannot parse valid offer parameters from the email body, the email is treated as a general communication (follow-up, clarification, etc.) rather than an offer delivery.

**Client silence handling:** If the client goes silent/ignores the offer, the agent should follow up repeatedly until the client responds (accept/renegotiate/reject). The agent will be shown a "user" message with its current inbox state. Since the client decided to not respond, the inbox should be empty, and the agent should select the "wait" tool to pass the time. When the "wait" tool is called, we check again if the client will respond this time; if they don't, the same process continues; if they do, the agent's inbox state will show that there's a new unread email.

There is a time limit of **26 days**. After this, in the real data, the offer and loan process is automatically cancelled. If 26 simulated days pass without a response from the client, the task should auto-end and trigger this phase's verification setup (checking against the ground truth whether this specific client did in fact go stale, and be rewarded or penalized accordingly).

If the client accepts the offer, this phase is complete and the verification setup for it is triggered.

**Phase 2 scoring:** Based on how close the terms of the final offer (either accepted, or last offer when the 26 simulated days are up) made by the agent are to the ground truth final offer (either accepted, or last offer before auto-cancelling after 26 days) + a penalty based on the number of renegotiation steps that happened (should exponentially increase from 0 to 1). We want the agent to learn to predict what the client wants and offer more appropriate terms faster (fewer back-and-forth negotiation steps). Difference is calculated as percentage difference on each field, averaged across fields.

### Phase 3: Verification and Finalization

The client has now accepted an offer provided by the agent and must provide any outstanding documents, pass verification of those documents, and sign the official loan.

After accepting the offer, the agent has to ask for income and any fields that are still missing from the loan application data after the first phase.

- The agent must email the client to collect the missing information. If the client's response still omits some fields, the agent must follow up again until all required fields are present.
- This phase awards **+1** if there were missing fields and the agent emailed the client to request them, and **-1** if there were no missing fields and the agent emailed anyway. This is scored identically to Phase 1.
- Once all required fields are present, the agent proceeds to run the Fraud Check and BKR Check tools and make its approve/deny decision.

Once the agent has all of the required information it needs to decide to approve or reject:

- To do this properly the agent should call both the fraud check tool and the BKR check tool and then make a decision. We'll give a reward of **+1** for calling each.

If the agent decides to approve, then they must email the client that they were approved and the final loan requires their signature (loop here until signature is obtained).

**Phase 3 scoring:** Whether the final decision (approve or reject) matches the actual decision from the real data.

### Final Decision

Once the agent has made its decision, it must call the `finalize_decision` tool with either `approve` or `reject`. If approving, the agent should email the client that they were approved and the final loan requires their signature before calling `finalize_decision`. The `finalize_decision` call ends the task.

---

## Overall Scoring

The final score of the model through a task is the percentage of the total possible score (based on the states it saw: +1 for there being missing application data in the first phase, +1 for the second phase, +3 for the third phase).

We should additionally track if the task was a fraud attempt and the agent decided to reject (i.e. `{"was_fraud_attempt": True, "rejected": True}`).

---

## Appendix: BPI Dataset Terms

| Term | Definition | Source | Values / Range |
|------|-----------|--------|---------------|
| **LoanGoal** | The stated purpose of the loan. 10.9% of real applications left this as "Unknown" or "Not specified." | Client-submitted | `Car`, `Home improvement`, `Existing loan takeover`, `Other, see explanation`, `Unknown`, `Not specified`, `Remaining debt home`, `Extra spending limit`, `Caravan / Camper`, `Motorcycle`, `Boat`, `Tax payments`, `Business goal`, `Debt restructuring` |
| **ApplicationType** | Whether the client is requesting a brand new loan or an increase to an existing credit line. Limit raises have a 73.3% approval rate vs 52.4% for new credit, because the bank already has a relationship with the customer. Limit raise applicants do not require a new credit score assessment — the bank already has their history on file. | Client-submitted | `New credit`, `Limit raise` |
| **OfferedAmount** | The loan principal the bank is proposing to lend, in EUR. Matched the client's requested amount exactly in 82% of real cases. | Agent-set | €5,000–€75,000 |
| **MonthlyCost** | The fixed monthly repayment amount the client would pay, covering both principal and interest. This is the number the client sees and reacts to most strongly. Lower MonthlyCost relative to OfferedAmount correlates with higher approval rates (1.46% ratio for approved vs 1.78% for cancelled). | Agent-set | €43–€6,674. Median €244. |
| **NumberOfTerms** | The repayment period in months. Longer terms reduce MonthlyCost but increase total interest paid. Common values are 60 months (5 years) and 120 months (10 years). | Agent-set | 5–180 months. Median 77. |
| **FirstWithdrawalAmount** | The initial lump sum disbursed to the client's account upon loan activation. May be less than the full OfferedAmount if the loan is structured for staged withdrawals. 29.7% of real offers set this to €0. | Agent-set | €0–€75,000. Median €5,000. |