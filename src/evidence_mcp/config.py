"""Configuration, read from environment variables and an optional .env file.

The GitHub token is a secret: it is only ever read from the environment/.env,
never hardcoded, and it is excluded from repr() so it cannot leak into logs.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# GitHub owner / repository names become part of an API URL path, so only the
# characters GitHub itself allows are accepted.
_GITHUB_NAME = re.compile(r"[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class Settings:
    evidence_root: Path
    max_file_size_bytes: int
    http_timeout_seconds: float
    max_redirects: int
    github_owner: str = ""
    github_repo: str = ""
    github_branch: str = "main"
    # repr=False keeps the token out of repr(settings), so printing or logging
    # the settings object can never reveal it.
    github_token: str = field(default="", repr=False)

    @property
    def github_configured(self) -> bool:
        return bool(self.github_token and self.github_owner and self.github_repo)


def load_settings(env_file: Path | None = None) -> Settings:
    """Build Settings from the environment, using .env to fill in missing values.

    Relative paths resolve against the current working directory. The Claude
    Desktop config starts the server with `uv --directory <project>`, so that
    is the project folder.
    """
    # override=False: a variable already set in the real environment wins over .env.
    load_dotenv(env_file or Path.cwd() / ".env", override=False)

    evidence_root = Path(os.getenv("EVIDENCE_ROOT", "./evidence_store")).expanduser().resolve()
    max_mb = _read_number("MAX_FILE_SIZE_MB", default=20, minimum=0.001)
    timeout = _read_number("HTTP_TIMEOUT_SECONDS", default=10, minimum=0.1)
    max_redirects = int(_read_number("MAX_REDIRECTS", default=3, minimum=0))

    return Settings(
        evidence_root=evidence_root,
        max_file_size_bytes=int(max_mb * 1024 * 1024),
        http_timeout_seconds=timeout,
        max_redirects=max_redirects,
        github_owner=_read_github_name("GITHUB_OWNER"),
        github_repo=_read_github_name("GITHUB_REPO"),
        github_branch=os.getenv("GITHUB_BRANCH", "").strip() or "main",
        github_token=os.getenv("GITHUB_TOKEN", "").strip(),
    )


def _read_number(name: str, default: float, minimum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        # Name the variable but do not echo its value.
        raise ValueError(f"{name} must be a number") from None
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _read_github_name(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value and (not _GITHUB_NAME.fullmatch(value) or value in {".", ".."}):
        raise ValueError(f"{name} may only contain letters, digits, '-', '_' and '.'")
    return value
