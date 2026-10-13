"""Ask you for the required questions a form filler could not answer, fill your answers in, remember them.

Shared by every form-based flow (company-site navigator, known ATS adapters, LinkedIn Easy Apply), so a
question you answer once in the dashboard is filled automatically on every later application.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.answers.options import choice_labels
from app.answers.question_bank import Answer, QuestionContext
from app.browser.forms import FilledField, FormFiller, Root
from app.engine.context import ApplyContext, GateResult

log = logging.getLogger(__name__)


@dataclass
class AskOutcome:
    answered: bool  # True: continue with the form (answers filled, or you completed it in the browser)
    skipped_by_you: bool = False
    still_missing: list[str] = field(default_factory=list)


def question_payload(items: list[FilledField]) -> list[dict]:
    return [{
        "id": i.field.id, "question": i.question or i.field.label, "type": i.field.type,
        "options": choice_labels(i.field.option_labels)[:40], "required": i.field.required,
        "suggested": i.answer.value if i.answer and i.answer.value else "", "note": i.note,
    } for i in items]


async def ask_and_fill(ctx: ApplyContext, page, root: Root, filler: FormFiller,
                       items: list[FilledField]) -> AskOutcome:
    """Ask once for ``items`` (unresolved required fields), fill the answers into ``root``, remember them."""
    labels = [i.question or i.field.label for i in items]
    if not items:
        return AskOutcome(True)
    if ctx.mode.value == "dry_run":
        return AskOutcome(False, still_missing=labels)
    answers = await ctx.ask_questions(question_payload(items), page)
    if answers is None:
        return AskOutcome(False, skipped_by_you=ctx.last_gate == GateResult.REJECT, still_missing=labels)
    if not answers:  # you completed the fields directly in the browser
        return AskOutcome(True)
    missing: list[str] = []
    for i in items:
        value = answers.get(i.field.id)
        label = i.question or i.field.label
        if not value:
            missing.append(label)
            continue
        loc = root.locator(i.field.selector).first
        if not await filler.set_value(loc, i.field, value, root=root):
            missing.append(label)
            continue
        verified, problem = await filler.verify(loc, i.field, value, root)
        if problem:
            await ctx.event("form_rejected", f"The form did not accept your answer to '{label[:80]}': {problem}",
                            level="warning")
            missing.append(label)
            continue
        i.status, i.verified = "filled", verified
        await ctx.save_user_answer(label, value)
        if ctx.record_answer is not None:
            q = QuestionContext(question=label, field_type=i.field.type, options=i.field.option_labels,
                                required=i.field.required, job=ctx.job.to_job_input(), portal=ctx.portal)
            await ctx.record_answer(i.field, q, Answer(value, 1.0, "user", note="answered by you", mode="user",
                                                       sources=["you"], verified=verified))
    return AskOutcome(True, still_missing=missing)
