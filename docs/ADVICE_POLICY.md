# ARIA advice-boundary policy (COMP-01)

> **Status: DRAFT, pending review by counsel.** Owner: the operator.
> Decided on 2026-10-14: **education-only**. Nothing in this document is legal advice. Before ARIA is offered to the public, have it reviewed by a lawyer familiar with SEBI's Investment Advisers Regulations 2013 and Research Analysts Regulations 2014. Record the outcome at the end of this file.

## 1. Posture

ARIA is an **educational research tool**. It is not an investment adviser or a research analyst, and it runs without SEBI registration. Personalised advice to the public, for a fee or at scale, can fall under SEBI's IA and RA regulations, and a disclaimer does not change what a product actually does. So ARIA's behaviour stays educational:

| ARIA will | ARIA will not |
|---|---|
| Explain what data says about a company, fund or market, with sources | Tell a specific person to buy, sell or hold a specific security |
| Show **model portfolios by risk band** (conservative / moderate / aggressive), adapted to the user's horizon | Prescribe named funds or stocks *for that person* as a recommendation |
| Show fund **categories** and how funds within them are compared, with named funds only as examples ranked by a stated method | Present a price target as advice ("buy, target ₹X") |
| Discuss risks, both sides of a thesis, and what to check before deciding | Promise or imply guaranteed, assured or risk-free returns |
| Point users to a SEBI-registered adviser for personal decisions | Help with insider information, market manipulation or pump schemes |
| Explain concepts (P/E, SIP, ELSS, rebalancing) | Tip unlisted or penny stocks or grey-market premiums as opportunities |

## 2. How each request type is handled

| Request | Action | Where enforced |
|---|---|---|
| Insider information (UPSI, "before results are public") | **Refuse**, and explain why | `graph/guardrails.py::check_input` |
| Manipulation (pump-and-dump, operator stocks, circular trading) | **Refuse** | `check_input` |
| Guaranteed or assured returns ("risk-free 30%", "double my money") | **Refuse, with education** on why no investment can promise this | `check_input` |
| Personal call ("should I buy X?", "sell now?", "target price?", "multibagger tips") | **Reframe**: answer with the analysis and what to weigh, with no buy/sell call and no target as advice | `check_input` sets `advice_mode`; CommunicationLayer adds the instruction |
| Unlisted, penny, SME or grey-market tips | **Reframe**, with a risk caution | `check_input` |
| Any answer text that promises returns or makes a personal call | **Rewrite**: the sentence is removed and a note is shown | `graph/guardrails.py::check_output` |
| Planning | **Model portfolio** for the risk band; categories plus example funds, ranked by a stated method | `utils/planning_logic.py::build_strategy_prompt` |

## 3. Disclaimers

Footers are added **in code** (`graph/advice_policy.py::FOOTERS`), never left to the LLM.
- Planning, report and research answers always carry one.
- A plain chat answer carries one when it expresses a view on a security.

## 4. Records and review

- **Logging.** Every refused or reframed request is logged with its turn id and category; the `turns` table carries the status.
- **Red-team set.** At least 40 prompts in `tests/test_guardrails.py`. It must stay 100% correct on every change.
- **Review.** The operator reviews this policy before the public beta, and again whenever ARIA starts doing something new (for example, portfolio-specific suggestions).

## 5. Counsel review

| Date | Reviewer | Outcome / changes required |
|---|---|---|
| *pending* | | |
