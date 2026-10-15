"""A labelled regression dataset for the job-decision rules (synthetic postings - no real employer text).

Each case says what a correct decision is *for the synthetic test profile* (tests/fixtures/profile_test.json: a
senior React / Next.js / TypeScript engineer, 7 years, Node.js as a secondary skill, Bengaluru or remote):

* ``relevant``   - the role is the profile's work: it must not be skipped (APPLY expected unless ``review_ok``);
* ``unsuitable`` - not the profile's work: it must never be APPLY (SKIP or a decision for you are both safe);
* ``ambiguous``  - too little to judge: it must be left to you (MANUAL_REVIEW).

The cases reproduce what the 2026-10-15 audit of run 76 found (an "AI Engineer" title skipped unread, React + Node
full-stack roles called backend-heavy, a WordPress / Shopify role and a freelance designer's role queued, an employer's
"25 years of experience" read as a requirement) next to the rules that were already right.

Limitations: the postings are short and written to test one rule each; real descriptions are longer, mix several
signals and use wording these do not. A measured rate here says the rules behave as intended on these patterns, not
how many real jobs are misjudged - that comes from audits of stored runs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    key: str
    label: str  # relevant | unsuitable | ambiguous
    title: str
    description: str
    location: str = "Bengaluru"
    experience_text: str = ""
    review_ok: bool = False  # a relevant case for which "yours to decide" is also correct
    why: str = ""


_REACT_JD = ("We are looking for a senior frontend engineer.\nRequirements:\n- 5+ years building web applications with React and "
             "TypeScript\n- Next.js, Redux, HTML, CSS, responsive design, accessibility\n- Jest and React Testing Library\n- "
             "REST APIs and GraphQL, web performance\nYou will own the UI architecture of our product.\n")

CASES: list[Case] = [
    # ------------------------------------------------------------------ relevant
    Case("frontend-senior", "relevant", "Senior Frontend Engineer", _REACT_JD * 2, experience_text="5-8 Yrs"),
    Case("react-dev", "relevant", "React Developer", _REACT_JD * 2, experience_text="5-9 Yrs"),
    Case("nextjs-dev", "relevant", "Next.js Developer", _REACT_JD.replace("senior frontend engineer", "Next.js developer") * 2,
         experience_text="4-8 Yrs"),
    Case("ui-engineer", "relevant", "UI Engineer",
         ("Build accessible, responsive interfaces in React and TypeScript. Requirements: 5+ years of JavaScript, HTML, CSS, "
          "design systems, Storybook-style component libraries, web performance and accessibility.\n") * 3),
    Case("frontend-analytics", "relevant", "Senior Frontend Developer (Analytics)", _REACT_JD * 2,
         why="'Analytics' names the product domain, not a data role"),
    Case("fullstack-react-node", "relevant", "Full Stack Engineer (React + Node.js)",
         ("Requirements:\n- 5+ years of React and TypeScript on the frontend\n- Node.js and Express services, REST APIs\n"
          "- Node.js microservices, Express middleware, REST API design, Node.js performance\n- PostgreSQL or MongoDB\n"
          "- CI/CD, Docker, AWS deployments\nYou will build features end to end.\n") * 2,
         why="backend in the profile's own stack (Node.js): not backend-heavy"),
    Case("generic-react-heavy", "relevant", "Software Engineer", _REACT_JD * 2,
         why="a generic title judged by its React-heavy description"),
    Case("ai-engineer-frontend", "relevant", "AI Engineer",
         ("Build the React and TypeScript interfaces of our LLM products: chat UIs, streaming responses, Next.js app router, "
          "RAG-backed search in the browser. Requirements: 5+ years of React, TypeScript, Next.js, HTML, CSS; experience with "
          "LLM APIs and generative AI features.\n") * 3, review_ok=True,
         why="an 'AI Engineer' title must be read, not skipped as a data role"),
    Case("optional-java", "relevant", "Frontend Engineer",
         _REACT_JD * 2 + "Nice to have: some exposure to Java services.\n", why="an optional backend skill is not mandatory"),
    Case("employer-years", "relevant", "Senior Frontend Engineer",
         "Acme Labs is a fintech company with over 25 years of experience in payments.\n" + _REACT_JD * 2,
         why="the employer's 25 years are not a requirement"),

    # ------------------------------------------------------------------ unsuitable
    Case("java-fullstack", "unsuitable", "Java Full Stack Developer (React)",
         ("Must have: Java, Spring Boot, Hibernate, microservices, SQL. React for the admin UI.\n") * 4),
    Case("django-fullstack", "unsuitable", "Full Stack Engineer",
         ("Requirements:\n- Python and Django REST framework, Celery, PostgreSQL, Redis, Kafka\n- Django ORM, Django "
          "migrations, data pipelines, AWS\n- Some React for internal tools\n") * 3),
    Case("data-engineer", "unsuitable", "Data Engineer", "Spark, Airflow, SQL pipelines, data warehouses.\n" * 5),
    Case("qa-automation", "unsuitable", "QA Automation Engineer", "Selenium, test plans, regression suites, Java.\n" * 5),
    Case("devops", "unsuitable", "DevOps Engineer", "Kubernetes, Terraform, AWS, CI/CD pipelines, monitoring.\n" * 5),
    Case("react-native", "unsuitable", "React Native Developer", "React Native, iOS and Android apps, mobile releases.\n" * 5),
    Case("android", "unsuitable", "Android Developer", "Kotlin, Android SDK, Jetpack Compose.\n" * 5),
    Case("dotnet-generic", "unsuitable", "Software Engineer",
         ("Requirements: C#, .NET Core, ASP.NET Web API, SQL Server, Entity Framework, Azure. .NET microservices.\n") * 4),
    Case("ml-engineer", "unsuitable", "Machine Learning Engineer", "PyTorch, model training, feature stores, MLOps.\n" * 5),
    Case("ai-engineer-ml", "unsuitable", "AI Engineer",
         ("Train and fine-tune models in PyTorch and TensorFlow, build data pipelines in Python, deploy with Kubernetes on "
          "AWS, machine learning evaluation and MLOps.\n") * 4, why="read, then judged on its (non-frontend) description"),
    Case("backend-node", "unsuitable", "Backend Engineer (Node.js)",
         "Node.js, Express, PostgreSQL, Kafka, Redis, microservices, REST APIs.\n" * 5),
    Case("wordpress", "unsuitable", "WordPress Developer",
         ("Build and maintain WordPress themes and plugins with PHP, Elementor and WooCommerce; HTML, CSS and JavaScript "
          "for page templates; WordPress hosting and SEO.\n") * 3),
    Case("freelance-designer", "unsuitable", "Freelance Web Designer & Developer",
         ("Employment Type: Contract / Freelance. Design and launch our website on Wix or Squarespace; responsive layout, "
          "accessibility, branding and content.\n") * 3, location="Remote"),
    Case("servicenow", "unsuitable", "ServiceNow Developer",
         ("Develop ServiceNow Service Portal widgets with JavaScript, HTML, CSS and AngularJS; client scripts, business rules "
          "and ServiceNow integrations; ServiceNow ITSM modules.\n") * 3,
         why="customises a vendor platform: never auto-applied (its JavaScript is the platform's)"),
    Case("engineer-in-test", "unsuitable", "Software Development Engineer in Test",
         ("Build automated test suites with Playwright and Selenium for our web apps; JavaScript, TypeScript, CI/CD, test "
          "plans and regression testing.\n") * 3, why="a QA role whatever the wording ('Engineer in Test')"),
    Case("oic-walk-in", "unsuitable", "OIC Developer - Face to Face Walk-In Drive",
         ("Design system integrations, reports and custom user interfaces in the Oracle Cloud ecosystem using Oracle "
          "Integration Cloud (OIC); JavaScript, HTML, CSS, REST APIs, SQL. Walk-in drive on Saturday.\n") * 3),
    Case("blockchain", "unsuitable", "Blockchain Developer",
         ("Build smart contracts in Solidity and Web3 dApps; blockchain integrations, Git, CI/CD, web performance.\n") * 3),
    Case("react-contract", "unsuitable", "Senior React Developer", "Job Type: Contract\n" + _REACT_JD * 2,
         why="relevant work, but contract terms are yours to decide - never auto-applied"),

    # ------------------------------------------------------------------ ambiguous
    Case("thin-generic", "ambiguous", "Software Engineer", "Join our engineering team. Competitive pay."),
    Case("thin-consultant", "ambiguous", "Technology Consultant",
         "We help clients with digital transformation. Great culture and benefits.\n" * 3),
]
