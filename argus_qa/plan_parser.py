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
