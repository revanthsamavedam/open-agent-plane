"""Versioned SKILL.md-style skill definitions."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


class Skill(BaseModel):
    name: str
    version: str = "0.0.0"
    description: str = ""
    write: bool = False  # write skills produce drafts that need approval
    body: str = ""


def load_skills() -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        text = path.read_text()
        meta: dict = {}
        body = text
        if text.startswith("---"):
            _, front, body = text.split("---", 2)
            meta = yaml.safe_load(front) or {}
        skill = Skill.model_validate({**meta, "body": body.strip()})
        skills[skill.name] = skill
    return skills
