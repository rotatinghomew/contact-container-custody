"""交接服务门面:记录接入、出箱申请、资格判定、撤回处置与回查。

职责对应业务要求:
- 用箱厂领取实际可用的容器(出箱申请可并发、可撤回);
- 保管方知道哪些箱可以移交;交接未吻合的箱任何一方不能凭单边录入再交厂;
- 扫描绑定错了可补交更正及点收依据,期间事实保留可查;
- 清洗部分撤回时圈定受影响箱:未交接的退出可领清单、已领未灌装通知停用、
  已灌装的开出按实物识别的隔离交接单;范围外及已换件且有独立证据的继续可用;
- 交接/接触记录更新后,不得沿用早先资格结果发出交接(资格与台账版本绑定);
- 现场接口确认实物领用与隔离交回;库管可回查为何可再用、在谁手上、处理到哪步;
- 补传扫描可核清发生在哪次保管期间。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from . import eligibility
from .eligibility import EligibilityReport, PartTimeline, WashBook
from .identity import IdentityResolver
from .ledger import CustodyLedger, CustodyPeriod
from .records import Label, PartChange, Purpose, Scan, Shell, Use, Wash


class ServiceError(Exception):
    """交接服务通用错误。"""


class StaleQualification(ServiceError):
    """资格结果落后于台账版本,不得用于发出交接。"""


class RequestStateError(ServiceError):
    """出箱申请状态不允许该操作。"""


class TransferBlocked(ServiceError):
    """该箱当前不允许移交(未吻合/停用/隔离等)。"""


@dataclass(frozen=True)
class Notice:
    """处置通知:停用、隔离交接单、退出可领清单、解除限制。"""

    kind: str  # stop_use / isolation_order / delist / hold_lifted
    shell: str
    party: Optional[str]
    at: datetime
    detail: str
    ref: Optional[str] = None


@dataclass
class IsolationOrder:
    """按实物识别的隔离交接单。"""

    id: str
    shell: str
    custodian: Optional[str]
    wash_ref: str
    created_at: datetime
    status: str = "open"  # open -> returned
    returned_at: Optional[datetime] = None


@dataclass
class Qualification:
    """一次资格结果,与台账版本绑定;版本落后即不得用于发出交接。"""

    request_id: str
    purpose: str
    at: datetime
    version: int
    candidates: tuple[str, ...]


@dataclass
class BoxRequest:
    """出箱申请。"""

    id: str
    plant: str
    purpose: str
    state: str = "open"  # open / withdrawn / fulfilled
    qualification: Optional[Qualification] = None
    claimed: Optional[str] = None


class ExchangeService:
    def __init__(self) -> None:
        self.parts = PartTimeline()
        self.washes = WashBook()
        self.ledger = CustodyLedger()
        self.identity = IdentityResolver()
        self.purposes: dict[str, Purpose] = {}
        self.uses: list[Use] = []
        self.requests: dict[str, BoxRequest] = {}
        self.notices: list[Notice] = []
        self.isolation_orders: dict[str, IsolationOrder] = {}
        self.holds: dict[str, dict] = {}  # shell -> 限制(停用/隔离/退出可领)
        self.version = 0  # 台账版本:任何交接/接触记录更新都会递增
        self.audit: list[dict] = []
        self._shells: dict[str, Shell] = {}
        self._order_seq = 0

    # ------------------------------------------------------------------
    # 基础登记与记录接入
    # ------------------------------------------------------------------
    def _record_event(self, kind: str, shell: Optional[str] = None, **detail) -> None:
        self.version += 1
        self.audit.append({"seq": self.version, "kind": kind, "shell": shell, **detail})

    def register_shell(self, shell: Shell) -> None:
        self._shells[shell.id] = shell
        self.parts.register(shell.id, shell.valve, shell.liner)
        self.identity.register_shell(shell.id)
        self._record_event("register_shell", shell.id)

    def add_label(self, label: Label) -> None:
        self.identity.add_label(label)
        self._record_event("add_label", label.shell, mark=label.mark)

    def add_purpose(self, purpose: Purpose) -> None:
        self.purposes[purpose.id] = purpose
        self._record_event("add_purpose", None, purpose=purpose.id)

    def known_shells(self) -> tuple[str, ...]:
        return tuple(sorted(self._shells))

    def ingest_scan(self, scan: Scan) -> dict:
        """接入一条装卸扫描;身份冲突时抛 IdentityError,不产生第二只容器。"""
        resolution = self.identity.resolve(scan)
        entry, superseded = self.ledger.ingest(scan, resolution.shell)
        self.identity.note_paper(scan.paper, resolution.shell, scan.to_party)
        self._record_event(
            "scan",
            resolution.shell,
            handoff=scan.handoff,
            role=scan.role,
            at=scan.at.isoformat(),
            reported_at=scan.arrived_at.isoformat(),
        )
        return {
            "shell": resolution.shell,
            "evidence": resolution.evidence,
            "seq": entry.seq,
            "superseded_seq": superseded.seq if superseded else None,
            "matched": self.ledger.matched(scan.handoff, resolution.shell),
        }

    def ingest_wash(self, wash: Wash) -> list[Notice]:
        """接入清洗作业或部分撤回;撤回时圈定受影响箱并发出处置通知。"""
        self.washes.add(wash)
        self._record_event("wash", None, ref=wash.ref, revokes=wash.revokes)
        if wash.revokes:
            return self._apply_revocation(wash)
        self._refresh_holds(wash.at)
        return []

    def ingest_part_change(self, change: PartChange) -> None:
        self.parts.add_change(change)
        self._record_event("part_change", change.shell, actual_part=change.actual_part)

    def ingest_use(self, use: Use) -> None:
        self.uses.append(use)
        self._record_event("use", use.shell, class_=use.class_, at=use.at.isoformat())

    # ------------------------------------------------------------------
    # 清洗部分撤回的圈定与处置
    # ------------------------------------------------------------------
    def _apply_revocation(self, rev: Wash) -> list[Notice]:
        original = self.washes.get(rev.issuer, rev.revokes or "")
        if original is None:
            raise ServiceError(f"撤回指向未知作业: {rev.issuer}/{rev.revokes}")
        revoked_parts = set(rev.coverage)
        emitted: list[Notice] = []
        for shell_id in self.known_shells():
            contact = self.parts.contact_parts(shell_id, rev.at)
            hit = revoked_parts & set(contact.values())
            if not hit:
                continue  # 范围外,或受影响部位已更换(如另有独立证据的新件)
            shell_uses = [u for u in self.uses if u.shell == shell_id and u.at <= rev.at]
            last_use_at = max((u.at for u in shell_uses), default=None)
            still_covered = all(
                self.washes.covering_washes(part, rev.at, since=last_use_at) for part in hit
            )
            if still_covered:
                continue  # 受影响部件另有有效清洗证据
            holder = self.ledger.reported_holder(shell_id)
            filled = any(u.shell == shell_id and u.at >= original.at for u in self.uses)
            if filled:
                order = self._open_isolation_order(shell_id, holder, rev)
                self.holds[shell_id] = {
                    "kind": "isolation",
                    "parts": frozenset(hit),
                    "since": rev.at,
                    "order": order.id,
                }
                notice = Notice(
                    "isolation_order", shell_id, holder, rev.at,
                    f"清洗作业 {rev.revokes} 部分撤回且已灌装,开出隔离交接单 {order.id}",
                    ref=order.id,
                )
            elif holder is not None and holder != rev.issuer:
                self.holds[shell_id] = {
                    "kind": "stop_use",
                    "parts": frozenset(hit),
                    "since": rev.at,
                }
                notice = Notice(
                    "stop_use", shell_id, holder, rev.at,
                    f"清洗作业 {rev.revokes} 部分撤回,通知现保管人 {holder} 停止使用",
                )
            else:
                self.holds[shell_id] = {
                    "kind": "delist",
                    "parts": frozenset(hit),
                    "since": rev.at,
                }
                notice = Notice(
                    "delist", shell_id, holder, rev.at,
                    f"清洗作业 {rev.revokes} 部分撤回,未交接箱退出可领清单",
                )
            self.notices.append(notice)
            emitted.append(notice)
        return emitted

    def _open_isolation_order(self, shell: str, custodian: Optional[str], rev: Wash) -> IsolationOrder:
        self._order_seq += 1
        order = IsolationOrder(
            id=f"ISO-{self._order_seq}",
            shell=shell,
            custodian=custodian,
            wash_ref=rev.revokes or "",
            created_at=rev.at,
        )
        self.isolation_orders[order.id] = order
        return order

    def _refresh_holds(self, at: datetime) -> None:
        """重新清洗覆盖受影响部件后解除限制(隔离箱须先交回)。"""
        for shell_id, hold in list(self.holds.items()):
            if hold["kind"] == "isolation":
                order = self.isolation_orders[hold["order"]]
                if order.status != "returned":
                    continue
            lifted = all(
                self.washes.covering_washes(part, at, since=hold["since"])
                for part in hold["parts"]
            )
            if lifted:
                del self.holds[shell_id]
                self.notices.append(
                    Notice("hold_lifted", shell_id, None, at, "重新清洗覆盖受影响部件,解除限制")
                )

    # ------------------------------------------------------------------
    # 出箱申请(可并发、可撤回;资格与版本绑定)
    # ------------------------------------------------------------------
    def open_request(self, request_id: str, plant: str, purpose: str) -> BoxRequest:
        if request_id in self.requests:
            raise RequestStateError(f"申请号已存在: {request_id}")
        if purpose not in self.purposes:
            raise ServiceError(f"未知用途: {purpose}")
        req = BoxRequest(id=request_id, plant=plant, purpose=purpose)
        self.requests[request_id] = req
        return req

    def qualify(self, request_id: str, at: datetime) -> Qualification:
        """评估当前可领的箱;结果绑定台账版本。"""
        req = self._open_request(request_id)
        purpose = self.purposes[req.purpose]
        candidates = tuple(
            shell
            for shell, _holder in self._releasable(
                purpose, at, exclude_plant=req.plant, exclude_reserved_by=request_id
            )
        )
        qualification = Qualification(request_id, req.purpose, at, self.version, candidates)
        req.qualification = qualification
        if req.claimed and req.claimed not in candidates:
            req.claimed = None  # 重新评估后原预留不再成立
        return qualification

    def claim(self, request_id: str, shell: str) -> None:
        """预留一只候选箱;并发的其他申请不能再预留同一只。"""
        req = self._open_request(request_id)
        if req.qualification is None or shell not in req.qualification.candidates:
            raise RequestStateError(f"{shell} 不在当前资格结果中")
        if self._reserved_by_other(shell, request_id):
            raise RequestStateError(f"{shell} 已被其他申请预留")
        req.claimed = shell

    def withdraw(self, request_id: str) -> None:
        req = self._open_request(request_id)
        req.state = "withdrawn"
        req.claimed = None

    def release(self, request_id: str, shell: str, handoff: str, at: datetime) -> dict:
        """保管方按申请发出交接(交出方录入)。

        资格结果之后若台账有更新(交接、换件、清洗、用途等),不得沿用旧结果。
        """
        req = self._open_request(request_id)
        if req.claimed != shell:
            raise RequestStateError(f"申请 {request_id} 未预留 {shell}")
        qualification = req.qualification
        if qualification is None or qualification.version != self.version:
            raise StaleQualification(
                f"资格结果版本 {None if qualification is None else qualification.version} "
                f"落后于台账版本 {self.version},须重新评估"
            )
        self._check_transferable(shell)
        holder = self.ledger.current_holder(shell)
        if holder is None:
            raise TransferBlocked(f"{shell} 无保管记录,不能移交")
        if self.ledger.entries(shell=shell, handoff=handoff):
            raise ServiceError(f"{handoff} 中已有 {shell} 的录入")
        scan = Scan(
            handoff=handoff, from_party=holder, to_party=req.plant,
            role="from", shell=shell, at=at,
        )
        result = self.ingest_scan(scan)
        req.state = "fulfilled"
        req.claimed = None
        return result

    def _check_transferable(self, shell: str) -> None:
        unmatched = self.ledger.unmatched_handoff(shell)
        if unmatched:
            raise TransferBlocked(f"{shell} 在 {unmatched} 交接未吻合,不能凭单边录入再交厂")
        if shell in self.holds:
            raise TransferBlocked(f"{shell} 处于 {self.holds[shell]['kind']} 限制中")

    def _reserved_by_other(self, shell: str, request_id: str) -> bool:
        return any(
            r.id != request_id and r.state == "open" and r.claimed == shell
            for r in self.requests.values()
        )

    def _open_request(self, request_id: str) -> BoxRequest:
        req = self.requests.get(request_id)
        if req is None:
            raise RequestStateError(f"未知申请: {request_id}")
        if req.state != "open":
            raise RequestStateError(f"申请 {request_id} 已 {req.state}")
        return req

    def _releasable(
        self,
        purpose: Purpose,
        at: datetime,
        *,
        exclude_plant: Optional[str] = None,
        exclude_reserved_by: Optional[str] = None,
    ) -> list[tuple[str, str]]:
        """当前可移交且资格成立的箱及其保管方。"""
        out = []
        for shell_id in self.known_shells():
            if shell_id in self.holds:
                continue
            holder = self.ledger.current_holder(shell_id)
            if holder is None or holder == exclude_plant:
                continue
            if self.ledger.unmatched_handoff(shell_id):
                continue
            if exclude_reserved_by and self._reserved_by_other(shell_id, exclude_reserved_by):
                continue
            report = eligibility.evaluate(
                shell_id, purpose, at, parts=self.parts, washes=self.washes, uses=self.uses
            )
            if report.eligible:
                out.append((shell_id, holder))
        return out

    # ------------------------------------------------------------------
    # 现场接口
    # ------------------------------------------------------------------
    def confirm_physical_claim(self, handoff: str, shell: str, by: str, at: datetime) -> dict:
        """现场确认实物领用(接收方录入),与交出方记录对照。"""
        slot = self.ledger.handoff_view(handoff).get(shell)
        if not slot or not slot["from"]:
            raise ServiceError(f"{handoff} 中无 {shell} 的交出记录")
        if slot["to"]:
            raise ServiceError(f"{handoff} 中 {shell} 已确认领用")
        from_scan = slot["from"].scan
        if by != from_scan.to_party:
            raise ServiceError(f"领用方 {by} 与交接对象 {from_scan.to_party} 不符")
        scan = Scan(
            handoff=handoff, from_party=from_scan.from_party, to_party=by,
            role="to", shell=shell, at=at,
        )
        return self.ingest_scan(scan)

    def confirm_isolation_return(self, order_id: str, by: str, at: datetime) -> IsolationOrder:
        """现场确认隔离交回:按隔离交接单号完成一次交接,实物回到处置方。"""
        order = self.isolation_orders.get(order_id)
        if order is None:
            raise ServiceError(f"未知隔离交接单: {order_id}")
        if order.status != "open":
            raise ServiceError(f"隔离交接单 {order_id} 已 {order.status}")
        custodian = order.custodian or self.ledger.reported_holder(order.shell)
        if custodian is None:
            raise ServiceError(f"{order.shell} 无保管记录,无法交回")
        self.ingest_scan(Scan(order_id, custodian, by, "from", at, shell=order.shell))
        self.ingest_scan(Scan(order_id, custodian, by, "to", at, shell=order.shell))
        order.status = "returned"
        order.returned_at = at
        self._record_event("isolation_return", order.shell, order=order_id)
        self._refresh_holds(at)
        return order

    # ------------------------------------------------------------------
    # 回查
    # ------------------------------------------------------------------
    def why_reusable(self, shell: str, purpose_id: str, at: datetime) -> EligibilityReport:
        """每只箱为何可以再用:逐部件的证据链。"""
        purpose = self.purposes.get(purpose_id)
        if purpose is None:
            raise ServiceError(f"未知用途: {purpose_id}")
        return eligibility.evaluate(
            shell, purpose, at, parts=self.parts, washes=self.washes, uses=self.uses
        )

    def current_holder(self, shell: str) -> Optional[str]:
        return self.ledger.current_holder(shell)

    def reported_holder(self, shell: str) -> Optional[str]:
        return self.ledger.reported_holder(shell)

    def custody_timeline(self, shell: str) -> list[CustodyPeriod]:
        return self.ledger.custody_timeline(shell)

    def custody_period_at(self, shell: str, at: datetime) -> Optional[CustodyPeriod]:
        return self.ledger.custody_period_at(shell, at)

    def attribute_scan(self, scan: Scan) -> dict:
        """核清一条(补传的)扫描发生在哪次保管期间。"""
        resolution = self.identity.resolve(scan)
        period = self.ledger.custody_period_at(resolution.shell, scan.at)
        return {
            "shell": resolution.shell,
            "at": scan.at,
            "holder": period.holder if period else None,
            "period_start": period.start if period else None,
            "period_end": period.end if period else None,
        }

    def handoff_status(self, handoff: str) -> dict:
        """一次交接的对照情况,含被补正取代的旧录入。"""
        view = self.ledger.handoff_view(handoff)
        shells = {
            shell: {
                "from": slot["from"] is not None,
                "to": slot["to"] is not None,
                "matched": slot["from"] is not None and slot["to"] is not None,
            }
            for shell, slot in view.items()
        }
        superseded = [
            {
                "shell": e.shell,
                "role": e.scan.role,
                "at": e.scan.at,
                "superseded_by": e.superseded_by,
            }
            for e in self.ledger.entries(handoff=handoff, effective_only=False)
            if not e.effective
        ]
        state = "matched" if shells and all(s["matched"] for s in shells.values()) else "open"
        return {"handoff": handoff, "state": state, "shells": shells, "superseded": superseded}

    def claimable(self, purpose_id: str, at: datetime) -> list[dict]:
        """可领清单:当前可移交且资格成立的箱。"""
        purpose = self.purposes.get(purpose_id)
        if purpose is None:
            raise ServiceError(f"未知用途: {purpose_id}")
        return [
            {"shell": shell, "holder": holder}
            for shell, holder in self._releasable(purpose, at)
        ]

    def transferable(self, holder: str) -> list[str]:
        """保管方视角:自己手上当前可以移交的箱。"""
        out = []
        for shell_id in self.known_shells():
            if self.ledger.current_holder(shell_id) != holder:
                continue
            if shell_id in self.holds:
                continue
            if self.ledger.unmatched_handoff(shell_id):
                continue
            out.append(shell_id)
        return out

    def request_state(self, request_id: str) -> dict:
        req = self.requests.get(request_id)
        if req is None:
            raise ServiceError(f"未知申请: {request_id}")
        qualification = req.qualification
        return {
            "id": req.id,
            "plant": req.plant,
            "purpose": req.purpose,
            "state": req.state,
            "claimed": req.claimed,
            "qualification_version": qualification.version if qualification else None,
            "stale": bool(qualification and qualification.version != self.version),
        }

    def processing_status(self, shell: str) -> dict:
        """库管回查:在谁手上、受何限制、相关处理截至哪一步。"""
        last_use = max((u for u in self.uses if u.shell == shell), key=lambda u: u.at, default=None)
        return {
            "shell": shell,
            "confirmed_holder": self.ledger.current_holder(shell),
            "reported_holder": self.ledger.reported_holder(shell),
            "unmatched_handoff": self.ledger.unmatched_handoff(shell),
            "hold": self.holds.get(shell),
            "isolation_orders": [
                {"id": o.id, "status": o.status, "created_at": o.created_at, "returned_at": o.returned_at}
                for o in self.isolation_orders.values()
                if o.shell == shell
            ],
            "last_use": {"class": last_use.class_, "at": last_use.at} if last_use else None,
            "claimed_by": [r.id for r in self.requests.values() if r.state == "open" and r.claimed == shell],
            "notices": [n for n in self.notices if n.shell == shell],
            "history": [e for e in self.audit if e.get("shell") == shell],
        }
