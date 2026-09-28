---
name: promotion_classify
description: 'Classifier question for memory promotion: company, personal, agent_only
  or unclear, plus the personal-life check. Edit the text only; section names and the
  four labels are fixed.'
tunable: true
---
## Instructions
This is one note remembered by an AI assistant that works on a company team. Would it help other people or other AI assistants on the same company team?

## Label: company
what: Useful to the company team: people, organizations, clients, partners, vendors, products, tools and systems the business uses or builds; deals (terms, pricing, counterparties); market and competitor information; and procedures or working methods any teammate or assistant could reuse, such as how to follow up on commitments, diagnose a tool failure, or delegate work.
not_for: The operator's private life.

## Label: personal
what: The operator's private life: family, health, home, personal money, personal appointments, hobbies, and side projects unrelated to the business.
not_for: Business contacts, company work, or general working methods.

## Label: agent_only
what: Only meaningful to this one assistant's own setup and would confuse others: its private configuration, identity files, one-off session state, scratch notes.
not_for: General working methods other assistants could also follow.

## Label: unclear
what: None of these, not enough information to tell, or text that is not a statement about work or personal life.

## Personal check
This text is mainly about the operator's private life (family, health, home, personal money, hobbies).
