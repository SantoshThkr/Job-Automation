"""Navigator: a general browser agent that completes job applications on sites it has never seen.

It does not follow a fixed script. Every step it observes the page (forms, buttons, dialogs,
frames, blockers), classifies what kind of page it is, decides the next action with rules first
and the local LLM as a constrained fallback, acts, and verifies that the page actually changed.
Irreversible clicks always pass the Dry Run / Review / Auto submit gate.
"""

from app.navigator.agent import ApplicationNavigator, navigate_application

__all__ = ["ApplicationNavigator", "navigate_application"]
