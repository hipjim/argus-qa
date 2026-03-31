"""Terminal colors using ANSI escape codes. No dependencies."""

import os
import sys

# Disable colors if NO_COLOR is set or output isn't a terminal
_ENABLED = (
    os.environ.get("NO_COLOR") is None
    and hasattr(sys.stdout, "isatty")
    and sys.stdout.isatty()
)


def _code(c: str) -> str:
    return c if _ENABLED else ""


# ── Styles ───────────────────────────────────────────────────────
RESET = _code("\033[0m")
BOLD = _code("\033[1m")
DIM = _code("\033[2m")
ITALIC = _code("\033[3m")

# ── Foreground ───────────────────────────────────────────────────
RED = _code("\033[31m")
GREEN = _code("\033[32m")
YELLOW = _code("\033[33m")
BLUE = _code("\033[34m")
MAGENTA = _code("\033[35m")
CYAN = _code("\033[36m")
WHITE = _code("\033[37m")
GRAY = _code("\033[90m")

# ── Agent name colors (cycle through these for parallel agents) ──
AGENT_COLORS = [
    _code("\033[36m"),   # cyan
    _code("\033[33m"),   # yellow
    _code("\033[35m"),   # magenta
    _code("\033[32m"),   # green
    _code("\033[34m"),   # blue
    _code("\033[91m"),   # bright red
    _code("\033[96m"),   # bright cyan
    _code("\033[93m"),   # bright yellow
]

_agent_color_idx = 0
_agent_color_map: dict[str, str] = {}


def agent_color(name: str) -> str:
    """Get a consistent color for an agent name. Each agent gets a unique color."""
    global _agent_color_idx
    if name not in _agent_color_map:
        _agent_color_map[name] = AGENT_COLORS[_agent_color_idx % len(AGENT_COLORS)]
        _agent_color_idx += 1
    return _agent_color_map[name]


# ── Helpers ──────────────────────────────────────────────────────
def header(text: str) -> str:
    return f"{BOLD}{CYAN}{text}{RESET}"


def success(text: str) -> str:
    return f"{BOLD}{GREEN}{text}{RESET}"


def error(text: str) -> str:
    return f"{BOLD}{RED}{text}{RESET}"


def warn(text: str) -> str:
    return f"{BOLD}{YELLOW}{text}{RESET}"


def info(text: str) -> str:
    return f"{BLUE}{text}{RESET}"


def dim(text: str) -> str:
    return f"{DIM}{text}{RESET}"


def agent_line(name: str, text: str) -> str:
    """Format an agent's output line with colored name."""
    c = agent_color(name)
    return f"  {c}{BOLD}[{name}]{RESET} {DIM}>{RESET} {text}"


def phase(number: int, total: int, text: str) -> str:
    return f"\n{BOLD}{CYAN}[{number}/{total}]{RESET} {text}\n"


def result_bar(passed: int, failed: int, blocked: int, skipped: int) -> str:
    """Colored pass/fail summary bar."""
    total = passed + failed + blocked + skipped
    if total == 0:
        return dim("  No tests run.")
    parts = []
    if passed:
        parts.append(f"{GREEN}{BOLD}PASSED: {passed}{RESET}")
    if failed:
        parts.append(f"{RED}{BOLD}FAILED: {failed}{RESET}")
    if blocked:
        parts.append(f"{YELLOW}{BOLD}BLOCKED: {blocked}{RESET}")
    if skipped:
        parts.append(f"{DIM}SKIPPED: {skipped}{RESET}")
    return "  " + "  ".join(parts)


def banner(text: str) -> str:
    line = "=" * 60
    return f"\n{BOLD}{GREEN}{line}\n  {text}\n{line}{RESET}"
