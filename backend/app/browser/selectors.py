"""Robust element targeting: each Target lists several strategies that are tried in order.

Portals change their markup frequently, so adapters describe *what* they want (an apply button)
with several independent strategies (ids, attribute fragments, visible text, ARIA role) instead of
depending on one brittle CSS selector.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Frame, Locator, Page


@dataclass(frozen=True)
class Target:
    name: str = ""
    css: tuple[str, ...] = ()
    text: tuple[str, ...] = ()  # exact-ish visible text of a clickable element
    role: tuple[tuple[str, str], ...] = ()  # (aria role, accessible-name regex)
    label: tuple[str, ...] = ()
    placeholder: tuple[str, ...] = ()
    xpath: tuple[str, ...] = ()
    within_text: tuple[str, ...] = field(default=())  # css:has-text() style fragments

    def strategies(self, root: Page | Frame | Locator) -> list[tuple[str, Locator]]:
        out: list[tuple[str, Locator]] = []
        for c in self.css:
            out.append((f"css={c}", root.locator(c)))
        for role, name in self.role:
            out.append((f"role={role}[{name}]", root.get_by_role(role, name=re.compile(name, re.I))))  # type: ignore[arg-type]
        for t in self.text:
            rx = re.compile(rf"^\s*{re.escape(t)}\s*$", re.I)
            out.append((f"text={t}", root.locator("button, a, [role=button], input[type=submit], span, div[role=button]")
                        .filter(has_text=rx)))
        for lbl in self.label:
            out.append((f"label={lbl}", root.get_by_label(re.compile(lbl, re.I))))
        for ph in self.placeholder:
            out.append((f"placeholder={ph}", root.get_by_placeholder(re.compile(ph, re.I))))
        for xp in self.xpath:
            out.append((f"xpath={xp}", root.locator(f"xpath={xp}")))
        for frag in self.within_text:
            out.append((f"has-text={frag}", root.locator(f"text={frag}")))
        return out

    def describe(self) -> str:
        return self.name or ", ".join(
            [*self.css[:2], *(f"text:{t}" for t in self.text[:2]), *(f"role:{r}" for r, _ in self.role[:1])])


def _tup(v: Any) -> tuple:
    if not v:
        return ()
    if isinstance(v, str):
        return (v,)
    return tuple(v)


def T(name: str = "", *, css: Any = (), text: Any = (), role: Any = (), label: Any = (), placeholder: Any = (),
      xpath: Any = (), within_text: Any = ()) -> Target:
    """Convenience constructor accepting a string or a list for every strategy.

    ``role`` accepts a single ("button", "name regex") pair or a list of pairs.
    """
    roles: tuple = ()
    if role:
        roles = (tuple(role),) if isinstance(role, tuple) and isinstance(role[0], str) else tuple(tuple(r) for r in role)
    return Target(name=name, css=_tup(css), text=_tup(text), role=roles, label=_tup(label),
                  placeholder=_tup(placeholder), xpath=_tup(xpath), within_text=_tup(within_text))
