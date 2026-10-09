# Test plan format

A test plan is a Markdown file. Write it from scratch, generate it with `argus-qa analyze`, or edit it as a form in the web UI (which stores the same Markdown).

```markdown
# Test Plan: My App

> URL: https://myapp.com

## Credentials

| Role    | Username         | Password  | Notes       |
|---------|------------------|-----------|-------------|
| Admin   | admin@myapp.com  | admin123  | Full access |
| User    | user@myapp.com   | user123   | Limited     |

## Test Cases

### TC-001: Login with valid credentials

**Priority:** critical
**Category:** functional

**Preconditions:**
- User is logged out

**Steps:**
1. Go to the login page
2. Enter `admin@myapp.com` in the email field
3. Enter `admin123` in the password field
4. Click "Sign In"

**Expected result:**
- User is redirected to the dashboard
- Welcome message is visible
```

## The parts

| Part | Required | Notes |
|------|----------|-------|
| `> URL:` line | no | The app's address. `--url` or a [project](projects.md) URL takes precedence |
| Credentials table | no | Better kept in a project, so passwords stay out of the plan |
| `### TC-NNN: Title` | yes | One per test case. IDs are what `--only` and `--skip` use |
| Priority, Category | no | `critical`/`high`/`medium`/`low`; any category you like |
| Preconditions | no | What must be true before step 1 (logged out, a product in the cart) |
| Steps | yes | Numbered, one action each |
| Expected result | yes | What to check. Each bullet becomes an acceptance criterion |

## Writing good tests

- **Write what a person would do,** not how: "Add any product to the cart", not "click `.btn-primary`".
- **Make expected results checkable.** "An order number is shown" is checkable; "checkout works" isn't.
- **Refer to accounts by role** ("Log in as admin") or with placeholders like `{{admin.password}}`, and keep the values in a [project](projects.md).
- **Keep tests independent.** Each test should be able to run on its own, in any order.
- Criteria that need judgement ("the layout looks right") are fine for the AI, but can't be turned into a [script](scripts.md), so those tests always use AI.

See [`examples/testplan_example.md`](../examples/testplan_example.md) for a complete plan.
