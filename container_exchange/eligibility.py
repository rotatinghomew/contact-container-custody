"""接触部件时间线、清洗证据与用途资格。

口径:
- 壳体、卸料阀、内胆为独立接触部件,换件后按实际新件判定;
- wash 的 coverage 只覆盖作业当时接触到的部件,不延伸至后来安装的部件;
- 撤回按 (issuer, ref, 部件集合) 从原作业中扣除;
- 适用证据取决于实际接触部件、上次用途和清洗覆盖范围:清洗须晚于上次用途,
  且晚于该部件装上的时刻;fresh 内胆凭独立未使用出库凭据替代清洗证明。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .records import PartChange, Purpose, Use, Wash

ROLES = ("shell", "valve", "liner")


class PartTimeline:
    """每个壳身各时刻的实际接触部件。"""

    def __init__(self) -> None:
        self._base: dict[str, dict[str, str]] = {}
        self._changes: list[PartChange] = []

    def register(self, shell_id: str, valve: str, liner: str) -> None:
        self._base[shell_id] = {"shell": shell_id, "valve": valve, "liner": liner}

    def add_change(self, change: PartChange) -> None:
        self._changes.append(change)

    def contact_parts(self, shell: str, at: datetime) -> dict[str, str]:
        """某时刻实际接触的部件(壳身不变,阀/胆按换件推进)。"""
        parts = dict(self._base[shell])
        ordered = sorted(
            (c for c in self._changes if c.shell == shell and c.at <= at),
            key=lambda c: c.at,
        )
        for change in ordered:
            for role in ("valve", "liner"):
                if parts[role] == change.replaced:
                    parts[role] = change.actual_part
        return parts

    def change_installing(self, shell: str, part: str) -> Optional[PartChange]:
        """装上某部件的换件记录(取最近一次)。"""
        matches = [c for c in self._changes if c.shell == shell and c.actual_part == part]
        return max(matches, key=lambda c: c.at) if matches else None


class WashBook:
    """清洗作业凭据与部分撤回。"""

    def __init__(self) -> None:
        self._washes: dict[tuple[str, str], Wash] = {}
        self._revocations: list[Wash] = []

    def add(self, wash: Wash) -> None:
        if wash.revokes:
            self._revocations.append(wash)
        else:
            self._washes[(wash.issuer, wash.ref)] = wash

    def get(self, issuer: str, ref: str) -> Optional[Wash]:
        return self._washes.get((issuer, ref))

    def washes(self) -> list[Wash]:
        return list(self._washes.values())

    def revocations_of(self, issuer: str, ref: str, at: Optional[datetime] = None) -> list[Wash]:
        return [
            r
            for r in self._revocations
            if r.issuer == issuer and r.revokes == ref and (at is None or r.at <= at)
        ]

    def effective_coverage(self, wash: Wash, at: datetime) -> set[str]:
        """截至某时刻,该作业仍有效的覆盖部件(扣除已撤回部分)。"""
        covered = set(wash.coverage)
        for rev in self.revocations_of(wash.issuer, wash.ref, at):
            covered -= set(rev.coverage)
        return covered

    def covering_washes(
        self,
        part: str,
        at: datetime,
        *,
        since: Optional[datetime] = None,
        installed_at: Optional[datetime] = None,
        procedures: tuple[str, ...] = (),
    ) -> list[Wash]:
        """某部件在某时刻可用的清洗凭据:晚于上次用途、晚于装件、未被撤回。"""
        out = []
        for w in self._washes.values():
            if w.at > at:
                continue
            if since is not None and w.at <= since:
                continue
            if installed_at is not None and w.at <= installed_at:
                continue
            if procedures and w.procedure not in procedures:
                continue
            if part not in self.effective_coverage(w, at):
                continue
            out.append(w)
        return sorted(out, key=lambda w: w.at)


@dataclass(frozen=True)
class PartEvidence:
    """单个接触部件的适用证明;kind 为 wash / fresh / missing。"""

    role: str
    part: str
    kind: str
    ref: Optional[str]
    at: Optional[datetime]
    note: str


@dataclass(frozen=True)
class EligibilityReport:
    """一只箱对某用途的资格结论及证据链。"""

    shell: str
    purpose: str
    at: datetime
    eligible: bool
    last_use_class: Optional[str]
    last_use_at: Optional[datetime]
    parts: tuple[PartEvidence, ...]
    reasons: tuple[str, ...]


def evaluate(
    shell: str,
    purpose: Purpose,
    at: datetime,
    *,
    parts: PartTimeline,
    washes: WashBook,
    uses: list[Use],
) -> EligibilityReport:
    """按实际接触部件、上次用途与清洗覆盖范围评估资格。"""
    shell_uses = [u for u in uses if u.shell == shell and u.at <= at]
    last_use = max(shell_uses, key=lambda u: u.at) if shell_uses else None
    reasons: list[str] = []
    if last_use is not None and purpose.permitted_previous_classes:
        if last_use.class_ not in purpose.permitted_previous_classes:
            reasons.append(f"上次用途 {last_use.class_} 不在 {purpose.id} 允许范围")
    since = last_use.at if last_use is not None else None
    contact = parts.contact_parts(shell, at)
    evidences: list[PartEvidence] = []
    for role in purpose.required_parts:
        part = contact.get(role)
        if part is None:
            evidences.append(PartEvidence(role, "", "missing", None, None, f"缺少部件角色 {role}"))
            continue
        change = parts.change_installing(shell, part)
        installed_at = change.at if change else None
        valid = washes.covering_washes(
            part, at, since=since, installed_at=installed_at, procedures=purpose.procedures
        )
        if valid:
            wash = valid[-1]
            evidences.append(
                PartEvidence(role, part, "wash", wash.ref, wash.at, f"{wash.issuer} 作业 {wash.ref} 覆盖 {part}")
            )
            continue
        if (
            role == "liner"
            and purpose.allow_fresh_liner
            and change is not None
            and change.fresh
            and change.new_evidence
            and (since is None or change.at > since)
        ):
            evidences.append(
                PartEvidence(
                    role, part, "fresh", change.new_evidence, change.at,
                    f"独立未使用出库凭据 {change.new_evidence}",
                )
            )
            continue
        evidences.append(PartEvidence(role, part, "missing", None, None, f"部件 {part} 缺少适用证明"))
    for e in evidences:
        if e.kind == "missing":
            reasons.append(e.note)
    return EligibilityReport(
        shell=shell,
        purpose=purpose.id,
        at=at,
        eligible=not reasons,
        last_use_class=last_use.class_ if last_use else None,
        last_use_at=last_use.at if last_use else None,
        parts=tuple(evidences),
        reasons=tuple(reasons),
    )
