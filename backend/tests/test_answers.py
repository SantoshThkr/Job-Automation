"""Phase 8: application question answering from profile facts, memory, and AI with confidence + guard."""

from __future__ import annotations

import pytest

from app.ai.job_assistant import JobAssistant
from app.answers.answerer import Answerer
from app.answers.options import choose_option, parse_option_range
from app.answers.question_bank import QuestionContext
from app.matching.analyzer import JobInput
from tests.conftest import fake_ollama

JOB = JobInput(title="Senior Frontend Engineer", company="Razorpay", location="Bengaluru",
               description="Build React and TypeScript payment UIs. 5+ years of experience.")


def q(question, field_type="text", options=None, required=True, portal="linkedin", max_length=None):
    return QuestionContext(question=question, field_type=field_type, options=options or [], required=required,
                           job=JOB, portal=portal, max_length=max_length)


@pytest.fixture
def answerer(profile):
    return Answerer(profile, None, min_confidence=0.75)


@pytest.mark.parametrize("question,field_type,options,expected", [
    ("First Name", "text", None, "Asha"),
    ("Last name*", "text", None, "Verma"),
    ("Email address", "email", None, "asha.verma.dev@gmail.com"),
    ("Mobile phone number", "tel", None, "9812345678"),
    ("What is your email address?", "text", None, "asha.verma.dev@gmail.com"),
    ("LinkedIn Profile", "url", None, "https://www.linkedin.com/in/asha-verma-dev"),
    ("Current City", "text", None, "Bengaluru"),
    ("How many years of total experience do you have?", "number", None, "7"),
    ("Total Experience (in months)", "number", None, "84"),
    ("How many years of work experience do you have with React?", "number", None, "6"),
    ("How many years of experience do you have in TypeScript?", "text", None, "4"),
    ("Notice period (in days)", "number", None, "30"),
    ("What is your notice period?", "select", ["Immediate", "15 days or less", "1 Month", "2 Months", "3 Months"], "1 Month"),
    ("Current CTC (in Lakhs)", "number", None, "28"),
    ("Expected CTC (in lakhs per annum)", "text", None, "38"),
    ("What is your current CTC?", "text", None, "28 LPA"),
    ("Expected salary range", "select", ["10-20 LPA", "20-30 LPA", "30-40 LPA", "40+ LPA"], "30-40 LPA"),
    ("Are you willing to relocate?", "radio", ["Yes", "No"], "No"),
    ("Are you willing to relocate to Bengaluru?", "radio", ["Yes", "No"], "Yes"),
    ("Are you comfortable working from our Bengaluru office 3 days a week (hybrid)?", "radio", ["Yes", "No"], "Yes"),
    ("Will you now or in the future require sponsorship for employment visa status?", "select", ["Yes", "No"], "No"),
    ("Are you legally authorized to work in India?", "radio", ["Yes", "No"], "Yes"),
    ("Are you legally authorized to work in the United States?", "radio", ["Yes", "No"], "No"),
    ("Have you completed the following level of education: Bachelor's Degree?", "radio", ["Yes", "No"], "Yes"),
    ("Do you have experience with React?", "radio", ["Yes", "No"], "Yes"),
    ("How did you hear about us?", "select", ["LinkedIn", "Referral", "Company website"], "LinkedIn"),
    ("Gender", "select", ["Male", "Female", "Prefer not to say"], "Female"),
    ("Are you a protected veteran?", "select", ["I am a protected veteran", "I am not a protected veteran",
                                                 "I don't wish to answer"], "I don't wish to answer"),
    ("Years of experience", "select", ["0-2 years", "3-5 years", "6-8 years", "9+ years"], "6-8 years"),
    ("Are you at least 18 years of age?", "radio", ["Yes", "No"], "Yes"),
    ("Current Company", "text", None, "Acme Fintech"),
    ("What is your preferred pronoun?", "text", None, "she/her"),
])
async def test_known_questions(answerer, question, field_type, options, expected):
    a = await answerer.answer(q(question, field_type, options))
    assert a.value == expected, (a.value, a.note)
    assert not a.needs_review, a.note
    assert a.confidence >= 0.75


