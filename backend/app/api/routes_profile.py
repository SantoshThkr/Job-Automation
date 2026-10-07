"""Profile, settings and resume endpoints."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ValidationError

from app.profile import get_profile_store, validate_profile
from app.resumes import list_resumes, save_uploaded_resume

router = APIRouter(prefix="/api", tags=["profile"])

EDITABLE_SECTIONS = {"search", "matching", "job_preferences", "work_preferences", "custom_answers"}


@router.get("/profile")
def get_profile() -> dict[str, Any]:
    store = get_profile_store()
    profile = store.load()
    issues = validate_profile(profile)
    return {
        "path": str(store.path),
        "profile": profile.model_dump(),
        "issues": [i.model_dump() for i in issues],
        "ready_for_submission": not any(i.severity == "error" for i in issues),
        "load_error": store.last_error,
    }


class SettingsUpdate(BaseModel):
    search: dict[str, Any] | None = None
    matching: dict[str, Any] | None = None
    job_preferences: dict[str, Any] | None = None
    work_preferences: dict[str, Any] | None = None
    custom_answers: list[dict[str, Any]] | None = None


@router.put("/profile/settings")
def update_settings(body: SettingsUpdate) -> dict[str, Any]:
    updates = {k: v for k, v in body.model_dump().items() if v is not None and k in EDITABLE_SECTIONS}
    if not updates:
        raise HTTPException(400, "Nothing to update")
    try:
        profile = get_profile_store().update_sections(updates)
    except ValidationError as exc:
        raise HTTPException(422, f"Invalid settings: {exc.errors()[:3]}") from exc
    return {"ok": True, "profile": profile.model_dump()}


@router.get("/resumes")
def resumes() -> list[dict[str, Any]]:
    return [asdict(r) for r in list_resumes(get_profile_store().load())]


@router.post("/resumes")
async def upload_resume(file: UploadFile = File(...), resume_id: str = Form(...), label: str = Form(""),
                        tags: str = Form(""), make_default: bool = Form(False)) -> dict[str, Any]:
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "Resume larger than 10 MB")
    try:
        entry = save_uploaded_resume(get_profile_store(), file.filename or "resume.pdf", content, resume_id,
                                     label=label, tags=[t.strip() for t in tags.split(",") if t.strip()],
                                     make_default=make_default)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "resume": entry.model_dump()}
