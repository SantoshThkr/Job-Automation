"""Skill taxonomy: canonical skill names, categories and alias patterns used to read job descriptions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class SkillDef:
    name: str
    category: str  # frontend|backend|fullstack_js|mobile|devops|data|testing|general|design
    pattern: str
    weight: float = 1.0  # strength of the signal for the frontend/backend ratio
    framework: bool = False  # a primary UI framework (React/Angular/Vue/Svelte)
    signal_only: bool = False  # used for the frontend/backend ratio, not as a listed skill


_SKILLS: list[SkillDef] = [
    # --- Frontend -------------------------------------------------------
    SkillDef("React", "frontend", r"\breact(?:\.?js)?\b(?![\s-]*native)", 1.5, framework=True),
    SkillDef("Angular", "frontend", r"\bangular(?:\s?js|\.js|\s?\d+)?\b", 1.5, framework=True),
    SkillDef("Vue", "frontend", r"\bvue(?:\.?js)?\b|\bvuex\b", 1.5, framework=True),
    SkillDef("Svelte", "frontend", r"\bsvelte(?:kit)?\b", 1.5, framework=True),
    SkillDef("Next.js", "frontend", r"\bnext\.?js\b", 1.3),
    SkillDef("Nuxt", "frontend", r"\bnuxt(?:\.?js)?\b", 1.3),
    SkillDef("Remix", "frontend", r"\bremix\.?run\b|\bremix framework\b", 1.0),
    SkillDef("Gatsby", "frontend", r"\bgatsby(?:\.?js)?\b", 1.0),
    SkillDef("Redux", "frontend", r"\bredux(?:[\s-]?toolkit|[\s-]?saga|[\s-]?thunk)?\b", 1.2),
    SkillDef("MobX", "frontend", r"\bmobx\b", 1.0),
    SkillDef("Zustand", "frontend", r"\bzustand\b", 1.0),
    SkillDef("React Query", "frontend", r"\breact[\s-]?query\b|\btanstack\b", 1.0),
    SkillDef("RxJS", "frontend", r"\brxjs\b", 1.0),
    SkillDef("JavaScript", "frontend", r"\bjavascript\b|\bjava\s+script\b|\bes6\+?\b|\becmascript\b|\bvanilla\s+js\b|(?<![.\w])js\b", 1.0),
    SkillDef("TypeScript", "frontend", r"\btypescript\b", 1.0),
    SkillDef("HTML", "frontend", r"\bhtml\s?5?\b", 1.2),
    SkillDef("CSS", "frontend", r"\bcss\s?3?\b(?![\s-]*in[\s-]*js)|\bcss[\s-]in[\s-]js\b", 1.2),
    SkillDef("SCSS", "frontend", r"\bs[ac]ss\b|\bless\s?css\b", 1.0),
    SkillDef("Tailwind CSS", "frontend", r"\btailwind(?:\s?css)?\b", 1.0),
    SkillDef("Bootstrap", "frontend", r"\bbootstrap\b", 0.8),
    SkillDef("Material UI", "frontend", r"\bmaterial[\s-]?ui\b|\bmui\b|\bchakra(?:\s?ui)?\b|\bant\s?design\b", 0.8),
    SkillDef("Styled Components", "frontend", r"\bstyled[\s-]components\b|\bemotion\b", 0.8),
    SkillDef("Webpack", "frontend", r"\bwebpack\b", 1.0),
    SkillDef("Vite", "frontend", r"\bvite\b", 1.0),
    SkillDef("Babel", "frontend", r"\bbabel\b", 0.8),
    SkillDef("jQuery", "frontend", r"\bjquery\b", 0.8),
    SkillDef("Responsive Design", "frontend", r"\bresponsive(?:\s+(?:design|web|ui|layouts?))?\b|\bmobile[\s-]first\b", 1.0),
    SkillDef("Web Performance", "frontend", r"\bweb\s+performance\b|\bcore\s+web\s+vitals\b|\blighthouse\b|\bperformance\s+optimi[sz]ation\b|\bpage\s+load\b", 1.0),
    SkillDef("Accessibility", "frontend", r"\baccessibility\b|\ba11y\b|\bwcag\b|\baria\b", 1.0),
    SkillDef("Micro Frontends", "frontend", r"\bmicro[\s-]?front[\s-]?ends?\b|\bmodule\s+federation\b", 1.2),
    SkillDef("Storybook", "frontend", r"\bstorybook\b", 1.0),
    SkillDef("D3.js", "frontend", r"\bd3(?:\.js)?\b", 0.8),
    SkillDef("Three.js", "frontend", r"\bthree\.?js\b|\bwebgl\b", 0.8),
    SkillDef("Web Components", "frontend", r"\bweb\s+components\b|\blit(?:element)?\b(?=.*component)", 0.8),
    SkillDef("PWA", "frontend", r"\bpwa\b|\bprogressive\s+web\s+apps?\b|\bservice\s+workers?\b", 0.8),
    SkillDef("SSR", "frontend", r"\bssr\b|\bserver[\s-]side\s+rendering\b", 0.8),
    SkillDef("Browser APIs", "frontend", r"\bdom\b|\bbrowser\s+apis?\b|\bcross[\s-]browser\b", 1.0, signal_only=True),
    SkillDef("UI Development", "frontend", r"\bui\s+(?:development|engineering|components?)\b|\bfront[\s-]?end\b|\bfrontend\b|\buser\s+interfaces?\b", 1.0, signal_only=True),
    SkillDef("Design Systems", "frontend", r"\bdesign\s+systems?\b|\bcomponent\s+librar(?:y|ies)\b", 1.0),
    # --- Design ---------------------------------------------------------
    SkillDef("Figma", "design", r"\bfigma\b|\bsketch\b|\badobe\s+xd\b", 0.5),
    SkillDef("UI/UX", "design", r"\bui\s*/\s*ux\b|\bux\b", 0.5),
    # --- Frontend testing ----------------------------------------------
    SkillDef("Jest", "testing", r"\bjest\b", 0.8),
    SkillDef("React Testing Library", "testing", r"\b(?:react\s+)?testing[\s-]library\b|\brtl\b", 0.8),
    SkillDef("Cypress", "testing", r"\bcypress\b", 0.8),
    SkillDef("Playwright", "testing", r"\bplaywright\b", 0.6),
    SkillDef("Vitest", "testing", r"\bvitest\b", 0.8),
    SkillDef("Enzyme", "testing", r"\benzyme\b", 0.8),
    SkillDef("Selenium", "testing", r"\bselenium\b", 0.5),
    # --- JS backend (counts as fullstack signal) -------------------------
    SkillDef("Node.js", "fullstack_js", r"\bnode(?:\.?js)?\b", 1.0),
    SkillDef("Express", "fullstack_js", r"\bexpress\.?js\b|\bnode(?:\.?js)?\s*(?:/|,|and|&)\s*express\b", 1.0),
    SkillDef("NestJS", "fullstack_js", r"\bnest\.?js\b", 1.0),
    # --- Backend --------------------------------------------------------
    SkillDef("Java", "backend", r"\bjava\b(?!\s*script)|\bj2ee\b|\bjee\b", 1.5),
    SkillDef("Spring Boot", "backend", r"\bspring(?:\s?boot|\s+framework|\s+mvc)?\b", 1.5),
    SkillDef("Hibernate", "backend", r"\bhibernate\b|\bjpa\b", 1.2),
    SkillDef("Python", "backend", r"\bpython\b", 1.2),
    SkillDef("Django", "backend", r"\bdjango\b", 1.3),
    SkillDef("Flask", "backend", r"\bflask\b", 1.3),
    SkillDef("FastAPI", "backend", r"\bfastapi\b", 1.3),
    SkillDef("Go", "backend", r"\bgolang\b|\bgo\s+(?:language|lang|programming)\b|\(go\)", 1.5),
    SkillDef("Ruby on Rails", "backend", r"\bruby(?:\s+on\s+rails)?\b|\brails\b", 1.3),
    SkillDef("PHP", "backend", r"\bphp\b|\blaravel\b|\bsymfony\b|\bcodeigniter\b", 1.3),
    SkillDef(".NET", "backend", r"\.net\b|\bdotnet\b|\bc#|\basp\.net\b", 1.5),
    SkillDef("Scala", "backend", r"\bscala\b", 1.3),
    SkillDef("Rust", "backend", r"\brust\b", 1.0),
    SkillDef("C++", "backend", r"\bc\+\+", 1.0),
    SkillDef("Microservices", "backend", r"\bmicro[\s-]?services?\b", 1.0),
    SkillDef("Kafka", "backend", r"\bkafka\b", 1.0),
    SkillDef("RabbitMQ", "backend", r"\brabbitmq\b|\bmessage\s+queues?\b", 1.0),
    SkillDef("Redis", "backend", r"\bredis\b", 0.8),
    SkillDef("SQL", "backend", r"\bsql\b|\bmysql\b|\bpostgres(?:ql)?\b|\boracle\s+db\b|\bplsql\b|\bpl/sql\b", 0.8),
    SkillDef("MongoDB", "backend", r"\bmongo(?:db)?\b", 0.7),
    SkillDef("DynamoDB", "backend", r"\bdynamo\s?db\b|\bcassandra\b", 0.8),
    SkillDef("Elasticsearch", "backend", r"\belastic\s?search\b", 0.8),
    SkillDef("gRPC", "backend", r"\bgrpc\b", 1.0),
    SkillDef("Backend Development", "backend", r"\bdistributed\s+systems?\b|\bhigh[\s-]throughput\b|\bscalable\s+backend\b|\bback[\s-]?end\b", 1.0, signal_only=True),
    # --- Shared ---------------------------------------------------------
    SkillDef("REST APIs", "general", r"\brestful\b|\brest\s*(?:apis?|services|web\s+services|endpoints)\b|\bapi\s+integration\b", 0.3),
    SkillDef("GraphQL", "general", r"\bgraphql\b|\bapollo\b", 0.3),
    SkillDef("Git", "general", r"\bgit\b|\bgithub\b|\bgitlab\b|\bbitbucket\b", 0.2),
    SkillDef("CI/CD", "general", r"\bci\s*/\s*cd\b|\bcontinuous\s+integration\b", 0.3),
    SkillDef("Agile", "general", r"\bagile\b|\bscrum\b|\bjira\b", 0.1),
    SkillDef("WebSockets", "general", r"\bweb\s?sockets?\b|\bsocket\.io\b", 0.3),
    # --- DevOps / cloud --------------------------------------------------
    SkillDef("AWS", "devops", r"\baws\b|\bamazon\s+web\s+services\b|\blambda\b|\bec2\b|\bs3\b", 0.6),
    SkillDef("Azure", "devops", r"\bazure\b", 0.6),
    SkillDef("GCP", "devops", r"\bgcp\b|\bgoogle\s+cloud\b", 0.6),
    SkillDef("Docker", "devops", r"\bdocker\b", 0.6),
    SkillDef("Kubernetes", "devops", r"\bkubernetes\b|\bk8s\b|\bhelm\b", 1.0),
    SkillDef("Terraform", "devops", r"\bterraform\b|\bansible\b|\bcloudformation\b", 1.2),
    SkillDef("Jenkins", "devops", r"\bjenkins\b", 0.6),
    SkillDef("Linux", "devops", r"\blinux\b|\bshell\s+scripting\b|\bbash\b", 0.6),
    # --- Mobile ---------------------------------------------------------
    SkillDef("React Native", "mobile", r"\breact[\s-]*native\b", 1.2),
    SkillDef("Flutter", "mobile", r"\bflutter\b|\bdart\b", 1.5),
    SkillDef("Swift", "mobile", r"\bswift(?:ui)?\b|\bobjective[\s-]c\b", 1.5),
    SkillDef("Android", "mobile", r"\bandroid\b|\bkotlin\b", 1.5),
    SkillDef("iOS", "mobile", r"\bios\b|\bxcode\b", 1.5),
    # --- Data / ML ------------------------------------------------------
    SkillDef("Machine Learning", "data", r"\bmachine\s+learning\b|\bdeep\s+learning\b|\bml\b|\bnlp\b|\bmodel\s+training\b", 1.5),
    SkillDef("Data Engineering", "data", r"\bspark\b|\bhadoop\b|\bairflow\b|\betl\b|\bdata\s+pipelines?\b|\bsnowflake\b|\bdatabricks\b", 1.5),
    SkillDef("Data Analysis", "data", r"\bpandas\b|\bnumpy\b|\btableau\b|\bpower\s?bi\b|\bdata\s+analysis\b", 1.2),
    SkillDef("TensorFlow", "data", r"\btensorflow\b|\bpytorch\b|\bkeras\b|\bscikit[\s-]learn\b", 1.5),

    # --- AI-powered applications (LLM APIs, RAG, agents). Neutral for the frontend/backend ratio: these
    # skills appear in frontend + AI roles as much as in backend ones.
    SkillDef("Generative AI", "ai", r"\bgen(?:erative)?[\s-]?ai\b|\bllms?\b|\blarge\s+language\s+models?\b|\bchat\s?gpt\b|"
                                   r"\bgpt[\s-]?\d(?:\.\d)?o?\b|\bopen\s?ai\b|\bazure\s+open\s?ai\b|\bclaude\b|\bgemini\b|"
                                   r"\bprompt\s+engineering\b|\bcopilot\b", 1.0),
    SkillDef("RAG", "ai", r"\brag\b|\bretrieval[\s-]augmented\b|\bvector\s+(?:db|database|search|stores?)s?\b|"
                         r"\bembeddings?\b|\bpinecone\b|\bchromadb\b|\bweaviate\b|\bfaiss\b|\bpgvector\b", 1.0),
    SkillDef("AI Agents", "ai", r"\bai\s+agents?\b|\bagentic\b|\blangchain\b|\blanggraph\b|\bllama\s?index\b|"
                               r"\bautogen\b|\bcrew\s?ai\b|\bvercel\s+ai\s+sdk\b|\bai\s+sdk\b", 1.0),
]

FRONTEND_CATEGORIES = {"frontend", "testing", "design"}
BACKEND_CATEGORIES = {"backend", "devops", "data", "mobile"}


@lru_cache
def _compiled() -> list[tuple[SkillDef, re.Pattern[str]]]:
    return [(s, re.compile(s.pattern, re.IGNORECASE)) for s in _SKILLS]


def all_skills() -> list[SkillDef]:
    return list(_SKILLS)


def skill_def(name: str) -> SkillDef | None:
    key = name.strip().lower()
    for s in _SKILLS:
        if s.name.lower() == key:
            return s
    return None


def extract_skills(text: str) -> dict[str, int]:
    """Return canonical skill -> number of mentions found in ``text``."""
    if not text:
        return {}
    found: dict[str, int] = {}
    for s, rx in _compiled():
        n = len(rx.findall(text))
        if n:
            found[s.name] = n
    return found


def canonicalize(skill: str) -> str:
    """Map a free-form skill name (e.g. from the profile or a portal tag) to its canonical name."""
    raw = skill.strip()
    if not raw:
        return raw
    direct = skill_def(raw)
    if direct:
        return direct.name
    for s, rx in _compiled():
        m = rx.search(raw)
        if m and (m.end() - m.start()) >= max(2, len(raw) * 0.5):
            return s.name
    return raw


def category_of(skill: str) -> str:
    d = skill_def(skill)
    return d.category if d else "general"
