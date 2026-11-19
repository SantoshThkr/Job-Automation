"""Resume management: listing, per-job selection and uploads."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pypdf import PdfReader

from app.config import PROJECT_ROOT, get_settings
from app.matching.skills import canonicalize, extract_skills
from app.profile import Profile, ProfileStore, ResumeEntry

ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx"}
# how each allowed type starts: a renamed executable or HTML page is not a resume
_SIGNATURES = {".pdf": (b"%PDF-",), ".docx": (b"PK\x03\x04",), ".doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",)}


@dataclass
class ResumeInfo:
    id: str
    label: str
    path: str
    absolute_path: str
    exists: bool
    size_bytes: int
    tags: list[str]
    default: bool


def list_resumes(profile: Profile) -> list[ResumeInfo]:
    out = []
    for r in profile.resumes:
        p = r.absolute_path
        out.append(ResumeInfo(
            id=r.id, label=r.label or r.id, path=r.path, absolute_path=str(p),
            exists=p.is_file(), size_bytes=p.stat().st_size if p.is_file() else 0,
            tags=r.tags, default=r.default,
        ))
    return out


@lru_cache(maxsize=32)
def _document_skills(path: Path, modified_ns: int, size: int) -> frozenset[str] | None:
    if path.suffix.lower() != ".pdf":
        return None
    try:
        reader = PdfReader(path)
        listed = set()
        for page in reader.pages:
            for line in (page.extract_text() or "").splitlines():
                heading, colon, items = line.strip().partition(":")
                if colon and re.fullmatch(r"front[ -]?end|back[ -]?end(?:/integration)?|generative ai|databases|"
                                          r"cloud\s*&\s*devops|testing\s*&\s*(?:quality|practices)|"
                                          r"stack|technologies\s+used|(?:technical\s+)?skills", heading, re.I):
                    listed.update(extract_skills(re.split(r"\(integrat(?:ed|ion) with", items, flags=re.I)[0]))
        return frozenset(listed)
    except Exception:
        # Uploaded documents can be encrypted, damaged or image-only. Their configured tags still apply.
        return None


def resume_skills(resume: ResumeEntry) -> frozenset[str]:
    path = resume.absolute_path
    if not path.is_file():
        return frozenset()
    stat = path.stat()
    return _document_skills(path, stat.st_mtime_ns, stat.st_size) or frozenset()


def rank_resumes(profile: Profile, category: str, skills: list[str], required: list[str]) -> list[dict]:
    wanted = {canonicalize(s) for s in skills}
    mandatory = {canonicalize(s) for s in required}
    ranked = []
    for resume in profile.resumes:
        path = resume.absolute_path
        if not path.is_file():
            continue
        document = resume_skills(resume)
        evidence = {canonicalize(s) for s in resume.tags} | set(document or ())
        matched = sorted(evidence & wanted)
        matched_required = sorted(evidence & mandatory)
        family = re.sub(r"[ -]", "", category.lower())
        labels = [re.sub(r"full[ -]stack", "fullstack", t.lower()) for t in
                  (resume.id, resume.label, *resume.tags)]
        family_match = bool(family and any(re.search(r"\b" + re.escape(family) + r"\b", t) for t in labels))
        score = len(matched) + 2 * len(matched_required) + 4 * family_match
        ranked.append({"id": resume.id, "score": score, "matched": matched, "matched_required": matched_required,
                       "source": "PDF and tags" if document else "configured tags", "default": resume.default,
                       "category_match": family_match, "designated": resume.id.casefold() == family})
    return sorted(ranked, key=lambda r: (r["score"], r["designated"], r["default"]), reverse=True)


def select_resume(profile: Profile, category: str = "", skills: list[str] | None = None) -> ResumeEntry | None:
    ranked = rank_resumes(profile, category, skills or [], [])
    return get_resume(profile, ranked[0]["id"]) if ranked else None


def get_resume(profile: Profile, resume_id: str | None) -> ResumeEntry | None:
    if not resume_id:
        return None
    return next((r for r in profile.resumes if r.id == resume_id), None)


def _safe_filename(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).stem).strip("-") or "resume"
    return f"{stem}{Path(name).suffix.lower()}"


def save_uploaded_resume(store: ProfileStore, filename: str, content: bytes, resume_id: str,
                         label: str = "", tags: list[str] | None = None, make_default: bool = False) -> ResumeEntry:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported resume type {ext}; use PDF/DOC/DOCX")
    if not content:
        raise ValueError("Empty file")
    if not content.lstrip(b"\xef\xbb\xbf\r\n\t ")[:8].startswith(_SIGNATURES[ext]):
        raise ValueError(f"The file does not look like a {ext[1:].upper()} document")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", resume_id):
        raise ValueError("Resume id must be 1-64 letters, digits, '-' or '_'")
    resumes_dir = get_settings().resumes_path
    resumes_dir.mkdir(parents=True, exist_ok=True)
    target = resumes_dir / _safe_filename(filename)
    if target.resolve().parent != resumes_dir.resolve():
        raise ValueError("Invalid file name")
    target.write_bytes(content)
    target.chmod(0o600)
    try:
        rel = str(target.relative_to(PROJECT_ROOT))
    except ValueError:
        rel = str(target)

    raw = store.raw()
    resumes = [r for r in raw.get("resumes", []) if r.get("id") != resume_id]
    if make_default:
        for r in resumes:
            r["default"] = False
    entry = {"id": resume_id, "path": rel, "label": label or resume_id, "tags": tags or [],
             "default": make_default or not resumes}
    resumes.append(entry)
    store.update_sections({"resumes": resumes})
    return ResumeEntry.model_validate(entry)
