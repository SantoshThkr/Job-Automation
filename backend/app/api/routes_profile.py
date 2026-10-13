"""Profile, settings and resume endpoints."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import PROJECT_ROOT, get_settings
from app.profile import get_profile_store, validate_profile
from app.resumes import list_resumes, save_uploaded_resume

router = APIRouter(prefix="/api", tags=["profile"])

EDITABLE_SECTIONS = {"search", "matching", "job_preferences", "work_preferences", "custom_answers"}


def display_path(path: str | Path) -> str:
    """A path as shown in the dashboard: relative to the project (or to your home folder) - the API does not
    hand out absolute file-system paths."""
    p = Path(path)
    for base, prefix in ((PROJECT_ROOT, ""), (Path.home(), "~/")):
        try:
            return prefix + str(p.resolve().relative_to(base.resolve()))
        except (ValueError, OSError):
            continue
    return p.name


@router.get("/profile")
def get_profile() -> dict[str, Any]:
    store = get_profile_store()
    profile = store.load()
    issues = validate_profile(profile)
    return {
        "path": display_path(store.path),
        "profile": profile.model_dump(),
        "issues": [i.model_dump() for i in issues],
        "ready_for_submission": not any(i.severity == "error" for i in issues),
        "load_error": store.last_error,
    }


class SettingsUpdate(BaseModel):
    """Only the preference sections are editable here; personal details, work history and resumes are not."""
    model_config = ConfigDict(extra="forbid")

    search: dict[str, Any] | None = None
    matching: dict[str, Any] | None = None
    job_preferences: dict[str, Any] | None = None
    work_preferences: dict[str, Any] | None = None
    custom_answers: Annotated[list[dict[str, Any]], Field(max_length=500)] | None = None


@router.put("/profile/settings")
def update_settings(body: SettingsUpdate) -> dict[str, Any]:
    updates = {k: v for k, v in body.model_dump().items() if v is not None and k in EDITABLE_SECTIONS}
    if not updates:
        raise HTTPException(400, "Nothing to update")
    try:
        profile = get_profile_store().update_sections(updates)
    except ValidationError as exc:  # where and what - not the values that were sent
        problems = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', 'invalid')}"
                             for e in exc.errors()[:5])
        raise HTTPException(422, f"Invalid settings: {problems}") from exc
    return {"ok": True, "profile": profile.model_dump()}


@router.get("/resumes")
def resumes() -> list[dict[str, Any]]:
    return [asdict(r) | {"absolute_path": display_path(r.absolute_path)}
            for r in list_resumes(get_profile_store().load())]


@router.post("/resumes")
async def upload_resume(file: UploadFile = File(...), resume_id: str = Form(..., max_length=64),
                        label: str = Form("", max_length=200), tags: str = Form("", max_length=500),
                        make_default: bool = Form(False)) -> dict[str, Any]:
    limit = get_settings().max_upload_bytes
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(1024 * 1024):  # never hold more than the limit in memory
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"Resume larger than {limit // (1024 * 1024)} MB")
        chunks.append(chunk)
    content = b"".join(chunks)
    try:
        entry = save_uploaded_resume(get_profile_store(), file.filename or "resume.pdf", content, resume_id,
                                     label=label, tags=[t.strip() for t in tags.split(",") if t.strip()],
                                     make_default=make_default)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "resume": entry.model_dump()}
