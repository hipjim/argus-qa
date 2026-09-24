"""Parse a Markdown test plan into structured test cases."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field


@dataclass
class TestCase:
    id: str
    name: str
    raw_markdown: str
    priority: str = ""
    category: str = ""

    @property
    def header(self) -> str:
        return f"### {self.id}: {self.name}"


@dataclass
class TestPlan:
    raw: str
    url: str | None
    preamble: str  # everything before the first test case (credentials, setup, etc.)
    cases: list[TestCase] = field(default_factory=list)

    def filter(
        self,
        only: list[str] | None = None,
        skip: list[str] | None = None,
    ) -> TestPlan:
        """Return a new TestPlan with only/skip filters applied."""
        cases = self.cases
        if only:
            only_upper = {tc.upper() for tc in only}
            cases = [c for c in cases if c.id.upper() in only_upper]
        if skip:
            skip_upper = {tc.upper() for tc in skip}
            cases = [c for c in cases if c.id.upper() not in skip_upper]
        return TestPlan(
            raw=self.raw,
            url=self.url,
            preamble=self.preamble,
            cases=cases,
        )

    def unknown_ids(self, ids: list[str]) -> list[str]:
        """Return the IDs from `ids` that don't match any test case in the plan."""
        known = {c.id.upper() for c in self.cases}
        return [i for i in ids if i.upper() not in known]

    def to_markdown(self) -> str:
        """Reassemble the plan from preamble + selected cases."""
        parts = [self.preamble.rstrip()]
        for case in self.cases:
            parts.append(case.raw_markdown.rstrip())
        return "\n\n---\n\n".join(parts) + "\n"

    def split_chunks(self, n: int) -> list[TestPlan]:
        """Split test cases into n roughly equal chunks, each with the preamble."""
        if n <= 1 or len(self.cases) <= 1:
            return [self]
        chunks = []
        size = math.ceil(len(self.cases) / n)
        for i in range(0, len(self.cases), size):
            chunk_cases = self.cases[i : i + size]
            chunks.append(
                TestPlan(
                    raw=self.raw,
                    url=self.url,
                    preamble=self.preamble,
                    cases=chunk_cases,
                )
            )
        return chunks


# Regex to match test case headers like "### TC-001: Login with valid credentials"
_TC_HEADER = re.compile(
    r"^###\s+(TC-\d+)\s*:\s*(.+)$", re.MULTILINE
)

# Regex to extract priority/category from the body
_PRIORITY = re.compile(r"\*\*Priority:\*\*\s*(\w+)", re.IGNORECASE)
_CATEGORY = re.compile(r"\*\*Category:\*\*\s*(\w+)", re.IGNORECASE)


def parse_test_plan(text: str) -> TestPlan:
    """Parse a Markdown test plan into a TestPlan object."""
    # Find all TC headers and their positions
    matches = list(_TC_HEADER.finditer(text))

    if not matches:
        # No test cases found — whole thing is preamble
        return TestPlan(raw=text, url=_extract_url(text), preamble=text, cases=[])

    preamble = text[: matches[0].start()].rstrip()
    # Only look for the URL in the preamble, so "URL:" inside a test step isn't picked up
    url = _extract_url(preamble)

    cases: list[TestCase] = []
    for i, match in enumerate(matches):
        tc_id = match.group(1)
        tc_name = match.group(2).strip()

        # Extract body: from this header to the next header (or end)
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]

        # Strip trailing --- separators
        body = re.sub(r"\n---\s*$", "", body.rstrip())

        priority_match = _PRIORITY.search(body)
        category_match = _CATEGORY.search(body)

        cases.append(
            TestCase(
                id=tc_id,
                name=tc_name,
                raw_markdown=body,
                priority=priority_match.group(1).lower() if priority_match else "",
                category=category_match.group(1).lower() if category_match else "",
            )
        )

    return TestPlan(raw=text, url=url, preamble=preamble, cases=cases)


