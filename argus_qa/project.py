"""Projects: reusable configuration for testing one app.

A project holds the target URL, test accounts (by role), test data, secrets,
and standing instructions for the tester agent. The CLI reads it from an
`argus.toml` file; the server stores projects via its /projects API. Both use
the same shape:

    name = "Looma"
    url = "https://staging.looma.example"
    instructions = "Dismiss the cookie banner. Never delete real data."

    [credentials.admin]
    username = "qa-admin@looma.example"
    password = "${LOOMA_ADMIN_PASSWORD}"   # expanded from the environment
    notes = "Full access"

    [variables]
    search_term = "running shoes"

    [secrets]
    test_card = "${LOOMA_TEST_CARD}"

Plans can reference values as {{admin.username}}, {{admin.password}},
{{search_term}}, or {{test_card}}.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_FILE = "argus.toml"
MASK = "********"
REDACTED = "[redacted]"

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")
_FIELDS = {"name", "slug", "url", "description", "instructions", "credentials", "variables", "secrets"}
_CREDENTIAL_FIELDS = {"username", "password", "notes"}
# Secrets shorter than this aren't redacted from output; they'd mangle unrelated text
_MIN_REDACT_LEN = 4


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63] or "project"


@dataclass
class Credential:
    username: str = ""
    password: str = ""
    notes: str = ""


@dataclass
class Project:
    name: str
    slug: str = ""
    url: str | None = None
    description: str = ""
    instructions: str = ""
    credentials: dict[str, Credential] = field(default_factory=dict)
    variables: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.slug:
            self.slug = slugify(self.name)
        if not _SLUG.match(self.slug):
            raise ValueError(f"Invalid project slug {self.slug!r}: use lowercase letters, digits, dashes.")
        for kind, keys in (("credential role", self.credentials), ("variable", self.variables),
                           ("secret", self.secrets)):
            for key in keys:
                if not _KEY.match(key):
                    raise ValueError(f"Invalid {kind} name {key!r}: use letters, digits, '_' and '-'.")
        clash = set(self.variables) & set(self.secrets)
        if clash:
            raise ValueError(f"Names used as both variable and secret: {', '.join(sorted(clash))}")

    # ── (de)serialization ─────────────────────────────────────

    @classmethod
    def from_dict(cls, data: dict) -> Project:
        if not isinstance(data, dict):
            raise ValueError("Project must be an object.")
        unknown = set(data) - _FIELDS
        if unknown:
            raise ValueError(f"Unknown project fields: {', '.join(sorted(unknown))}")
        if not data.get("name"):
            raise ValueError("Project needs a `name`.")

        credentials = {}
        for role, cred in (data.get("credentials") or {}).items():
            if not isinstance(cred, dict):
                raise ValueError(f"Credential {role!r} must be a table with username/password/notes.")
            bad = set(cred) - _CREDENTIAL_FIELDS
            if bad:
                raise ValueError(f"Unknown fields in credential {role!r}: {', '.join(sorted(bad))}")
            credentials[role] = Credential(**{k: str(v) for k, v in cred.items()})

        return cls(
            name=str(data["name"]),
            slug=str(data.get("slug") or ""),
            url=str(data["url"]) if data.get("url") else None,
            description=str(data.get("description") or ""),
            instructions=str(data.get("instructions") or ""),
            credentials=credentials,
            variables=_str_dict(data.get("variables"), "variables"),
            secrets=_str_dict(data.get("secrets"), "secrets"),
        )

    def to_dict(self, mask_secrets: bool = False) -> dict:
        def secret(value: str) -> str:
            return MASK if mask_secrets and value else value

        return {
            "name": self.name,
            "slug": self.slug,
            "url": self.url,
            "description": self.description,
            "instructions": self.instructions,
            "credentials": {
                role: {"username": c.username, "password": secret(c.password), "notes": c.notes}
                for role, c in self.credentials.items()
            },
            "variables": dict(self.variables),
            "secrets": {k: secret(v) for k, v in self.secrets.items()},
        }

    def keep_masked_secrets(self, previous: Project) -> Project:
        """Where this (updated) project still has the mask placeholder, keep the previous secret.

        Lets clients GET a project, edit it, and PUT it back without resending secrets.
        """
        data = self.to_dict()
        for role, cred in data["credentials"].items():
            if cred["password"] == MASK and role in previous.credentials:
                cred["password"] = previous.credentials[role].password
        for key, value in data["secrets"].items():
            if value == MASK and key in previous.secrets:
                data["secrets"][key] = previous.secrets[key]
        return Project.from_dict(data)

    # ── runtime ───────────────────────────────────────────────

    def resolve_env(self, environ: dict[str, str] | None = None) -> Project:
        """Return a copy with ${VAR} references expanded from the environment."""
        env = os.environ if environ is None else environ
        missing: set[str] = set()

        def expand(value: str) -> str:
            def repl(m: re.Match) -> str:
                if m.group(1) not in env:
                    missing.add(m.group(1))
                    return m.group(0)
                return env[m.group(1)]

            return _ENV_REF.sub(repl, value)

        data = self.to_dict()
        data["url"] = expand(data["url"]) if data["url"] else None
        for cred in data["credentials"].values():
            cred["username"] = expand(cred["username"])
            cred["password"] = expand(cred["password"])
        data["variables"] = {k: expand(v) for k, v in data["variables"].items()}
        data["secrets"] = {k: expand(v) for k, v in data["secrets"].items()}
        if missing:
            raise ValueError(
                f"Project {self.slug!r} references unset environment variables: {', '.join(sorted(missing))}"
            )
        return Project.from_dict(data)

    def secret_values(self) -> list[str]:
        values = [c.password for c in self.credentials.values()] + list(self.secrets.values())
        return [v for v in values if v]

    def lookup(self, name: str) -> str | None:
        """Resolve a {{placeholder}} name: role.username / role.password / variable / secret."""
        if "." in name:
            role, _, attr = name.partition(".")
            cred = self.credentials.get(role)
            if cred is not None and attr in ("username", "password"):
                return getattr(cred, attr)
            return None
        if name in self.variables:
            return self.variables[name]
        return self.secrets.get(name)

    def prompt_section(self) -> str:
        """Markdown describing the project for an agent's prompt."""
        lines = [f"## Project: {self.name}"]
        if self.description:
            lines += ["", self.description]
        if self.credentials:
            lines += ["", "### Test accounts",
                      "Use these accounts whenever a test needs to log in. Tests refer to them by role."]
            for role, c in self.credentials.items():
                entry = f"- **{role}**: username `{c.username}`, password `{c.password}`"
                if c.notes:
                    entry += f" ({c.notes})"
                lines.append(entry)
        data = {**self.variables, **self.secrets}
        if data:
            lines += ["", "### Test data"]
            lines += [f"- **{k}**: `{v}`" for k, v in data.items()]
        if self.instructions:
            lines += ["", "### Standing instructions", "Always follow these, in every test:", "",
                      self.instructions.strip()]
        return "\n".join(lines) + "\n"


