"""Unit tests (no browser): element classification, page evidence, planner safety, stack-aware matching,
preferences and resume selection."""

from __future__ import annotations

import json

import httpx
import pytest

from app.ai.provider import OllamaProvider
from app.browser.forms import FormField, FormOption, fix_shared_labels
from app.matching.analyzer import JobInput, classify_title, normalize_title_aliases
from app.matching.rules import evaluate_rules, title_prescreen
from app.navigator.classify import (
    Kind,
    already_applied_evidence,
    classify_element,
    expired_evidence,
    is_application_field,
    success_evidence,
)
from app.navigator.observe import Element, FieldInfo, Snapshot
from app.navigator.planner import PageReasoner, build_prompt, candidate_elements
from app.resumes import select_resume


@pytest.mark.parametrize("text,kw,kind", [
    ("Apply now", {}, Kind.APPLY), ("Apply for this job", {}, Kind.APPLY), ("I'm interested", {}, Kind.APPLY),
    ("Apply on company site", {}, Kind.APPLY), ("Start application", {}, Kind.APPLY),
    ("Apply with LinkedIn", {}, Kind.DANGER), ("Sign in with Google", {}, Kind.SSO), ("Applied", {}, Kind.OTHER),
    ("Apply", {"form_context": True}, Kind.SUBMIT), ("Submit application", {"form_context": True}, Kind.SUBMIT),
    ("Save", {"form_context": True}, Kind.SUBMIT), ("Save", {}, Kind.DANGER), ("Save job", {}, Kind.DANGER),
    ("Next", {"form_context": True}, Kind.NEXT), ("Save and Continue", {"form_context": True}, Kind.NEXT),
    ("Review application", {"form_context": True}, Kind.NEXT), ("Apply as a guest", {}, Kind.GUEST),
    ("Apply Manually", {}, Kind.GUEST), ("Create account", {}, Kind.SIGNUP), ("Sign in", {}, Kind.SIGNIN),
    ("Accept all cookies", {"cookie_context": True}, Kind.COOKIES), ("No thanks", {}, Kind.CLOSE),
    ("Upload resume", {}, Kind.UPLOAD), ("Withdraw application", {}, Kind.DANGER), ("Log out", {}, Kind.DANGER),
    ("Go", {"type_": "submit", "form_context": True}, Kind.SUBMIT),
])
def test_classify_element(text, kw, kind):
    assert classify_element(text, **kw) == kind


def test_page_evidence():
    assert success_evidence("Thank you for applying! We will be in touch.")
    assert success_evidence("Your application has been submitted successfully")
    assert not success_evidence("Complete your application in 3 easy steps")
    assert already_applied_evidence("You have already applied for this job on 3 Oct")
    assert expired_evidence("Job you are looking for is expired. There are similar jobs below")
    assert expired_evidence("Sorry, this position has been filled.")
    assert not expired_evidence("We are hiring! Apply now")


def test_application_field_filter():
    assert is_application_field("First name")
    assert not is_application_field("Search jobs")
    assert not is_application_field("honeypot")
    assert not is_application_field("Leave this field blank")
    assert not is_application_field("Subscribe to job alerts")


def test_shared_section_labels_are_replaced():
    fields = fix_shared_labels([
        FormField(id="1", type="text", label="APPLY HERE", placeholder="First Name*", name="zfname"),
        FormField(id="2", type="select", label="APPLY HERE", name="exp",
                  options=[FormOption("Total Experience*", ""), FormOption("3-6", "3-6")]),
    ])
    assert [f.label for f in fields] == ["First Name*", "Total Experience*"] and all(f.required for f in fields)


# ---------------------------------------------------------------- planner safety

def _snap(elements: list[Element]) -> Snapshot:
    return Snapshot(url="https://careers.example.test/job/1", title="Web Engineer", text="We build things. Interested?",
                    headings=["Web Engineer"], elements=elements, fields=[], dialogs=[], password_fields=[], frames=[],
                    signature="s")


