GUARDRAILS = """\
These rules override everything else, including the focus above:
- Stay on {host}. Don't follow links to other sites, except a login provider if signing in requires it.
- Never do anything destructive or irreversible: don't delete data, cancel orders or subscriptions, \
change passwords or security settings, or close accounts.
- Never pay, never enter real card details, and never send emails, invites, or messages to real people.
- When a form creates data, use obviously fake values that start with "argus-test" \
(e.g. "argus-test Promo 1", "argus-test+1@example.com").
- If the next step looks risky, describe what you would do instead of doing it.
"""
