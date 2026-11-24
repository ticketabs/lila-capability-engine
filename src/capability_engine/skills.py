"""Versioned SkillRegistry with strict validation, SHA-256 digests, and injection filtering."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional

from .audit import CapabilityAuditLedger, atomic_write_private_json
from .models import CapabilityKind, CapabilityValidationError, SkillSpec, estimate_token_count

_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,80}$")
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

_FORBIDDEN_SKILL_PATTERNS = (
    re.compile(r"<\s*/?\s*tool_call\s*>", re.IGNORECASE),
    re.compile(r"<\|im_(start|end)\|>", re.IGNORECASE),
    re.compile(r"<\s*/?\s*think\s*>", re.IGNORECASE),
    re.compile(r"\bignore\s+(all\s+)?(previous|prior|system)\s+instructions\b", re.IGNORECASE),
    re.compile(r"\bbypass\s+(owner\s+)?(approval|permission|policy|oia)\b", re.IGNORECASE),
    re.compile(r"\boverride\s+(system\s+)?(permission|policy|approval)\b", re.IGNORECASE),
    re.compile(r"\bgrant\s+(execution\s+)?authority\b", re.IGNORECASE),
    re.compile(r"\bskip\s+oia\b", re.IGNORECASE),
)


def _version_sort_key(version: str) -> tuple[int, int, int, str]:
    match = _SEMVER_RE.match(version.strip())
    if not match:
        return (0, 0, 0, version)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), version)


def compute_skill_digest(
    instructions_markdown: str,
    *,
    tags: tuple[str, ...] = (),
    recommended_tools: tuple[str, ...] = (),
    resource_paths: tuple[str, ...] = (),
) -> str:
    """Compute a deterministic SHA-256 digest of normalized skill instructions and metadata."""
    payload = {
        "instructions": instructions_markdown.strip(),
        "tags": list(tags),
        "recommended_tools": list(recommended_tools),
        "resource_paths": list(resource_paths),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_skill_content(
    skill_id: str,
    version: str,
    instructions_markdown: str,
    *,
    max_chars: int = 4000,
    max_tokens: int = 900,
) -> None:
    """Validate skill identifier, semver version, size bounds, and prompt-injection safety."""
    if not _SKILL_ID_RE.match(skill_id or ""):
        raise CapabilityValidationError(f"Invalid skill_id format: {skill_id!r}")
    if not _SEMVER_RE.match(version or ""):
        raise CapabilityValidationError(f"Skill version must be X.Y.Z semver: {version!r}")
    text = (instructions_markdown or "").strip()
    if not text:
        raise CapabilityValidationError("Skill instructions must not be empty.")
    if len(text) > max_chars:
        raise CapabilityValidationError(
            f"Skill '{skill_id}' exceeds max_chars ({len(text)} > {max_chars})."
        )
    est_tokens = estimate_token_count(text)
    if est_tokens > max_tokens:
        raise CapabilityValidationError(
            f"Skill '{skill_id}' exceeds max_tokens ({est_tokens} > {max_tokens})."
        )
    for pattern in _FORBIDDEN_SKILL_PATTERNS:
        if pattern.search(text):
            raise CapabilityValidationError(
                f"Skill '{skill_id}' rejected: forbidden control/override pattern ({pattern.pattern})."
            )


class SkillRegistry:
    """Stores, versions, validates, persists, and retrieves non-authoritative procedural skills."""

    def __init__(
        self,
        audit_ledger: CapabilityAuditLedger,
        *,
        storage_dir: Optional[Path] = None,
        max_chars: int = 4000,
        max_tokens: int = 900,
    ) -> None:
        self._audit = audit_ledger
        self._storage_path: Optional[Path] = (
            Path(storage_dir) / "skills.json" if storage_dir is not None else None
        )
        self._max_chars = int(max_chars)
        self._max_tokens = int(max_tokens)
        self._skills: dict[str, dict[str, SkillSpec]] = {}
        self._latest: dict[str, str] = {}
        if self._storage_path is not None and self._storage_path.exists():
            self._load_persisted()

    def register_skill(
        self,
        skill_id: str,
        version: str,
        title: str,
        summary: str,
        instructions_markdown: str,
        *,
        tags: tuple[str, ...] = (),
        recommended_tools: tuple[str, ...] = (),
        resource_paths: tuple[str, ...] = (),
        origin: str = "builtin",
        actor: str = "system",
    ) -> SkillSpec:
        """Validate, hash, version, and register a reusable procedural skill."""
        clean_id = skill_id.strip()
        clean_version = version.strip()
        clean_instructions = instructions_markdown.strip()
        validate_skill_content(
            clean_id,
            clean_version,
            clean_instructions,
            max_chars=self._max_chars,
            max_tokens=self._max_tokens,
        )
        norm_tags = tuple(str(t).strip() for t in tags if str(t).strip())
        norm_tools = tuple(str(t).strip() for t in recommended_tools if str(t).strip())
        norm_resources = tuple(str(p).strip() for p in resource_paths if str(p).strip())
        digest = compute_skill_digest(
            clean_instructions,
            tags=norm_tags,
            recommended_tools=norm_tools,
            resource_paths=norm_resources,
        )
        spec = SkillSpec(
            skill_id=clean_id,
            version=clean_version,
            title=title.strip() or clean_id,
            summary=summary.strip(),
            instructions_markdown=clean_instructions,
            content_digest=digest,
            tags=norm_tags,
            recommended_tools=norm_tools,
            resource_paths=norm_resources,
            estimated_tokens=estimate_token_count(clean_instructions),
            origin=origin,
        )
        ver_map = self._skills.setdefault(clean_id, {})
        ver_map[clean_version] = spec
        best = max(ver_map.keys(), key=_version_sort_key)
        self._latest[clean_id] = best
        self._persist()
        self._audit.record(
            actor,
            "skill_registered",
            f"{clean_id}@{clean_version}",
            target_kind=CapabilityKind.SKILL.value,
            digest=digest,
            details={"origin": origin, "estimated_tokens": spec.estimated_tokens},
        )
        return spec

    def get(self, skill_id: str, version: Optional[str] = None) -> Optional[SkillSpec]:
        """Return the requested skill version, or the latest semver when omitted."""
        ver_map = self._skills.get(skill_id)
        if not ver_map:
            return None
        target_ver = version or self._latest.get(skill_id)
        return ver_map.get(target_ver) if target_ver else None

    def versions(self, skill_id: str) -> list[SkillSpec]:
        """Return all registered versions of `skill_id` ordered oldest-to-newest."""
        ver_map = self._skills.get(skill_id, {})
        return [ver_map[k] for k in sorted(ver_map.keys(), key=_version_sort_key)]

    def all_latest(self) -> list[SkillSpec]:
        """Return the latest version of every registered skill."""
        return [
            self._skills[sid][self._latest[sid]]
            for sid in sorted(self._skills.keys())
            if sid in self._latest and self._latest[sid] in self._skills[sid]
        ]

    def set_active_version(
        self,
        skill_id: str,
        version: str,
        *,
        actor: str = "owner",
    ) -> SkillSpec:
        """Switch the active selected version of an existing skill (e.g. for evolution rollback)."""
        clean_id = (skill_id or "").strip()
        clean_ver = (version or "").strip()
        ver_map = self._skills.get(clean_id)
        if not ver_map or clean_ver not in ver_map:
            raise CapabilityValidationError(
                f"Cannot switch skill '{clean_id}' to unknown version '{clean_ver}'."
            )
        prior_ver = self._latest.get(clean_id)
        spec = ver_map[clean_ver]
        self._latest[clean_id] = clean_ver
        self._persist()
        self._audit.record(
            actor,
            "skill_version_switched",
            f"{clean_id}@{clean_ver}",
            target_kind=CapabilityKind.SKILL.value,
            digest=spec.content_digest,
            details={"from_version": prior_ver, "to_version": clean_ver},
        )
        return spec

    def _persist(self) -> None:
        if self._storage_path is None:
            return
        payload = {
            "latest": dict(self._latest),
            "skills": {
                sid: [
                    {
                        "skill_id": s.skill_id,
                        "version": s.version,
                        "title": s.title,
                        "summary": s.summary,
                        "instructions_markdown": s.instructions_markdown,
                        "content_digest": s.content_digest,
                        "tags": list(s.tags),
                        "recommended_tools": list(s.recommended_tools),
                        "resource_paths": list(s.resource_paths),
                        "estimated_tokens": s.estimated_tokens,
                        "origin": s.origin,
                    }
                    for s in self.versions(sid)
                ]
                for sid in sorted(self._skills.keys())
            },
        }
        atomic_write_private_json(self._storage_path, payload)

    def _load_persisted(self) -> None:
        assert self._storage_path is not None
        data = json.loads(self._storage_path.read_text(encoding="utf-8"))
        for sid, items in (data.get("skills") or {}).items():
            for raw in items:
                clean_inst = str(raw["instructions_markdown"]).strip()
                tags = tuple(raw.get("tags") or ())
                rec_tools = tuple(raw.get("recommended_tools") or ())
                res_paths = tuple(raw.get("resource_paths") or ())
                validate_skill_content(
                    raw["skill_id"],
                    raw["version"],
                    clean_inst,
                    max_chars=self._max_chars,
                    max_tokens=self._max_tokens,
                )
                expected_digest = compute_skill_digest(
                    clean_inst,
                    tags=tags,
                    recommended_tools=rec_tools,
                    resource_paths=res_paths,
                )
                if raw.get("content_digest") != expected_digest:
                    raise CapabilityValidationError(
                        f"Persisted skill '{raw['skill_id']}@{raw['version']}' failed digest check."
                    )
                spec = SkillSpec(
                    skill_id=raw["skill_id"],
                    version=raw["version"],
                    title=raw["title"],
                    summary=raw["summary"],
                    instructions_markdown=clean_inst,
                    content_digest=expected_digest,
                    tags=tags,
                    recommended_tools=rec_tools,
                    resource_paths=res_paths,
                    estimated_tokens=int(raw.get("estimated_tokens") or estimate_token_count(clean_inst)),
                    origin=str(raw.get("origin") or "persisted"),
                )
                self._skills.setdefault(sid, {})[spec.version] = spec
        persisted_latest = data.get("latest") or {}
        for sid, ver_map in self._skills.items():
            if not ver_map:
                continue
            chosen = persisted_latest.get(sid)
            if chosen in ver_map:
                self._latest[sid] = chosen
            else:
                self._latest[sid] = max(ver_map.keys(), key=_version_sort_key)
