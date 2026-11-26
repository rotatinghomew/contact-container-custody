"""身份材料解析:壳号为主,标签与纸面点收为旁证。

- 重复扫描只解析到同一只已登记容器,不产生第二只容器;
- 旧式标签复用时,按扫描发生时刻落在哪个有效期窗口(左闭右开)判定;
- 多张身份材料指向不同壳身时判为冲突,拒绝入帐而不是新建实体。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .records import Label, Scan


class IdentityError(Exception):
    """身份材料无法唯一对应到已登记壳身。"""


class IdentityConflict(IdentityError):
    """多份身份材料指向不同壳身。"""


@dataclass(frozen=True)
class Resolution:
    """一次身份解析的结果:落定的壳号及用到的身份材料。"""

    shell: str
    evidence: tuple[str, ...]


class IdentityResolver:
    """按登记册、标签窗口与纸面点收把扫描解析到唯一壳身。"""

    def __init__(self) -> None:
        self._labels: list[Label] = []
        self._paper: dict[str, tuple[str, str]] = {}  # 凭证号 -> (壳号, 接收单位)
        self._known_shells: set[str] = set()

    def register_shell(self, shell_id: str) -> None:
        self._known_shells.add(shell_id)

    def add_label(self, label: Label) -> None:
        self._labels.append(label)

    def shells_for_mark(self, mark: str, at: datetime) -> set[str]:
        """某时刻某标记对应哪些壳身(旧标复用时按窗口区分)。"""
        return {l.shell for l in self._labels if l.mark == mark and l.covers(at)}

    def paper_shell(self, voucher: str) -> Optional[str]:
        known = self._paper.get(voucher)
        return known[0] if known else None

    def note_paper(self, voucher: Optional[str], shell: str, receiver: str) -> None:
        """登记纸面点收:凭证号 -> (壳号, 接收单位)。首次出现即确立。"""
        if voucher:
            self._paper.setdefault(voucher, (shell, receiver))

    def resolve(self, scan: Scan) -> Resolution:
        candidates: set[str] = set()
        evidence: list[str] = []
        if scan.shell:
            candidates.add(scan.shell)
            evidence.append(f"shell:{scan.shell}")
        if scan.mark:
            shells = self.shells_for_mark(scan.mark, scan.at)
            if not shells:
                raise IdentityError(f"标记 {scan.mark!r} 在 {scan.at.isoformat()} 无有效标签")
            if len(shells) > 1:
                raise IdentityError(
                    f"标记 {scan.mark!r} 在 {scan.at.isoformat()} 对应多只壳身: {sorted(shells)}"
                )
            candidates.update(shells)
            evidence.append(f"mark:{scan.mark}")
        if scan.paper:
            known = self._paper.get(scan.paper)
            if known is not None:
                candidates.add(known[0])
            evidence.append(f"paper:{scan.paper}")
        if not candidates:
            raise IdentityError("扫描缺少可解析的身份材料")
        if len(candidates) > 1:
            raise IdentityConflict(f"身份材料指向不同壳身: {sorted(candidates)}")
        shell = next(iter(candidates))
        if shell not in self._known_shells:
            raise IdentityError(f"未登记的壳身: {shell}")
        return Resolution(shell=shell, evidence=tuple(evidence))