def _extract_url(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip().lstrip(">").strip()
        if stripped.lower().startswith("url:"):
            return stripped.split(":", 1)[1].strip()
    return None


def plan_from_scenario(scenario: str, url: str, name: str = "Scenario") -> TestPlan:
    """Wrap a single plain-English scenario into a one-case test plan."""
    text = (
        f"# Test Plan: {name}\n\n"
        f"> URL: {url}\n\n"
        f"## Test Cases\n\n"
        f"### TC-001: {name}\n\n"
        f"{scenario.strip()}\n"
    )
    return parse_test_plan(text)


def renumber(case: TestCase, new_id: str) -> TestCase:
    """A copy of the case with a new ID, including in its heading."""
    body = _TC_HEADER.sub(lambda m: f"### {new_id}: {m.group(2).strip()}", case.raw_markdown, count=1)
    return TestCase(new_id, case.name, body, priority=case.priority, category=case.category)


def compose_plan(title: str, url: str | None, cases: list[TestCase]) -> str:
    """Markdown for a plan made of the given cases, numbered TC-001 onwards."""
    head = f"# Test Plan: {title}\n\n" + (f"> URL: {url}\n\n" if url else "") + "## Test Cases\n"
    body = [renumber(c, f"TC-{i:03d}").raw_markdown.strip() for i, c in enumerate(cases, start=1)]
    return head + "".join(f"\n{b}\n\n---\n" for b in body)


def append_cases(plan_text: str, cases: list[TestCase]) -> str:
    """Append cases to an existing plan, numbering them after its highest TC ID."""
    existing = parse_test_plan(plan_text).cases
    next_num = max((int(c.id.split("-")[1]) for c in existing), default=0) + 1
    added = [renumber(c, f"TC-{next_num + i:03d}").raw_markdown.strip() for i, c in enumerate(cases)]
    return plan_text.rstrip() + "\n" + "".join(f"\n{b}\n\n---\n" for b in added)


# ── Structured editing ───────────────────────────────────────────────
# The web UI edits test cases as fields; Markdown stays the storage format.
# case_fields() and render_case() convert between the two. Text that doesn't fit
# a field is kept in "notes", so converting never loses anything.

_LABEL = re.compile(r"^\*\*(.+?):?\*\*:?\s*(.*)$")
_PLAIN_LABEL = re.compile(
    r"^(priority|category|preconditions?|before you start|steps|"
    r"expected results?|expected|acceptance criteria)\s*:\s*(.*)$",
    re.IGNORECASE,
)
_ITEM = re.compile(r"^(\d+[.)]|[-*•+]|\[[ xX]\])\s+(.*)$")
_SECTIONS = {
    "preconditions": "preconditions", "precondition": "preconditions", "before you start": "preconditions",
    "steps": "steps",
    "expected result": "expected", "expected results": "expected", "expected": "expected",
    "acceptance criteria": "expected",
}


def case_fields(case: TestCase) -> dict:
    """Split a test case into editable fields."""
    fields: dict = {
        "id": case.id, "title": case.name, "priority": "", "category": "",
        "preconditions": [], "steps": [], "expected": [], "notes": "",
    }
    notes: list[str] = []
    section: str | None = None
    for line in case.raw_markdown.splitlines()[1:]:
        text = line.strip()
        if not text or text == "---":
            continue
        label = _LABEL.match(text) or _PLAIN_LABEL.match(text)
        if label:
            name, rest = label.group(1).strip().lower(), label.group(2).strip()
            if name in ("priority", "category"):
                fields[name] = rest.lower()
                section = None
                continue
            if name in _SECTIONS:
                section = _SECTIONS[name]
                if rest:
                    fields[section].append(rest)
                continue
        item = _ITEM.match(text)
        if item:
            numbered = item.group(1)[0].isdigit()
            target = section or ("steps" if numbered else None)
            if target:
                fields[target].append(item.group(2).strip())
                continue
        if section and fields[section] and line[:1].isspace():
            fields[section][-1] += " " + text  # wrapped continuation of the previous item
            continue
        section = None
        notes.append(line.rstrip())
    fields["notes"] = "\n".join(notes).strip()
    return fields


def render_case(fields: dict, case_id: str) -> str:
    """Markdown for one test case from its fields."""
    title = " ".join(str(fields.get("title") or "Untitled test").split())
    out = [f"### {case_id}: {title}", ""]
    meta = [f"**{k.title()}:** {fields[k]}" for k in ("priority", "category") if fields.get(k)]
    if meta:
        out += [*meta, ""]

    def clean(items) -> list[str]:
        return [" ".join(str(i).split()) for i in items or [] if str(i).strip()]

    if pre := clean(fields.get("preconditions")):
        out += ["**Preconditions:**", *[f"- {p}" for p in pre], ""]
    if steps := clean(fields.get("steps")):
        out += ["**Steps:**", *[f"{n}. {s}" for n, s in enumerate(steps, 1)], ""]
    if expected := clean(fields.get("expected")):
        out += ["**Acceptance criteria:**", *[f"- {e}" for e in expected], ""]
    if notes := str(fields.get("notes") or "").strip():
        out += [notes, ""]
    return "\n".join(out).rstrip() + "\n"


def plan_fields(text: str) -> dict:
    """Split a plan into its title, URL, free-form notes (setup etc.), and test case fields."""
    plan = parse_test_plan(text)
    title = ""
    notes: list[str] = []
    for line in plan.preamble.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not title:
            title = stripped[2:].removeprefix("Test Plan:").strip()
        elif stripped.lstrip(">").strip().lower().startswith("url:") or stripped.lower() == "## test cases":
            continue
        else:
            notes.append(line.rstrip())
    return {
        "title": title,
        "url": plan.url,
        "notes": "\n".join(notes).strip(),
        "cases": [case_fields(c) for c in plan.cases],
    }


def render_plan(data: dict) -> str:
    """Markdown for a plan from plan_fields()-shaped data. Existing test IDs are kept;
    new cases (no ID, or a duplicate) get the next free number."""
    cases = data.get("cases") or []
    used = {str(c.get("id")).upper() for c in cases if c.get("id")}
    numbers = [int(i.split("-")[1]) for i in used if re.fullmatch(r"TC-\d+", i)]
    next_num = max(numbers, default=0) + 1
    seen: set[str] = set()
    blocks = []
    for fields in cases:
        case_id = str(fields.get("id") or "").upper()
        if not re.fullmatch(r"TC-\d+", case_id) or case_id in seen:
            case_id = f"TC-{next_num:03d}"
            next_num += 1
        seen.add(case_id)
        blocks.append(render_case(fields, case_id).strip())

    head = [f"# Test Plan: {data.get('title') or 'Untitled'}", ""]
    if data.get("url"):
        head += [f"> URL: {data['url']}", ""]
    if str(data.get("notes") or "").strip():
        head += [str(data["notes"]).strip(), ""]
    head += ["## Test Cases", ""]
    return "\n".join(head) + "\n" + "\n\n---\n\n".join(blocks) + "\n"