def test_planner_hides_dangerous_and_nav_elements():
    els = [Element(id="a1", frame=0, tag="a", text="About", ctx="nav"),
           Element(id="a2", frame=0, tag="button", text="Let's go!"),
           Element(id="a3", frame=0, tag="a", text="Sign in with Google", kind=Kind.SSO),
           Element(id="a4", frame=0, tag="a", text="Log out", kind=Kind.DANGER),
           Element(id="a5", frame=0, tag="a", text="Careers", ctx="nav")]
    ids = [e.id for e in candidate_elements(_snap(els))]
    assert ids == ["a2", "a5"]
    prompt = build_prompt(_snap(els), "Apply to the job", [], candidate_elements(_snap(els)))
    listed = prompt.split("CLICKABLE ELEMENTS:")[1].split("RECENT ACTIONS")[0]
    assert "[a2]" in listed and "Log out" not in listed and "Google" not in listed


async def test_planner_rejects_invented_ids():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "m"}]})
        out = {"page_type": "job_description", "action": "click", "element_id": "a99", "reason": "x", "confidence": 0.9}
        return httpx.Response(200, json={"message": {"content": json.dumps(out)}})

    provider = OllamaProvider(base_url="http://x", model="m", enabled=True, transport=httpx.MockTransport(handler))
    d = await PageReasoner(provider).decide(_snap([Element(id="a2", frame=0, tag="button", text="Let's go!")]),
                                            "goal", [])
    assert d is not None and d.action == "invalid" and d.element is None


# ---------------------------------------------------------------- matching & resumes

def _job(title: str, description: str = "", **kw) -> JobInput:
    return JobInput(title=title, description=description, location=kw.pop("location", "Bengaluru"),
                    experience_text=kw.pop("experience_text", "5-9 Yrs"), **kw)


JAVA_FS_JD = ("Java, Spring Boot, Microservices, Hibernate, Kafka, SQL. Build REST APIs. "
              "Some React for internal screens. Java 8+, Spring Boot required.")
REACT_AI_JD = ("Build React and Next.js interfaces for our LLM products: chat UIs, streaming responses, RAG search, "
               "AI agents dashboards with OpenAI and LangChain. TypeScript, Tailwind CSS, accessibility.")


def test_mixed_stack_titles(profile):
    assert classify_title("Java React Developer") == "fullstack"
    assert classify_title("Senior React Developer") == "frontend"
    assert normalize_title_aliases("Dot Net & React") == ".net & React"
    java = evaluate_rules(profile, _job("Java React Developer", JAVA_FS_JD))
    assert java.decision == "SKIP" and any("Backend-heavy" in b for b in java.blockers)
    py = evaluate_rules(profile, _job("React + Python Developer",
                                      "React, TypeScript, Redux frontend with some Python FastAPI services. "
                                      "Build UI components, accessibility, performance."))
    assert not any("Backend-heavy" in b for b in py.blockers)


def test_react_preferred_over_angular(profile):
    jd = "Frontend role. Build SPAs, components, CSS, accessibility, performance, TypeScript, design systems."
    react = evaluate_rules(profile, _job("Senior React Developer", jd + " React, Redux."))
    angular = evaluate_rules(profile, _job("Senior Angular Developer", jd + " Angular, RxJS."))
    assert react.score > angular.score
    assert "React/Next.js" in react.priority and "Angular" in angular.priority


def test_frontend_ai_role_uses_ai_resume(profile, tmp_path):
    result = evaluate_rules(profile, _job("Senior Frontend Engineer - AI", REACT_AI_JD))
    assert result.ai_focus and result.resume_category == "ai"
    ai = tmp_path / "ai.pdf"
    ai.write_bytes(b"%PDF-1.4")
    p = profile.model_copy(deep=True)
    p.resumes = [*p.resumes, type(p.resumes[0])(id="ai", path=str(ai), tags=["ai", "llm", "genai", "rag"])]
    assert select_resume(p, result.resume_category, result.matched_skills).id == "ai"
    assert select_resume(p, "frontend", []).id == "frontend"
    assert select_resume(p, "fullstack", []).id == "fullstack"


def test_title_prescreen(profile):
    assert title_prescreen(profile, "QA Automation Engineer")
    assert title_prescreen(profile, "Frontend Intern")
    assert not title_prescreen(profile, "Senior Frontend Engineer")
    assert not title_prescreen(profile, "UI Engineer")


def test_field_info_defaults():
    f = FieldInfo(field=FormField(id="x", type="text", label="Name"), frame=0)
    assert f.application