async def test_mobile_development_question_is_not_a_phone_field(answerer):
    a = await answerer.answer(q("How many years of mobile development experience do you have?", "number"))
    assert a.value != "9812345678"


async def test_skill_years_are_derived_from_work_history(answerer):
    # Redux years are not set in skills.years, but Pixel Labs (2017-07 .. 2021-03) lists Redux
    a = await answerer.answer(q("How many years of experience do you have with Redux?", "number"))
    assert a.value == "3.5" and a.mode == "derived" and not a.needs_review
    assert "work-history" in a.note


async def test_missing_fact_goes_to_manual_review(answerer):
    a = await answerer.answer(q("What is your date of birth?", "date"))
    assert a.value is None and a.needs_review and a.mode == "ask"


async def test_unknown_skill_is_not_invented(answerer):
    a = await answerer.answer(q("How many years of experience do you have with Kubernetes?", "number"))
    assert a.value == "0" and a.needs_review  # suggested but must be confirmed


async def test_optional_open_question_left_blank(answerer):
    a = await answerer.answer(q("Additional information", "textarea", required=False))
    assert a.value == "" and not a.needs_review


async def test_open_question_without_ai_is_written_from_profile(answerer):
    a = await answerer.answer(q("Why are you interested in this role?", "textarea"))
    assert a.value and not a.needs_review and a.mode == "composed"
    assert "Razorpay" in a.value and "React" in a.value and "job.description" in a.sources


async def test_unrecognised_open_question_is_not_answered_generically(answerer):
    a = await answerer.answer(q("Describe your tenure at your longest-running employer", "textarea"))
    assert a.value is None and a.needs_review  # no generic paragraph for a question it does not understand


async def test_about_yourself_uses_profile_summary(answerer, profile):
    a = await answerer.answer(q("Tell us about yourself", "textarea"))
    assert a.value == profile.professional.summary and a.source == "profile"


async def test_memory_takes_precedence(profile):
    memory = {"are you comfortable with night shifts": "No"}
    ans = Answerer(profile, None, memory_lookup=lambda nq: memory.get(nq))
    a = await ans.answer(q("Are you comfortable with night shifts?", "radio", ["Yes", "No"]))
    assert a.value == "No" and a.source == "memory" and not a.needs_review


async def test_ai_factual_answer_grounded(profile):
    provider = fake_ollama([{"answer": "Computer Science", "confidence": 0.95,
                             "profile_fields_used": ["education"], "needs_human": False}])
    ans = Answerer(profile, JobAssistant(provider))
    a = await ans.answer(q("What did you specialise in during your undergraduate studies?", "text"))
    assert a.value == "Computer Science" and a.source == "ai"
    assert a.confidence <= 0.85


async def test_ai_says_needs_human(profile):
    provider = fake_ollama([{"answer": "", "confidence": 0, "profile_fields_used": [], "needs_human": True}])
    ans = Answerer(profile, JobAssistant(provider))
    a = await ans.answer(q("What is your passport number?", "text"))
    assert a.value is None and a.needs_review


async def test_ai_hallucinated_number_is_rejected(profile):
    # the model invents "12 years" which is not in the profile -> rejected / guard lowers confidence -> review
    bad = {"answer": "12 years at Google", "confidence": 0.95, "profile_fields_used": ["work_history"],
           "needs_human": False, "sources": ["professional.work_history[0]"], "requires_user": False}
    provider = fake_ollama([bad] * 6)
    ans = Answerer(profile, JobAssistant(provider))
    a = await ans.answer(q("Describe your tenure at your longest-running employer", "text"))
    assert a.needs_review
    assert "guard" in a.note


async def test_ai_written_motivation_answer(profile):
    text = ("I enjoy building fast, accessible React and TypeScript interfaces, and Razorpay's focus on "
            "payment experiences matches the design system and performance work I do today.")
    provider = fake_ollama([{"answer": text, "confidence": 0.9}])
    ans = Answerer(profile, JobAssistant(provider))
    a = await ans.answer(q("Why are you interested in this role?", "textarea"))
    assert a.value == text and not a.needs_review, a.note


