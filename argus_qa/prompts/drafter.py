DRAFTER_PROMPT = """\
You write test cases for a web application's QA team. Turn the description below into one \
clear, practical test case that a human (or an AI agent in a browser) can follow step by step.

## Description
{description}

## Project
{project_context}

## Rules
- Steps are short imperative sentences in plain English, one action each ("Click \\"Save\\"").
- Acceptance criteria are observable outcomes someone can check on screen.
- Use real page and button names only if the description mentions them; otherwise describe them generically.
- When the test needs a login, write "Log in as <role>" using a role listed above. Never write passwords.
- To use a project value, write it as {{{{name}}}} (for example {{{{promo_code}}}}), only with names listed above.
- Priority is one of: critical, high, medium, low. Category is one of: functional, usability, \
accessibility, security, performance.
- Keep it to what the description asks for; don't invent extra checks.

## Output
Return only JSON:

```json
{{
  "title": "Short name for the test",
  "priority": "high",
  "category": "functional",
  "preconditions": ["What must be true before starting"],
  "steps": ["Step 1", "Step 2"],
  "expected": ["Acceptance criterion 1"]
}}
```
"""