def substitute(text: str, project: Project | None) -> str:
    """Replace {{placeholders}} in a plan with project values. Unknown names are an error."""
    names = {m.group(1) for m in _PLACEHOLDER.finditer(text)}
    if not names:
        return text
    if project is None:
        raise ValueError(
            f"The plan uses placeholders ({', '.join('{{' + n + '}}' for n in sorted(names))}) "
            "but no project was given."
        )
    unknown = sorted(n for n in names if project.lookup(n) is None)
    if unknown:
        raise ValueError(
            f"Project {project.slug!r} has no value for: {', '.join('{{' + n + '}}' for n in unknown)}"
        )
    return _PLACEHOLDER.sub(lambda m: project.lookup(m.group(1)) or "", text)


class Redactor:
    """Removes secret values from text and JSON-like data before it's printed or saved."""

    def __init__(self, secrets: list[str] | None = None):
        self.values = sorted({s for s in secrets or [] if len(s) >= _MIN_REDACT_LEN}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for value in self.values:
            text = text.replace(value, REDACTED)
        return text

    def data(self, obj):
        if not self.values:
            return obj
        if isinstance(obj, str):
            return self(obj)
        if isinstance(obj, list):
            return [self.data(v) for v in obj]
        if isinstance(obj, dict):
            return {k: self.data(v) for k, v in obj.items()}
        return obj


def load_project_file(path: str | Path) -> Project:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"Invalid TOML in {path}: {e}") from e
    return Project.from_dict(data)


def find_project_file(directory: Path | None = None) -> Path | None:
    candidate = (directory or Path.cwd()) / PROJECT_FILE
    return candidate if candidate.is_file() else None


def _str_dict(value, what: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"`{what}` must be a table of name = value.")
    for k, v in value.items():
        if isinstance(v, dict | list):
            raise ValueError(f"{what}.{k} must be a plain value.")
    return {str(k): str(v) for k, v in value.items()}

