"""Registry of integration *kinds* (Gmail, Slack, GitHub, ...): the credential form shown in the UI and
an optional connection test. Tool modules register the kinds they consume:

    register_connector_kind(ConnectorKindSpec(
        kind="github", name="GitHub", description="Repositories, issues, pull requests, Actions",
        icon="github", category="engineering", tool_groups=["github"],
        fields=[ConnectorField("token", "Personal access token", type="password", required=True, secret=True)],
        test=_test_github,  # async (config: dict) -> tuple[bool, str]
    ))
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ConnectorField:
    key: str
    label: str
    type: str = "text"  # text | password | url | select | textarea | number
    required: bool = False
    secret: bool = False
    placeholder: str | None = None
    help: str | None = None
    options: list[str] | None = None


@dataclass
class ConnectorKindSpec:
    kind: str
    name: str
    description: str
    icon: str = "plug"
    category: str = "productivity"
    fields: list[ConnectorField] = field(default_factory=list)
    tool_groups: list[str] = field(default_factory=list)
    docs_url: str | None = None
    test: Callable[[dict[str, Any]], Awaitable[tuple[bool, str]]] | None = None

    def public(self) -> dict:
        d = asdict(self)
        d.pop("test", None)
        d["fields"] = [{k: v for k, v in asdict(f).items() if v is not None} for f in self.fields]
        return d

    @property
    def secret_keys(self) -> set[str]:
        return {f.key for f in self.fields if f.secret}


connector_kinds: dict[str, ConnectorKindSpec] = {}


def register_connector_kind(spec: ConnectorKindSpec) -> None:
    connector_kinds[spec.kind] = spec
