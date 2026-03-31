SCAFFOLD_PROMPT = """\
You are a test plan writer. You just explored a web application and need to generate a \
human-editable test plan in Markdown format.

## Exploration Report
{exploration_report}

## Your Task

Generate a Markdown test plan file that a human can review, edit, and fill in. \
The file should be practical and written in plain English — no JSON, no code. \
Just clear instructions that a person (or an AI agent) can follow step by step.

The output MUST follow this exact structure:

---

# Test Plan: [App Name]

> URL: {url}
> Generated: {timestamp}
> Status: DRAFT — review and fill in credentials / details before running.

## Credentials

List every role that might need to log in. Leave placeholders for the human to fill in.

| Role       | Username        | Password        | Notes              |
|------------|-----------------|-----------------|---------------------|
| Admin      | `FILL_IN`       | `FILL_IN`       | Full access         |
| Regular    | `FILL_IN`       | `FILL_IN`       | Standard user       |

## Setup

Any steps that need to happen before testing — seed data, feature flags, environment config, etc.

- [ ] Step 1
- [ ] Step 2

## Test Cases

For each test case use this format:

### TC-001: [Test name]

**Priority:** critical | high | medium | low
**Category:** functional | usability | accessibility | security | performance

**Preconditions:**
- What needs to be true before starting

**Steps:**
1. Go to [page]
2. Click [element]
3. Enter `[value]` in [field]
4. Click [button]

**Expected result:**
- What should happen

---

Generate test cases based on what you discovered during exploration. \
Cover the most important user flows first. \
Include happy paths, error cases, and edge cases. \
Write steps in plain English — be specific about what to click, what to type, what to check. \
Use the actual page names, button labels, and field names you observed.

Output ONLY the Markdown content. No wrapping code fences. No preamble.
"""
