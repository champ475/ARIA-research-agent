---
name: planning.intake
version: 1
description: Planning intake: profile updates + reply as JSON.
---
{field_guide}

CURRENT KNOWN PROFILE (JSON, may be empty):
{profile_json}

USER'S LATEST MESSAGE: "{message}"

Task: (1) extract any fields above you can confidently fill in from this
message, (2) decide if the MINIMUM fields are now all known, (3) write a
natural, conversational reply — NOT a robotic numbered form. If minimum
fields are missing, ask for 1-3 of the missing ones that flow naturally from
what the user just said (prioritize MINIMUM over nice-to-have, don't ask for
fields already known). If minimum fields are now complete, say so and offer
to build the plan now while inviting any nice-to-have detail they want to add
first.

Return ONLY valid JSON, no markdown fences:
{{"updates": {{...fields extracted this turn...}}, "minimum_complete": true or false, "reply": "..."}}

