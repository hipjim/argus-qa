# Test Plan: My SaaS App

> URL: https://staging.myapp.com
> Generated: 2026-03-24 14:00 UTC
> Status: READY

## Credentials

| Role       | Username             | Password        | Notes                    |
|------------|----------------------|-----------------|--------------------------|
| Admin      | admin@myapp.com      | admin123!       | Can manage users         |
| Regular    | user@myapp.com       | user123!        | Standard access          |
| Free tier  | free@myapp.com       | free123!        | Limited features         |

## Setup

- [ ] Make sure staging environment is running
- [ ] Seed database has been loaded (run `make seed` if not)
- [ ] Feature flag `new-dashboard` is ON

## Test Cases

### TC-001: Login with valid credentials

**Priority:** critical
**Category:** functional

**Preconditions:**
- User is logged out
- On the home page

**Steps:**
1. Go to the login page by clicking "Sign In" in the top nav
2. Enter `user@myapp.com` in the email field
3. Enter `user123!` in the password field
4. Click the "Sign In" button

**Expected result:**
- User is redirected to the dashboard
- Welcome message shows the user's name
- Navigation shows logged-in state (avatar, not "Sign In" button)

---

### TC-002: Login with invalid password

**Priority:** high
**Category:** functional

**Preconditions:**
- User is logged out

**Steps:**
1. Go to the login page
2. Enter `user@myapp.com` in the email field
3. Enter `wrongpassword` in the password field
4. Click "Sign In"

**Expected result:**
- Error message appears: "Invalid email or password"
- User stays on the login page
- Password field is cleared

---

### TC-003: Create a new project

**Priority:** critical
**Category:** functional

**Preconditions:**
- Logged in as `user@myapp.com`

**Steps:**
1. Click "New Project" button on the dashboard
2. Enter `Test Project` in the project name field
3. Select "Personal" from the workspace dropdown
4. Click "Create"

**Expected result:**
- Project is created and user is taken to the project page
- Project name "Test Project" appears in the header
- The project appears in the sidebar project list

---

### TC-004: Search functionality

**Priority:** medium
**Category:** functional

**Preconditions:**
- Logged in as `user@myapp.com`
- At least one project exists

**Steps:**
1. Click the search bar (or press `/` to focus it)
2. Type `Test`
3. Wait for results to appear

**Expected result:**
- Search results dropdown shows matching projects
- Results update as you type (live search)
- Clicking a result navigates to that project

---

### TC-005: Empty state for new user

**Priority:** medium
**Category:** usability

**Preconditions:**
- Logged in as a user with no projects (use `free@myapp.com`)

**Steps:**
1. Go to the dashboard

**Expected result:**
- Empty state illustration or message is shown
- Clear call-to-action to create first project
- No broken layout or missing elements