async def test_ai_written_answer_with_invented_employer_is_replaced_by_grounded_text(profile):
    text = "At Google and Netflix I led 40 engineers building Kubernetes platforms."
    provider = fake_ollama([{"answer": text, "confidence": 0.95, "sources": [], "requires_user": False}])
    ans = Answerer(profile, JobAssistant(provider))
    a = await ans.answer(q("Why are you interested in this role?", "textarea"))
    assert a.value and "Google" not in a.value and "Netflix" not in a.value
    assert a.mode == "composed" and not a.needs_review


async def test_ai_malformed_response_needs_review(profile):
    provider = fake_ollama(["<think>hmm</think> sure! here you go: answer=yes", "nope"])
    ans = Answerer(profile, JobAssistant(provider))
    a = await ans.answer(q("Do you hold an active security clearance?", "text"))
    assert a.value is None and a.needs_review


async def test_ai_unavailable_needs_review(profile):
    ans = Answerer(profile, JobAssistant(fake_ollama([], tags_ok=False)))
    a = await ans.answer(q("What is your blood group?", "text"))
    assert a.value is None and a.needs_review


def test_option_helpers():
    assert parse_option_range("3-5 years") == (3, 5)
    assert parse_option_range("More than 10 years")[0] == 10
    assert parse_option_range("Less than 1 year") == (0, 1)
    assert choose_option("7", ["0-2", "3-5", "5-8", "8+"], numeric=7) == "5-8"
    assert choose_option("Yes", ["Select an option", "Yes", "No"], yes_no=True) == "Yes"
    assert choose_option("Bengaluru", ["Bengaluru, Karnataka, India", "Mumbai"]) == "Bengaluru, Karnataka, India"
    assert choose_option("Something else", ["A", "B"]) is None


async def test_optional_unknown_questions_are_not_guessed(profile):
    provider = fake_ollama([{"answer": "Acme Fintech", "confidence": 0.9, "profile_fields_used": ["current_company"],
                             "needs_human": False}])
    ans = Answerer(profile, JobAssistant(provider))
    a = await ans.answer(q("Follow company", "checkbox", required=False))
    assert a.value == "" and not a.needs_review
    assert provider.calls == []  # the AI was not consulted


async def test_checkbox_answers_must_be_yes_or_no(profile):
    provider = fake_ollama([{"answer": "Acme Fintech", "confidence": 0.9, "profile_fields_used": ["current_company"],
                             "needs_human": False}])
    ans = Answerer(profile, JobAssistant(provider))
    a = await ans.answer(q("I hold a valid driving licence", "checkbox", required=True))
    assert a.needs_review


async def test_how_many_days_of_notice_is_a_number(answerer, profile):
    """LinkedIn (run #18): 'How many days is your notice period?' got 'Immediate' and LinkedIn said 'Invalid input'."""
    days = profile.professional.notice_period_days
    a = await answerer.answer(q("How many days is your notice period?"))
    assert a.value == str(days)


@pytest.mark.parametrize("question", [
    "How many years of professional experience do you have in full-stack development?",  # LinkedIn run #18 (said 7)
    "How many years of work experience do you have with User Interface Design?",  # LinkedIn run #18 (said 7)
    "How many years of experience do you have in mobile app development?",
    "How many years of work experience do you have with Databases?",  # LinkedIn run #20 (said 7)
])
async def test_years_in_a_discipline_the_profile_does_not_record_are_asked(answerer, question):
    a = await answerer.answer(q(question))
    assert a.value in (None, "") or a.needs_review, a


async def test_state_management_is_not_the_state_you_live_in(answerer, profile):
    a = await answerer.answer(q("Years of experience in state management?"))
    assert a.value != profile.personal.state


@pytest.mark.parametrize("question", ["How many years of work experience do you have with Software Development?",
                                      "How many years of work experience do you have with Front-End Development?"])
async def test_general_areas_still_use_your_years(answerer, profile, question):
    a = await answerer.answer(q(question))
    assert a.value is not None and float(a.value) > 0
