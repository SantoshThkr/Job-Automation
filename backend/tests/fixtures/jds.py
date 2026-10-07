"""Realistic job descriptions (modelled on typical Indian job-portal postings) with expected decisions.

Expected decisions assume the default example profile: frontend engineer, 7 years, React/TypeScript,
Bengaluru or remote, not willing to relocate.
"""

JDS = [
    {
        "id": "senior_react_bangalore",
        "expected": "APPLY",
        "title": "Senior Frontend Engineer - React",
        "company": "Razorpay",
        "location": "Bengaluru",
        "experience_text": "5-9 Yrs",
        "skills": ["React.js", "TypeScript", "Redux", "JavaScript", "HTML", "CSS"],
        "description": """About the role
We are looking for a Senior Frontend Engineer to build delightful, high-performance payment experiences used by millions of merchants.

What you will do:
- Own the architecture of large React + TypeScript applications
- Build reusable components for our design system and component library
- Improve web performance (Core Web Vitals, bundle size, page load)
- Collaborate with designers on pixel-perfect, accessible (WCAG) user interfaces
- Mentor engineers and review code

Requirements:
- 5+ years of experience building web applications with JavaScript and TypeScript
- Strong expertise in React, Redux / React Query and modern state management
- Deep understanding of HTML5, CSS3, SCSS and responsive design
- Experience with Webpack or Vite, Jest and React Testing Library
- Familiarity with REST APIs and GraphQL

Nice to have:
- Next.js and server-side rendering
- Experience with micro-frontends
""",
    },
    {
        "id": "react_dev_remote",
        "expected": "APPLY",
        "title": "React JS Developer",
        "company": "Toptal Partner",
        "location": "Remote",
        "experience_text": "4-8 Yrs",
        "skills": ["ReactJS", "JavaScript", "Redux"],
        "description": """Job Description
We need an experienced React.js developer to join a fully remote product team.

Responsibilities
- Develop new user-facing features using React.js and Redux toolkit
- Translate Figma designs into high quality code
- Optimise components for maximum performance across devices and browsers

Skills Required
- 4+ years of professional experience with React.js and JavaScript (ES6+)
- TypeScript, HTML, CSS, Tailwind CSS
- Unit testing with Jest
- Git, CI/CD, Agile
""",
    },
    {
        "id": "java_backend",
        "expected": "SKIP",
        "title": "Senior Java Backend Developer",
        "company": "Infosys",
        "location": "Bengaluru",
        "experience_text": "6-10 Yrs",
        "skills": ["Java", "Spring Boot", "Microservices", "Kafka", "SQL"],
        "description": """Role: Senior Java Developer
Must have:
- 6+ years of experience in Java 11+, Spring Boot, Spring MVC and Hibernate/JPA
- Designing and building RESTful microservices
- Kafka / RabbitMQ messaging, Redis caching
- Strong SQL (PostgreSQL, Oracle) and NoSQL (MongoDB)
- Docker, Kubernetes, AWS, Jenkins CI/CD
Good to have: exposure to Angular for minor UI changes.
""",
    },
    {
        "id": "fullstack_mern",
        "expected": ["APPLY", "MANUAL_REVIEW"],
        "title": "Full Stack Developer (React / Node.js)",
        "company": "Freshworks",
        "location": "Bengaluru, Chennai",
        "experience_text": "5-8 Yrs",
        "skills": ["React", "Node.js", "TypeScript", "MongoDB"],
        "description": """We're hiring a Full Stack Developer comfortable across the stack, with a frontend focus.

Responsibilities:
- Build responsive React + TypeScript interfaces and reusable UI components
- Develop Node.js / Express APIs that power the frontend
- Write unit tests with Jest and React Testing Library

Requirements:
- 5+ years experience with JavaScript/TypeScript
- Strong React, Redux, HTML, CSS skills
- Working knowledge of Node.js, Express and MongoDB
- REST APIs, Git
""",
    },
    {
        "id": "java_fullstack_pune",
        "expected": "SKIP",
        "title": "Java Full Stack Developer",
        "company": "Capgemini",
        "location": "Pune",
        "experience_text": "6-9 Yrs",
        "skills": ["Java", "Spring Boot", "Angular", "Microservices"],
        "description": """Key skills: Java 8, Spring Boot, Microservices, Hibernate, Angular 12+, SQL, Kafka, Docker, Jenkins.
Requirements:
- 6+ years of experience in Java backend development with Spring Boot and microservices
- Experience building REST APIs, Hibernate, Oracle/MySQL
- Angular for UI development
Work from office: Pune (5 days).
""",
    },
    {
        "id": "frontend_intern",
        "expected": "SKIP",
        "title": "Frontend Developer Intern",
        "company": "StartupX",
        "location": "Bengaluru",
        "experience_text": "0-1 Yrs",
        "skills": ["React", "HTML", "CSS"],
        "description": "Internship for students. Learn React, HTML, CSS and JavaScript while building internal tools. Stipend provided.",
    },
    {
        "id": "angular_lead",
        "expected": "MANUAL_REVIEW",
        "title": "Lead Angular Developer",
        "company": "Wipro",
        "location": "Bengaluru",
        "experience_text": "7-10 Yrs",
        "skills": ["Angular", "TypeScript", "RxJS", "NgRx"],
        "description": """Job Summary
Lead a team of frontend engineers building enterprise dashboards.

Mandatory skills:
- 7+ years of frontend development, 4+ years in Angular (v12+)
- TypeScript, RxJS, NgRx state management
- HTML5, CSS3, SCSS, responsive design
- Unit testing with Jasmine / Karma
- Leading code reviews and mentoring
""",
    },
    {
        "id": "react_native",
        "expected": "SKIP",
        "title": "React Native Developer",
        "company": "Swiggy",
        "location": "Bengaluru",
        "experience_text": "4-7 Yrs",
        "skills": ["React Native", "JavaScript", "iOS", "Android"],
        "description": """Build our consumer mobile apps for iOS and Android using React Native.
Requirements:
- 4+ years in mobile development with React Native
- Native modules in Swift / Kotlin
- App Store and Play Store releases, mobile performance profiling
""",
    },
    {
        "id": "devops",
        "expected": "SKIP",
        "title": "DevOps Engineer",
        "company": "Flipkart",
        "location": "Bengaluru",
        "experience_text": "5-8 Yrs",
        "skills": ["AWS", "Kubernetes", "Terraform", "Jenkins"],
        "description": "Own CI/CD pipelines, Kubernetes clusters on AWS, Terraform infrastructure as code, Linux, monitoring with Prometheus/Grafana. 5+ years of experience required.",
    },
    {
        "id": "junior_react",
        "expected": "SKIP",
        "title": "Junior React Developer",
        "company": "WebAgency",
        "location": "Bengaluru",
        "experience_text": "0-2 Yrs",
        "skills": ["React", "JavaScript"],
        "description": "Looking for a junior React developer with 0-2 years of experience. Freshers with strong JavaScript fundamentals are welcome. You will build landing pages using React, HTML and CSS.",
    },
    {
        "id": "staff_frontend_12y",
        "expected": "SKIP",
        "title": "Principal Frontend Architect",
        "company": "Atlassian",
        "location": "Bengaluru",
        "experience_text": "12-18 Yrs",
        "skills": ["React", "TypeScript", "Architecture"],
        "description": "We need a principal frontend architect with 12+ years of experience to define frontend architecture across products. React, TypeScript, micro-frontends, design systems.",
    },
    {
        "id": "generic_swe_frontend_heavy",
        "expected": ["APPLY", "MANUAL_REVIEW"],
        "title": "Software Engineer II",
        "company": "Microsoft",
        "location": "Bengaluru / Hybrid",
        "experience_text": "4-8 Yrs",
        "skills": ["React", "TypeScript", "JavaScript"],
        "description": """You will build the web experience for Microsoft Teams.

Qualifications:
- 4+ years of professional software development experience
- Expertise in TypeScript, JavaScript, React and modern frontend tooling
- Strong knowledge of HTML, CSS, browser APIs, web performance and accessibility
- Experience with unit testing (Jest) and Git
""",
    },
    {
        "id": "ui_dev_hyderabad",
        "expected": "SKIP",
        "title": "UI Developer",
        "company": "Deloitte",
        "location": "Hyderabad",
        "experience_text": "5-8 Yrs",
        "skills": ["HTML", "CSS", "JavaScript", "React"],
        "description": "UI Developer for client projects in Hyderabad (work from office). HTML5, CSS3, JavaScript, React, Bootstrap. 5+ years of experience.",
    },
    {
        "id": "em_frontend",
        "expected": "MANUAL_REVIEW",
        "title": "Engineering Manager - Frontend",
        "company": "PhonePe",
        "location": "Bengaluru",
        "experience_text": "8-12 Yrs",
        "skills": ["React", "People Management", "JavaScript"],
        "description": """Lead and grow a team of 8 frontend engineers working on React and TypeScript.
Requirements:
- 8+ years of experience, including 2+ years managing engineers
- Strong background in React, JavaScript, TypeScript and web performance
- Hiring, performance reviews, roadmap planning
""",
    },
    {
        "id": "data_scientist",
        "expected": "SKIP",
        "title": "Data Scientist",
        "company": "Fractal",
        "location": "Bengaluru",
        "experience_text": "4-8 Yrs",
        "skills": ["Python", "Machine Learning", "SQL"],
        "description": "Build machine learning models with Python, pandas, scikit-learn, TensorFlow. NLP and LLM experience preferred. 4+ years of experience.",
    },
    {
        "id": "vue_frontend",
        "expected": "MANUAL_REVIEW",
        "title": "Frontend Engineer (Vue.js)",
        "company": "Zoho",
        "location": "Bengaluru",
        "experience_text": "4-7 Yrs",
        "skills": ["Vue.js", "JavaScript", "Vuex"],
        "description": """Requirements:
- 4+ years building SPAs with Vue.js and Vuex
- JavaScript, TypeScript, HTML, CSS, SCSS
- Jest, Cypress
- REST APIs
""",
    },
    {
        "id": "nextjs_hybrid",
        "expected": "APPLY",
        "title": "Next.js Developer",
        "company": "Groww",
        "location": "Hybrid - Bengaluru",
        "experience_text": "5-10 Yrs",
        "skills": ["Next.js", "React", "TypeScript", "Tailwind CSS"],
        "description": """What you'll do:
- Build SEO-friendly, server-side rendered pages with Next.js and React
- Improve Core Web Vitals and accessibility
Must have:
- 5+ years of frontend experience with React, Next.js and TypeScript
- Tailwind CSS, HTML, CSS, responsive design
- Jest, React Testing Library, Git
""",
    },
]
