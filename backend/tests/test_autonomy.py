"""Routine questions are answered from the profile instead of being left for the user.

Every case here is a question that stopped a real application (reworded, with the synthetic test candidate:
Asha Verma, 7 years, Bengaluru, Acme Fintech since 2021-04 and Pixel Labs before, 28 -> 38 LPA, 30 days notice).
The rule is always the same: a profile fact or a safe derivation is used, whatever the wording; a fact the
profile does not hold is asked once - never guessed.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.answers.answerer import Answerer, canonical_key, fact_key, skill_years_key
from app.answers.intent import ASK_IF_MISSING, Category, classify
from app.answers.question_bank import QuestionContext, match_position, repair_label, repair_words, role_key
from app.matching.analyzer import JobInput

JOB = JobInput(title="Senior React Developer", company="Nimbus Works", location="Pune",
               description="React, TypeScript and Redux. Build web applications.")


def q(question, field_type="text", options=None, required=True, job=JOB, portal="naukri"):
    return QuestionContext(question=question, field_type=field_type, options=options or [], required=required,
                           job=job, portal=portal)


@pytest.fixture
def engine(profile):
    return Answerer(profile.model_copy(deep=True), None, min_confidence=0.75)


def answered(a) -> bool:
    return a.value not in (None, "") and not a.needs_review


# ------------------------------------------------------------------ the label as the form spells it

@pytest.mark.parametrize("raw,readable", [
    ("Notice - Period", "notice period"), ("mobilephone", "mobile phone"), ("D.O.B", "dob"), ("current_ctc", "current ctc"),
    ("emailAddress *", "email address"), ("Date of Birth", "date of birth"), ("C.T.C.", "ctc"),
    # ordinary words and sentences are left alone
    ("JavaScript", "javascript"), ("Statement of purpose", "statement of purpose"), ("U.S. work visa", "u.s. work visa"),
    ("What is your notice period?", "what is your notice period"), ("Website", "website"),
    ("10 - 15 LPA expected", "10 - 15 lpa expected"),
])
def test_label_repair(raw, readable):
    assert repair_label(raw) == readable


def test_label_repair_keeps_the_question_mark_for_intent():
    assert repair_words("Thanks! What is your notice - period?").endswith("?")
    assert classify("Thank you! What is your notice - period?").category == Category.NOTICE_PERIOD


async def test_differently_spelled_labels_get_the_profile_fact(engine):
    notice = await engine.answer(q("Notice - Period", "select", ["Immediate", "< 15 Days", "15 - 30 Days", "30 - 60 Days",
                                                                "60 - 90 Days"]))
    assert answered(notice) and notice.value == "15 - 30 Days", (notice.value, notice.note)
    phone = await engine.answer(q("mobilephone"))
    assert answered(phone) and phone.value == "9812345678"
    ctc = await engine.answer(q("current_ctc"))
    assert answered(ctc) and ctc.value == "28 LPA"
    first = await engine.answer(q("firstName"))
    assert answered(first) and first.value == "Asha"


# ------------------------------------------------------------------ skill years: every wording, never the total

@pytest.mark.parametrize("question", [
    "How many years have you worked with React?", "React.js experience", "Years of professional React experience",
    "How long have you used React in production?", "How many years of experience do you have in ReactJS?",
    "React (years)",
])
async def test_react_years_in_every_wording(engine, question):
    a = await engine.answer(q(question))
    assert answered(a) and a.value == "6", (question, a.value, a.note)  # skills.years['React'] = 6, total = 7


@pytest.mark.parametrize("question", ["Total experience", "Total years of experience", "Overall work experience (years)"])
async def test_total_experience_is_the_total(engine, question):
    a = await engine.answer(q(question))
    assert answered(a) and a.value == "7", (question, a.value)


@pytest.mark.parametrize("question", [
    "How many years of experience do you have with Kubernetes?", "How long have you used Terraform in production?",
    "How many years of work experience do you have with Relational Databases?",
    "How many years of work experience do you have as Full Stack Developer?",
])
async def test_years_of_something_not_in_the_profile_are_never_the_total(engine, question):
    a = await engine.answer(q(question))
    assert not answered(a) and a.value != "7", (question, a.value, a.note)


async def test_how_long_without_a_skill_is_not_an_experience_question(engine):
    a = await engine.answer(q("How long have you been working on your side projects?"))
    assert not answered(a)


# ------------------------------------------------------------------ the opening you are applying to

@pytest.mark.parametrize("title,options,expected", [
    ("Senior React Developer", ["Full Stack Developer", ".NET Developer", "Python Developer", "React Developer"], "React Developer"),
    ("React . JS Developer", ["PHP Developer", "Java Developer", "Android Developer", "React JS Developer"], "React JS Developer"),
    ("Sr. ReactJS Engineer (Remote)", ["React Developer", "React Native Developer"], "React Developer"),
    ("Frontend Engineer - React", ["Angular Developer", "React JS Developer", "UI/UX Designer"], "React JS Developer"),
    ("Frontend Developer", ["Frontend Developer", "Backend Developer", "Digital Marketing", "SEO Executive"], "Frontend Developer"),
    ("UI Developer", ["Frontend Developer", "Backend Developer", "QA Engineer"], "Frontend Developer"),
    ("AI Engineer", ["AI Engineer", "Field Sales Executive"], "AI Engineer"),
])
def test_position_is_matched_to_the_posting(title, options, expected):
    hit = match_position(title, options)
    assert hit is not None and hit[0] == expected, hit


@pytest.mark.parametrize("title,options", [
    ("React Developer", ["AI Engineer", "Field Sales Executive"]),
    ("React . JS Developer", ["Python Developer", "Senior PHP Developer"]),
    ("Software Engineer", ["PHP Developer", "Java Developer"]),  # 'developer' alone is not a match
    ("React Developer", ["React Developer (Pune)", "React Developer (Remote)", "React Native Developer"][1:] + ["React Lead"]),
])
def test_position_is_not_guessed_when_no_opening_is_clearly_it(title, options):
    hit = match_position(title, options)
    assert hit is None or role_key(hit[0]) == role_key(title), hit


@pytest.mark.parametrize("label", ["Select Position", "apply", "Job Role", "Select Positions", "Position applied for",
                                   "Select Job Profile Frontend Developer React Developer Digital Marketing"])
async def test_a_list_of_openings_is_answered_with_the_posting(engine, label):
    a = await engine.answer(q(label, "select", ["--Apply for--", "Frontend Developer", "React Developer", "Digital Marketing"]))
    assert answered(a) and a.value == "React Developer", (label, a.value, a.note)


async def test_a_list_without_the_opening_is_asked_not_guessed(profile):
    from app.ai.job_assistant import JobAssistant
    from tests.conftest import fake_ollama

    provider = fake_ollama([{"answer": "Python Developer", "confidence": 0.99}] * 3)
    engine = Answerer(profile, JobAssistant(provider), min_confidence=0.75)
    a = await engine.answer(q("Select Position", "select", ["Select", "Python Developer", "Senior PHP Developer"]))
    assert not answered(a) and a.value is None and "none of the listed openings" in a.note
    assert provider.calls == []  # recognised: the local AI is never asked to pick another job for you


async def test_technology_dropdown_of_roles(engine):
    a = await engine.answer(q("technology", "select", ["PHP Developer", "Java Developer", "iOS Developer", "React JS Developer"]))
    assert answered(a) and a.value == "React JS Developer"


async def test_applying_for_text_and_subject(engine):
    a = await engine.answer(q("Applying for"))
    assert answered(a) and a.value == "Senior React Developer"
    s = await engine.answer(q("Subject"))
    assert answered(s) and s.value == "Application for Senior React Developer"
    current = await engine.answer(q("Designation"))
    assert current.value == "Senior Frontend Engineer"  # your current title is still your current title


# ------------------------------------------------------------------ compensation, start date

async def test_compensation_wordings(engine):
    cur = await engine.answer(q("What is your Current Overall Compensation per Annum?"))
    assert answered(cur) and cur.value == "2800000"
    exp = await engine.answer(q("What is your Expected overall Compensation per Annum?", "textarea"))
    assert answered(exp) and exp.value.startswith("38 LPA")


@pytest.mark.parametrize("question,why", [
    ("What is your current monthly fixed/base salary in USD?", ""),
    ("What is your expected salary in USD?", "INR"),
    ("Please mention your current monthly salary", ""),  # in-hand pay is not CTC / 12
])
async def test_other_currencies_and_monthly_pay_are_never_computed(engine, question, why):
    a = await engine.answer(q(question))
    assert not answered(a) and why in a.note, (a.value, a.note)


async def test_date_available_follows_the_notice_period(engine):
    start = date.today() + timedelta(days=30)
    a = await engine.answer(q("Date Available"))
    assert answered(a) and a.value == start.isoformat()
    b = await engine.answer(q("Date available (DD/MM/YYYY)"))
    assert b.value == start.strftime("%d/%m/%Y")
    c = await engine.answer(q("Available from", "date"))
    assert c.value == start.isoformat()


# ------------------------------------------------------------------ written from the profile

async def test_bio_and_skills_boxes(engine, profile):
    bio = await engine.answer(q("Bio", "textarea"))
    assert answered(bio) and bio.value == profile.professional.summary
    skills = await engine.answer(q("Relevant Skills React js, HTML 5, JavaScript/ Typescript"))
    assert answered(skills)
    listed = [s.strip() for s in skills.value.split(",")]
    assert listed[:4] == ["React", "HTML", "JavaScript", "TypeScript"] or set(listed[:4]) == {"React", "HTML", "JavaScript", "TypeScript"}
    assert all(s in profile.skills.all for s in listed)  # nothing the profile does not list
    both = await engine.answer(q("Key skills & achievements", "textarea"))
    assert answered(both) and both.value.startswith("Key skills: ") and "Achievements: " in both.value
    assert profile.professional.highlights[0].rstrip(".") in both.value


async def test_why_company_by_name_is_a_why_company_question():
    assert classify("Why Technum Opus?", field_type="textarea").category == Category.WHY_COMPANY
    assert classify("Why do you want to work here?", field_type="textarea").category == Category.WHY_COMPANY


# ------------------------------------------------------------------ referral, previous employer

async def test_referral_questions(engine):
    a = await engine.answer(q("Did a current Nimbus Works employee refer you to this role?", "select", ["Select...", "Yes", "No"]))
    assert answered(a) and a.value == "No"
    b = await engine.answer(q("If yes, please share the name of the employee who referred you. (If No, type N/A)"))
    assert answered(b) and b.value == "N/A"
    c = await engine.answer(q("Referred by"))  # a required name box with no way to say "nobody": asked
    assert not answered(c)


@pytest.mark.parametrize("question,field_type,options", [
    ("Have you previously worked with us?", "text", None),
    ("Have you previously, or currently work for Nimbus Works?", "select", ["Select...", "Yes", "No"]),
    ("Are you a current or former employee of Nimbus Works?", "radio", ["Yes", "No"]),
])
async def test_previous_employer_is_no_only_when_the_history_is_declared_complete(profile, question, field_type, options):
    unset = await Answerer(profile, None, min_confidence=0.75).answer(q(question, field_type, options))
    assert not answered(unset) and "work_history_complete" in unset.note  # never assumed; says how to allow it
    declared = profile.model_copy(deep=True)
    declared.professional.work_history_complete = True
    a = await Answerer(declared, None, min_confidence=0.75).answer(q(question, field_type, options))
    assert answered(a) and a.value == "No" and "declared complete" in a.note


async def test_an_employer_in_the_history_is_yes(engine):
    job = JobInput(title="Frontend Engineer", company="Acme Fintech Pvt Ltd", location="Bengaluru")
    a = await engine.answer(q("Have you previously worked with us?", "radio", ["Yes", "No"], job=job))
    assert answered(a) and a.value == "Yes"


async def test_worked_with_a_technology_or_a_kind_of_team_is_not_an_employer_question(profile):
    declared = profile.model_copy(deep=True)
    declared.professional.work_history_complete = True
    engine = Answerer(declared, None, min_confidence=0.75)
    skill = await engine.answer(q("Have you worked with React?", "radio", ["Yes", "No"]))
    assert skill.rule != "h_previous_employment" and skill.value == "Yes"
    teams = await engine.answer(q("Have you worked with agile teams?", "radio", ["Yes", "No"]))
    assert not answered(teams)  # "agile teams" is not a company: 'No' would be a false statement


# ------------------------------------------------------------------ skills: yes / no and select-all

async def test_skill_yes_no_with_more_words(engine):
    a = await engine.answer(q("Do you have professional React frontend experience?", "select", ["Select...", "Yes", "No"]))
    assert answered(a) and a.value == "Yes"


async def test_select_all_that_apply_ticks_only_profile_skills(engine):
    a = await engine.answer(q("Which of the following do you have hands-on experience with? (Select all that apply)",
                              "checkbox_group", ["AWS", "React", "Terraform", "TypeScript, JavaScript", "None of the above"]))
    assert answered(a) and a.choices == ["React", "TypeScript, JavaScript"]
    assert a.value == "React; TypeScript, JavaScript"


async def test_select_all_never_ticks_none_of_the_above_for_you(engine):
    a = await engine.answer(q("Which of the following have you used professionally? (Select all that apply)", "checkbox_group",
                              ["Background jobs", "Retry logic", "Idempotency", "None of the above"]))
    assert not answered(a) and a.value is None


def test_multi_choice_values_are_split_on_semicolons_first():
    from app.answers.options import split_choices

    assert split_choices("React; TypeScript, JavaScript") == ["React", "TypeScript, JavaScript"]
    assert split_choices("React, Redux") == ["React", "Redux"]
    assert split_choices("React") == ["React"]
    assert split_choices("") == []


def test_an_option_that_contains_a_comma_is_one_choice():
    """Naukri (run #23): 'Please select the city...' offered 'Gurugram, Haryana'; the answer was split at the comma,
    no option matched 'Gurugram', and the application was parked ("Could not select 'Gurugram, Haryana'")."""
    from app.answers.options import split_choices

    cities = ["Gurugram, Haryana", "Pune, Maharashtra", "Noida, Uttar Pradesh", "Skip this question"]
    assert split_choices("Gurugram, Haryana", cities) == ["Gurugram, Haryana"]
    assert split_choices("gurugram, haryana", cities) == ["gurugram, haryana"]
    assert split_choices("Gurugram, Haryana, Pune, Maharashtra", cities) == ["Gurugram, Haryana", "Pune, Maharashtra"]
    assert split_choices("React, Pune, Maharashtra, Redux", cities) == ["React", "Pune, Maharashtra", "Redux"]
    assert split_choices("Gurugram, Haryana") == ["Gurugram", "Haryana"]  # without the form's options: plain list


# ------------------------------------------------------------------ location, shifts

@pytest.mark.parametrize("question,expected", [
    ("Are you okay with Office Location i.e. Koramangala, Bengaluru?", "Yes"),
    ("Locations : Bangalore, Pune, Noida", "Yes"),  # one of them is a preferred city
    ("Are you okay with Office Location i.e. NIBM Road, Pune?", "No"),  # not preferred, not willing to relocate
])
async def test_location_acceptance(engine, question, expected):
    a = await engine.answer(q(question, "radio", ["Yes", "No"]))
    assert answered(a) and a.value == expected, (a.value, a.note)


async def test_location_acceptance_follows_willingness_to_relocate(profile):
    p = profile.model_copy(deep=True)
    p.work_preferences.willing_to_relocate = True
    a = await Answerer(p, None, min_confidence=0.75).answer(
        q("Are you okay with Office Location i.e. NIBM Road, Pune?", "radio", ["Yes", "No"]))
    assert answered(a) and a.value == "Yes"


async def test_shift_questions_use_the_profile_and_are_asked_when_it_is_silent(profile):
    question = q("This role requires working US-based hours. Are you able to work night shifts?", "radio", ["Yes", "No"])
    silent = await Answerer(profile, None, min_confidence=0.75).answer(question)
    assert not answered(silent) and "work_preferences.open_to_shifts" in silent.note
    p = profile.model_copy(deep=True)
    p.work_preferences.open_to_shifts = False
    a = await Answerer(p, None, min_confidence=0.75).answer(question)
    assert answered(a) and a.value == "No"


# ------------------------------------------------------------------ your fixed answers, things only you know

async def test_a_fixed_answer_that_is_not_among_the_choices_does_not_block(profile):
    from app.profile import CustomAnswer

    p = profile.model_copy(deep=True)
    p.custom_answers.append(CustomAnswer(pattern="how did you hear", answer="A friend told me"))
    engine = Answerer(p, None, min_confidence=0.75)
    chosen = await engine.answer(q("How did you hear about us?", "select", ["Select", "LinkedIn", "Naukri.com", "Company website"]))
    assert answered(chosen) and chosen.value == "Naukri.com"  # applying through Naukri
    typed = await engine.answer(q("How did you hear about us?"))
    assert typed.value == "A friend told me" and typed.mode == "custom"  # where it can be typed, your wording is used


@pytest.mark.parametrize("question", ["D.O.B", "Age", "Hometown City", "PAN Number", "Enter verification code digit 1 of six.",
                                      "Blood group"])
async def test_personal_facts_without_a_profile_field_are_never_guessed(profile, question):
    from app.ai.job_assistant import JobAssistant
    from tests.conftest import fake_ollama

    provider = fake_ollama([{"answer": "1 January 1990", "confidence": 0.99}] * 4)
    a = await Answerer(profile, JobAssistant(provider), min_confidence=0.75).answer(q(question))
    assert not answered(a) and a.mode == "ask", (question, a.value)
    assert classify(question).category in ASK_IF_MISSING
    assert provider.calls == []  # the local AI was not even asked


# ------------------------------------------------------------------ one answer for every wording (keys)

@pytest.mark.parametrize("a,b", [
    ("D.O.B", "Date of Birth (DD/MM/YYYY)"), ("Reason for change", "Why are you looking for a job change?"),
    ("Reason for leaving current company", "Reasons for job switch"), ("Hometown City", "Native place"),
    ("10th Percentage", "SSC marks (%)"), ("12th Percentage", "Higher Secondary percentage"),
    ("Please mention your current monthly salary", "Current monthly in-hand salary"),
    ("How many years of work experience do you have as Full Stack Developer?",
     "How many years of professional experience do you have as a full-stack engineer, owning features end to end?"),
    ("How many years of experience do you have in Kubernetes?", "How long have you used Kubernetes in production?"),
])
def test_equivalent_wordings_share_one_key(a, b):
    assert canonical_key(a) is not None and canonical_key(a) == canonical_key(b), (canonical_key(a), canonical_key(b))


@pytest.mark.parametrize("question", [
    "Total experience", "What is your current monthly fixed/base salary in USD?", "Why do you want to join us?",
    "How many years of experience do you have with React and Node.js?", "10th board", "Current CTC",
])
def test_questions_that_are_not_the_same_fact_have_no_shared_key(question):
    assert canonical_key(question) is None, canonical_key(question)


def test_tenth_and_twelfth_are_different_facts():
    assert fact_key("10th Percentage") != fact_key("12th Percentage")
    assert fact_key("Secondary school percentage") == "fact: 10th percentage"
    assert fact_key("Senior secondary percentage") == "fact: 12th percentage"
    assert skill_years_key("Overall experience in years") is None


# ------------------------------------------------------------------ wordings that look alike but ask something else

async def test_shift_to_a_city_is_relocation_not_shift_work(profile):
    p = profile.model_copy(deep=True)
    p.work_preferences.willing_to_relocate = True
    engine = Answerer(p, None, min_confidence=0.75)
    a = await engine.answer(q("Are you willing to shift to Pune?", "radio", ["Yes", "No"]))
    assert answered(a) and a.value == "Yes" and "shift work" not in a.note
    b = await engine.answer(q("Are you open to working in rotational shifts?", "radio", ["Yes", "No"]))
    assert not answered(b) and "open_to_shifts" in b.note  # this one is about shift work: not in the profile


async def test_office_questions_still_follow_your_on_site_preference(profile):
    p = profile.model_copy(deep=True)
    p.work_preferences.open_to_onsite = False
    engine = Answerer(p, None, min_confidence=0.75)
    for question in ("Are you comfortable working from the office in Bengaluru?",
                     "Are you okay with Office Location i.e. Koramangala, Bengaluru?"):
        a = await engine.answer(q(question, "radio", ["Yes", "No"]))
        assert answered(a) and a.value == "No", (question, a.value, a.note)


async def test_a_bare_skills_tick_list_and_skills_you_do_not_have(engine):
    ticks = await engine.answer(q("Skills", "checkbox_group", ["React", "Angular", "TypeScript", "Kotlin"]))
    assert answered(ticks) and ticks.choices == ["React", "TypeScript"]
    wish = await engine.answer(q("Skills you want to learn next"))
    assert not answered(wish)  # not a list of the skills you have
    pick_one = await engine.answer(q("Skills", "select", ["Select", "Beginner", "Intermediate", "Expert"]))
    assert not answered(pick_one)  # a level, not a skill: left for you


async def test_portfolio_link_falls_back_to_github_but_a_personal_website_does_not(profile):
    p = profile.model_copy(deep=True)
    p.personal.portfolio_url = ""
    engine = Answerer(p, None, min_confidence=0.75)
    a = await engine.answer(q("Resume / Portfolio Link", "url"))
    assert answered(a) and a.value == p.personal.github_url and a.sources == ["personal.github_url"]
    site = await engine.answer(q("Personal website", "url"))
    assert not answered(site)  # a code host is not your website
    both = await Answerer(profile, None, min_confidence=0.75).answer(q("Portfolio", "url"))
    assert both.value == profile.personal.portfolio_url and both.sources == ["personal.portfolio_url"]


async def test_worked_with_a_tool_written_with_a_slash_is_a_skill_question(engine):
    """LinkedIn (run #19): 'Have you worked with Docker/containerization?' was read as "worked with <employer>" and
    asked with the note "'<the hiring company>' is not in your work history"."""
    a = await engine.answer(q("Have you worked with React/Next.js?", "radio", ["Yes", "No"]))
    assert answered(a) and a.value == "Yes" and a.rule != "h_previous_employment"


@pytest.mark.parametrize("question", ["In how many days you can join ?", "How many days will you need to join?",
                                      "Days required to join", "What is the lead time to join? - Please mention in days"])
async def test_days_to_join_is_the_notice_period(engine, question):
    a = await engine.answer(q(question))
    assert answered(a) and a.value in {"30", "30 days"}, (question, a.value, a.note)
    n = await engine.answer(q(question, "number"))
    assert answered(n) and n.value == "30"


async def test_a_skill_you_do_not_list_is_zero_only_when_the_list_is_declared_complete(profile):
    years = q("How many years of work experience do you have with Docker?")
    yes_no = q("Do you have experience with Kotlin?", "radio", ["Yes", "No"])
    unset = Answerer(profile, None, min_confidence=0.75)
    a = await unset.answer(years)
    assert not answered(a) and a.value == "0" and "skills.complete" in a.note  # suggested, never stated for you
    assert not answered(await unset.answer(yes_no))
    declared = profile.model_copy(deep=True)
    declared.skills.complete = True
    engine = Answerer(declared, None, min_confidence=0.75)
    b = await engine.answer(years)
    assert answered(b) and b.value == "0" and "declared complete" in b.note
    c = await engine.answer(yes_no)
    assert answered(c) and c.value == "No"
    listed = await engine.answer(q("How many years of experience do you have with Redux?"))
    assert listed.value != "0"  # a skill you DO list without years is still yours to state
    topic = await engine.answer(q("How many years of work experience do you have with Relational Databases?"))
    assert not answered(topic)  # not a skill name at all: never answered 0 for you


async def test_preferred_city_for_a_job_in_one_of_your_cities_is_that_city(profile):
    """SmartRecruiters (run #29): 'In which city would you prefer to work?' on a job in Pune."""
    question = q("In which city would you prefer to work?", "combobox")
    elsewhere = await Answerer(profile, None, min_confidence=0.75).answer(question)  # Pune is not one of the test cities
    assert answered(elsewhere) and elsewhere.value == "Bengaluru"
    p = profile.model_copy(deep=True)
    p.work_preferences.preferred_locations.append("Pune")
    a = await Answerer(p, None, min_confidence=0.75).answer(question)
    assert answered(a) and a.value == "Pune" and "preferred" in a.note
    typed = await Answerer(p, None, min_confidence=0.75).answer(q("Current city"))
    assert typed.value == "Bengaluru"  # where you live is still where you live


@pytest.mark.parametrize("option,years", [
    ("7 Years (7.0)", 7.0), ("6 Years 6 Months (6.6)", 6.5), ("0 Year 2 Months (0.2)", 2 / 12), ("1 Year 1 Month (1.1)", 1 + 1 / 12),
    ("7 Years 11 Months (7.11)", 7 + 11 / 12), ("Fresher", 0.0),
])
def test_year_and_month_options_are_exact_lengths(option, years):
    from app.answers.options import parse_option_range

    lo, hi = parse_option_range(option)
    assert abs(lo - years) < 1e-9 and lo == hi


def test_experience_is_matched_among_hundreds_of_year_month_options():
    """intellohire (run #35): 612 entries; text left in the filter box made the widget take its first one, 'Fresher'."""
    from app.answers.options import choose_option

    options = ["Fresher"] + [f"{y} Year{'s' if y != 1 else ''}" + (f" {m} Month{'s' if m != 1 else ''}" if m else "") + f" ({y}.{m})"
                             for y in range(0, 31) for m in range(0, 12) if y or m]
    assert choose_option("7", options, numeric=7.0) == "7 Years (7.0)"
    assert choose_option("6.5", options, numeric=6.5) == "6 Years 6 Months (6.6)"
    assert choose_option("0", options, numeric=0.0) == "Fresher"


async def test_preferred_locations_from_a_list(profile):
    question = q("Looking for jobs in", "checkbox_group", ["Chennai", "Bengaluru", "Remote", "Pune", "Bengaluru Rural",
                                                           "Bangalore, Karnataka"])
    a = await Answerer(profile, None, min_confidence=0.75).answer(question)
    assert answered(a) and a.choices == ["Bengaluru", "Remote", "Bangalore, Karnataka"]  # yours - not "Bengaluru Rural"
    only_all = q("Looking for jobs in", "checkbox_group", ["Pan india"])
    stay = await Answerer(profile, None, min_confidence=0.75).answer(only_all)
    assert not answered(stay) and "none of your preferred locations" in stay.note  # not willing to relocate: asked
    p = profile.model_copy(deep=True)
    p.work_preferences.willing_to_relocate = True
    move = await Answerer(p, None, min_confidence=0.75).answer(only_all)
    assert answered(move) and move.value == "Pan india" and "willing to relocate" in move.note


@pytest.mark.parametrize("raw,readable", [("Immedaite Joiner only", "immediate joiner only"),
                                          ("How many total year of experince you have with React?", "how many total year of experience you have with react")])
def test_recruiters_misspellings_are_read_as_meant(raw, readable):
    assert repair_label(raw) == readable


@pytest.mark.parametrize("raw,readable", [
    ("What's your total IT Exp", "what's your total it experience"), ("Exp in React", "experience in react"),
    ("Rel. Exp", "relevant experience"), ("5+ yrs exp in JS", "5+ yrs experience in js"),
    ("Exp CTC", "expected ctc"), ("Exp. salary", "expected salary"), ("Exp DOJ", "expected doj"),
    # not an abbreviation of either: an expiry date, a word that merely starts with the letters
    ("Passport exp date", "passport exp date"), ("Explain your role", "explain your role"), ("Expertise", "expertise"),
])
def test_exp_is_read_as_recruiters_mean_it(raw, readable):
    assert repair_label(raw) == readable


@pytest.mark.parametrize("short,full", [
    ("What's your total IT Exp", "What is your total experience?"),  # LinkedIn Easy Apply, run #45
    ("How much exp do you have in Typescript", "How many years of experience do you have in TypeScript?"),  # run #45
    ("Total exp", "Total experience"), ("Overall Exp (in years)", "Total experience"),
    ("Exp in React", "How many years of experience do you have in React?"),
    ("Exp CTC", "Expected CTC"),
])
async def test_questions_written_with_exp_get_the_same_answer_as_the_full_wording(engine, short, full):
    a, b = await engine.answer(q(short)), await engine.answer(q(full))
    assert answered(b), full
    assert answered(a) and a.value == b.value, (short, a.note)


async def test_exp_in_a_skill_you_do_not_list_is_still_asked(engine):
    a = await engine.answer(q("Exp in Kotlin"))
    assert not answered(a)  # never your total years, never an invented number


async def test_immediate_joiner_only_follows_the_notice_period(profile):
    """Naukri chat (run #36): 'Immedaite Joiner only' - a statement with a typo, answered from the notice period."""
    p = profile.model_copy(deep=True)
    p.professional.notice_period_days = 0
    now = await Answerer(p, None, min_confidence=0.75).answer(q("Immedaite Joiner only"))
    assert answered(now) and now.value == "Yes"
    later = await Answerer(profile, None, min_confidence=0.75).answer(q("Immediate joiner only"))  # 30 days in the test profile
    assert answered(later) and later.value.startswith("No") and "30 days" in later.value
    chips = await Answerer(p, None, min_confidence=0.75).answer(q("Immediate joiners only?", "radio", ["Yes", "No"]))
    assert answered(chips) and chips.value == "Yes"
    typo_years = await Answerer(profile, None, min_confidence=0.75).answer(q("How many total year of experince you have with React?"))
    assert answered(typo_years) and typo_years.value == "6"


@pytest.mark.parametrize("question", [
    "This Position is contract role for 3 months are you looking for contract role ?",  # LinkedIn Easy Apply, run #48
    "Are you open to contract-to-hire positions?", "Are you comfortable with a 6 month contractual role?",
])
async def test_contract_roles_follow_your_stated_preference_and_are_asked_without_one(profile, question):
    ask = await Answerer(profile, None, min_confidence=0.75).answer(q(question, "radio", ["Yes", "No"]))
    assert not answered(ask) and "work_preferences.open_to_contract" in ask.note  # never assumed
    for stated, expected in ((True, "Yes"), (False, "No")):
        p = profile.model_copy(deep=True)
        p.work_preferences.open_to_contract = stated
        a = await Answerer(p, None, min_confidence=0.75).answer(q(question, "radio", ["Yes", "No"]))
        assert answered(a) and a.value == expected


async def test_the_terms_of_a_contract_are_not_the_contract_preference(profile):
    p = profile.model_copy(deep=True)
    p.work_preferences.open_to_contract = True
    engine = Answerer(p, None, min_confidence=0.75)
    for question in ("Are you willing to work on W2? Strictly NO C2C candidates with an Employer.",
                     "What is your expected hourly rate?", "What is your expected contract rate?"):
        a = await engine.answer(q(question))
        assert not (answered(a) and a.value in {"Yes", "No"} and "open_to_contract" in (a.note or "")), question
    w2 = await engine.answer(q("Are you willing to work on W2? Strictly NO C2C candidates with an Employer.", "radio", ["Yes", "No"]))
    assert not answered(w2)  # a tax / legal arrangement: yours to answer
