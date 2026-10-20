"""Shared input validators."""
from __future__ import annotations

from typing import Annotated

import email_validator
from email_validator import EmailNotValidError, validate_email
from pydantic import AfterValidator

# Self-hosted organisations often use internal mail domains (Active Directory "corp.local", staging "*.test").
# email-validator documents SPECIAL_USE_DOMAIN_NAMES as configurable; keep .invalid/.onion/.arpa/.localhost blocked.
for _name in ("local", "test"):
    if _name in email_validator.SPECIAL_USE_DOMAIN_NAMES:
        email_validator.SPECIAL_USE_DOMAIN_NAMES.remove(_name)


def _email(value: str) -> str:
    """Valid, normalised (lower-case) email. Internal/special-use domains (corp.local, *.internal) are allowed —
    self-hosted organisations commonly use them."""
    try:
        info = validate_email(value.strip(), check_deliverability=False, globally_deliverable=False)
    except EmailNotValidError as e:
        raise ValueError(str(e)) from e
    return info.normalized.lower()


Email = Annotated[str, AfterValidator(_email)]
