"""保管台账:扫描对照、补正、保管期间与未吻合判定。

- 同一次 handoff 内,交出方与接收方对同一壳身的录入相互对照;
- 补正扫描(replaces_shell)取代同 handoff 同 role 的早前录入,原录入保留可查;
- 只有双方吻合的交接才移动确认保管;单边录入只构成"据报保管",
  任何一方都不能凭它把箱再交给另一个厂。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .records import Scan


@dataclass
class ScanEntry:
    """一条已入帐的扫描:解析后的壳身 + 是否被补正取代。"""

    seq: int
    scan: Scan
    shell: str
    superseded_by: Optional[int] = None

    @property
    def effective(self) -> bool:
        return self.superseded_by is None


@dataclass(frozen=True)
class CustodyPeriod:
    """一段保管期间;start 为 None 表示台账起点之前,end 为 None 表示至今。"""

    holder: str
    start: Optional[datetime]
    end: Optional[datetime]

    def contains(self, moment: datetime) -> bool:
        if self.start is not None and moment < self.start:
            return False
        if self.end is not None and moment >= self.end:
            return False
        return True


class CustodyLedger:
    def __init__(self) -> None:
        self._entries: list[ScanEntry] = []
        self._seq = 0

    def ingest(self, scan: Scan, shell: str) -> tuple[ScanEntry, Optional[ScanEntry]]:
        """入帐一条扫描;若是补正,返回被取代的旧条目。"""
        self._seq += 1
        entry = ScanEntry(seq=self._seq, scan=scan, shell=shell)
        superseded = None
        if scan.replaces_shell is not None:
            superseded = self._find_supersede_target(scan)
            if superseded is not None:
                superseded.superseded_by = entry.seq
        self._entries.append(entry)
        return entry, superseded

    def _find_supersede_target(self, scan: Scan) -> Optional[ScanEntry]:
        candidates = [
            e
            for e in self._entries
            if e.effective
            and e.scan.handoff == scan.handoff
            and e.scan.role == scan.role
            and e.shell == scan.replaces_shell
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda e: e.seq)

    def entries(
        self,
        shell: Optional[str] = None,
        handoff: Optional[str] = None,
        effective_only: bool = True,
    ) -> list[ScanEntry]:
        out = []
        for e in self._entries:
            if effective_only and not e.effective:
                continue
            if shell is not None and e.shell != shell:
                continue
            if handoff is not None and e.scan.handoff != handoff:
                continue
            out.append(e)
        return out

    def all_entries(self) -> list[ScanEntry]:
        """含被补正取代的旧条目,供回查。"""
        return list(self._entries)

    def handoff_view(self, handoff: str) -> dict[str, dict[str, Optional[ScanEntry]]]:
        """一次交接中每个壳身的双方录入情况。"""
        view: dict[str, dict[str, Optional[ScanEntry]]] = {}
        for e in self.entries(handoff=handoff):
            slot = view.setdefault(e.shell, {"from": None, "to": None})
            slot[e.scan.role] = e
        return view

    def matched(self, handoff: str, shell: str) -> bool:
        slot = self.handoff_view(handoff).get(shell)
        return bool(slot and slot["from"] and slot["to"])

    def transfer_moment(self, handoff: str, shell: str) -> Optional[datetime]:
        """双方吻合的交接以较晚一条录入的时刻为保管转移点。"""
        if not self.matched(handoff, shell):
            return None
        slot = self.handoff_view(handoff)[shell]
        return max(slot["from"].scan.at, slot["to"].scan.at)  # type: ignore[union-attr]

    def unmatched_handoff(self, shell: str) -> Optional[str]:
        """该壳身最近一次交接活动是否只有单边录入;返回 handoff 号。"""
        entries = self.entries(shell=shell)
        if not entries:
            return None
        latest = max(entries, key=lambda e: (e.scan.at, e.seq))
        handoff = latest.scan.handoff
        return None if self.matched(handoff, shell) else handoff

    def custody_timeline(self, shell: str) -> list[CustodyPeriod]:
        """按吻合交接推出的保管期间序列。"""
        entries = self.entries(shell=shell)
        if not entries:
            return []
        first = min(entries, key=lambda e: (e.scan.at, e.seq))
        holder = first.scan.from_party
        transfers: dict[str, tuple[datetime, str]] = {}
        for e in entries:
            handoff = e.scan.handoff
            if handoff not in transfers and self.matched(handoff, shell):
                moment = self.transfer_moment(handoff, shell)
                if moment is not None:
                    transfers[handoff] = (moment, e.scan.to_party)
        periods: list[CustodyPeriod] = []
        start: Optional[datetime] = None
        for moment, to_party in sorted(transfers.values(), key=lambda x: x[0]):
            periods.append(CustodyPeriod(holder=holder, start=start, end=moment))
            holder = to_party
            start = moment
        periods.append(CustodyPeriod(holder=holder, start=start, end=None))
        return periods

    def current_holder(self, shell: str) -> Optional[str]:
        """确认保管人(仅按双方吻合的交接)。"""
        periods = self.custody_timeline(shell)
        return periods[-1].holder if periods else None

    def reported_holder(self, shell: str) -> Optional[str]:
        """据报保管人:按最新一条有效录入推断实物所在,用于通知与圈定。"""
        entries = self.entries(shell=shell)
        if not entries:
            return None
        latest = max(entries, key=lambda e: (e.scan.at, e.seq))
        if latest.scan.role == "to":
            return latest.scan.to_party
        return latest.scan.from_party

    def custody_period_at(self, shell: str, moment: datetime) -> Optional[CustodyPeriod]:
        """某时刻落在哪段保管期间(用于核清补传扫描的归属)。"""
        for period in self.custody_timeline(shell):
            if period.contains(moment):
                return period
        return None
