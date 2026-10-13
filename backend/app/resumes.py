"""Resume management: listing, per-job selection and uploads."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.config import PROJECT_ROOT, get_settings
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


def select_resume(profile: Profile, category: str = "", skills: list[str] | None = None) -> ResumeEntry | None:
    """Pick the resume whose tags best fit the job category/skills. Only existing files qualify."""
    candidates = [r for r in profile.resumes if r.absolute_path.is_file()]
    if not candidates:
        return None
    wanted = {category.lower()} if category else set()
    wanted |= {s.lower() for s in (skills or [])}

    def score(r: ResumeEntry) -> tuple[int, int]:
        tags = {t.lower() for t in r.tags}
        overlap = len(tags & wanted)
        # category match counts double
        if category and category.lower() in tags:
            overlap += 2
        return overlap, 1 if r.default else 0

    best = max(candidates, key=score)
    if score(best)[0] == 0:
        default = next((r for r in candidates if r.default), None)
        return default or best
    return best


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
