"""Concrete ATS adapters: Greenhouse, Lever, Workday, SmartRecruiters.

Each adapter only declares platform knowledge (URLs, DOM signals, buttons, well-known field ids,
custom widgets). The step engine in ``ats/base.py`` does the rest. To support another ATS, subclass
``ATSAdapter``, declare its targets and add it to ``ATS_ADAPTERS`` in ``ats/registry.py``.
"""

from __future__ import annotations

import logging
import re

from playwright.async_api import Error as PlaywrightError, Page

from app.answers.question_bank import QuestionContext
from app.ats.base import ATSAdapter
from app.browser.forms import FormFiller
from app.browser.selectors import T
from app.engine.context import ApplyContext

log = logging.getLogger(__name__)


def _p(attr: str):
    return lambda ctx: getattr(ctx.profile.personal, attr) or None


class GreenhouseAdapter(ATSAdapter):
    name = "greenhouse"
    display_name = "Greenhouse"
    url_patterns = (r"boards\.greenhouse\.io", r"job-boards(?:\.eu)?\.greenhouse\.io", r"greenhouse\.io/embed",
                    r"[?&]gh_jid=")
    dom_signals = ("#grnhse_app", "iframe#grnhse_iframe", "form#application_form", "#application_form",
                   "#submit_app", "div#app_body", "a[href*='greenhouse.io']")
    open_targets = (
        T("greenhouse apply", css=["a#apply_button", "button#apply_button", "a[href='#app']"],
          role=("button", r"^\s*apply(?:\s+for\s+this\s+job)?\s*$"), text=["Apply for this job", "Apply"]),
    )
    form_root = T("greenhouse form", css=["form#application_form", "#application_form", "form#application-form",
                                         "#application", "form[action*='applications']"])
    submit_target = T("greenhouse submit", css=["#submit_app", "input#submit_app", "button[type=submit]"],
                      role=("button", r"submit\s+application"), text=["Submit Application", "Submit application"])
    success_url = r"/confirmation|/thank|confirmation=true"
    success_targets = (T(css=["#application_confirmation", ".application-confirmation",
                              "[data-testid='application-confirmation']"]),)
    fast_fields = (
        (T(css=["#first_name", "input[name='job_application[first_name]']"]), _p("first_name")),
        (T(css=["#last_name", "input[name='job_application[last_name]']"]), _p("last_name")),
        (T(css=["#email", "input[name='job_application[email]']"]), _p("email")),
        (T(css=["#phone", "input[name='job_application[phone]']"]), _p("phone")),
    )

    async def open_form(self, page: Page, ctx: ApplyContext) -> Page:
        # company career pages often embed the Greenhouse form in an iframe: open it top-level
        frame_src = await self.engine.attr_of(T(css=["iframe#grnhse_iframe", "iframe[src*='greenhouse.io']"]), "src",
                                              root=page)
        if frame_src:
            await self.engine.goto(frame_src, page=page)
            return page
        if await self.engine.exists(self.form_root, root=page):  # type: ignore[arg-type]
            return page
        return await super().open_form(page, ctx)


class LeverAdapter(ATSAdapter):
    name = "lever"
    display_name = "Lever"
    url_patterns = (r"jobs(?:\.eu)?\.lever\.co",)
    dom_signals = (".posting-page", "a.postings-btn", "div.application-page", "input[name='urls[LinkedIn]']",
                   "form[action*='lever.co']", "[data-qa='btn-apply-bottom']")
    open_targets = (
        T("lever apply", css=["a.postings-btn[href$='/apply']", "a[data-qa='btn-apply-bottom']",
                              "a.postings-btn.template-btn-submit"],
          text=["Apply for this job"]),
    )
    form_root = T("lever form", css=["form#application-form", "form[action*='apply']", ".application-form form",
                                     "div.application-page form"])
    submit_target = T("lever submit", css=["button#btn-submit", "button[data-qa='btn-submit']",
                                           "button.template-btn-submit[type=submit]"],
                      role=("button", r"submit\s+application"), text=["Submit application"])
    success_url = r"/thanks"
    success_targets = (T(css=["div.application-confirmation", "[data-qa='msg-submit-success']"],
                         within_text=["Application submitted!"]),)
    fast_fields = (
        (T(css=["input[name='name']"]), lambda c: c.profile.personal.full_name or None),
        (T(css=["input[name='email']"]), _p("email")),
        (T(css=["input[name='phone']"]), _p("phone")),
        (T(css=["input[name='org']"]), lambda c: c.profile.professional.current_company or None),
        (T(css=["input[name='urls[LinkedIn]']"]), _p("linkedin_url")),
        (T(css=["input[name='urls[GitHub]']"]), _p("github_url")),
        (T(css=["input[name='urls[Portfolio]']"]), _p("portfolio_url")),
        (T(css=["input[name='location']"]), lambda c: c.profile.personal.current_city or None),
    )


class WorkdayAdapter(ATSAdapter):
    name = "workday"
    display_name = "Workday"
    url_patterns = (r"myworkdayjobs\.com", r"myworkdaysite\.com", r"\.wd\d+\.myworkday", r"workday\.com/.+/job/")
    dom_signals = ("[data-automation-id='jobPostingHeader']", "[data-automation-id='adventureButton']",
                   "[data-automation-id='applyManually']", "[data-automation-id='bottom-navigation-next-button']")
    open_targets = (
        T("workday apply", css=["[data-automation-id='adventureButton']", "a[data-automation-id='applyButton']"],
          role=("button", r"^\s*apply\s*$"), text=["Apply"]),
        T("workday apply manually", css=["[data-automation-id='applyManually']"], text=["Apply Manually"]),
    )
    login_wall = T("workday sign in", css=["[data-automation-id='signInContent']", "[data-automation-id='createAccountLink']",
                                          "[data-automation-id='signInSubmitButton']", "form[data-automation-id='signInForm']"])
    form_root = T("workday form", css=["[data-automation-id='applyFlowPage']", "main", "form"])
    next_target = T("workday next", css=["[data-automation-id='bottom-navigation-next-button']:not(:has-text('Submit'))"],
                    role=("button", r"^\s*(save\s+and\s+continue|next|continue)\s*$"))
    submit_target = T("workday submit", css=["[data-automation-id='bottom-navigation-next-button']:has-text('Submit')"],
                      role=("button", r"^\s*submit\s*$"))
    success_targets = (T(css=["[data-automation-id='applicationSubmittedMessage']", "[data-automation-id='congratulationsPopup']"],
                         within_text=["Application Submitted", "Congratulations"]),)
    fast_fields = (
        (T(css=["[data-automation-id='legalNameSection_firstName']"]), _p("first_name")),
        (T(css=["[data-automation-id='legalNameSection_lastName']"]), _p("last_name")),
        (T(css=["[data-automation-id='email']"]), _p("email")),
        (T(css=["[data-automation-id='phone-number']"]), lambda c: c.profile.personal.phone_national or None),
        (T(css=["[data-automation-id='addressSection_city']"]), _p("current_city")),
        (T(css=["[data-automation-id='addressSection_postalCode']"]), _p("postal_code")),
    )

    async def prefill(self, page: Page, ctx: ApplyContext, filler: FormFiller) -> None:
        await super().prefill(page, ctx, filler)
        await self._fill_listbox_buttons(page, ctx)
        resume = page.locator("[data-automation-id='file-upload-input-ref']")
        try:
            if await resume.count() and ctx.resume_path and not await page.locator(
                    "[data-automation-id='file-upload-item']").count():
                await resume.first.set_input_files(str(ctx.resume_path))
                filler.mark_resume_handled()
                await ctx.event("resume", f"Uploaded {ctx.resume_path.name}")
        except PlaywrightError as exc:
            log.debug("workday resume upload: %s", exc)

    async def _fill_listbox_buttons(self, page: Page, ctx: ApplyContext) -> None:
        """Workday renders selects as buttons with aria-haspopup=listbox."""
        buttons = page.locator("button[aria-haspopup='listbox']")
        for i in range(await buttons.count()):
            btn = buttons.nth(i)
            try:
                if not await btn.is_visible():
                    continue
                current = (await btn.inner_text()).strip()
                if current and not re.match(r"^select\s+one$", current, re.I):
                    continue
                label = await btn.evaluate("""el => { const f = el.closest('[data-automation-id^="formField"]') || el.parentElement.parentElement;
                    const l = f && f.querySelector('label, legend'); return l ? l.innerText.trim() : (el.getAttribute('aria-label') || ''); }""")
                await btn.click()
                opts = page.locator("[role='listbox'] [role='option']:visible")
                await page.wait_for_timeout(300)
                texts = [((await opts.nth(k).inner_text()) or "").strip() for k in range(min(await opts.count(), 50))]
                ans = await ctx.answer(QuestionContext(question=label, field_type="select", options=texts,
                                                       required="*" in label, job=ctx.job.to_job_input(),
                                                       portal=ctx.job.portal))
                if ans.value and not ans.needs_review and ans.value in texts:
                    await opts.nth(texts.index(ans.value)).click()
                else:
                    await page.keyboard.press("Escape")
            except PlaywrightError as exc:
                log.debug("workday listbox: %s", exc)


class SmartRecruitersAdapter(ATSAdapter):
    name = "smartrecruiters"
    display_name = "SmartRecruiters"
    url_patterns = (r"jobs\.smartrecruiters\.com", r"careers\.smartrecruiters\.com", r"smartrecruiters\.com/.+/\d+")
    dom_signals = ("oc-oneclick-form", "[data-test='apply-button']", "#st-apply", "spl-button", "spl-input",
                   "[data-test='footer-submit']")
    open_targets = (
        T("smartrecruiters interested", css=["button[data-test='apply-button']", "a#st-apply", "#st-apply",
                                              "a.js-oneclick"],
          role=("button", r"i'?m\s+interested|apply\s+now"), text=["I'm interested", "Apply Now"]),
    )
    form_root = T("sr form", css=["oc-oneclick-form", "form", "main"])
    next_target = T("sr next", css=["button[data-test='footer-next']"], role=("button", r"^\s*next\s*$"))
    submit_target = T("sr submit", css=["button[data-test='footer-submit']"], role=("button", r"^\s*submit\s*$"))
    success_url = r"/confirmation|/success"
    success_targets = (T(css=["[data-test='confirmation']", "oc-confirmation"],
                         within_text=["Thank you for your application"]),)
    fast_fields = (
        (T(css=["#first-name-input"]), _p("first_name")),
        (T(css=["#last-name-input"]), _p("last_name")),
        (T(css=["#email-input"]), _p("email")),
        (T(css=["#confirm-email-input"]), _p("email")),
        (T(css=["#linkedin-input"]), _p("linkedin_url")),
        (T(css=["#phone-number-input"]), _p("phone")),
    )
