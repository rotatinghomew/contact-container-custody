"""把一批交换记录按到达顺序接入交接服务。

扫描按 reported_at(到达平台时间)入帐,其余记录按 at(发生时间)入帐,
以复现平台实际看到事件的次序;可用 until 截断到某一时刻。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .records import RecordBundle
from .service import ExchangeService


def ingest_bundle(
    svc: ExchangeService, bundle: RecordBundle, until: Optional[datetime] = None
) -> ExchangeService:
    for shell in bundle.shells:
        svc.register_shell(shell)
    for label in bundle.labels:
        svc.add_label(label)
    for purpose in bundle.purposes:
        svc.add_purpose(purpose)
    events: list[tuple[datetime, int, str, object]] = []
    for wash in bundle.washes:
        events.append((wash.at, 0, "wash", wash))
    for change in bundle.part_changes:
        events.append((change.at, 1, "part_change", change))
    for use in bundle.uses:
        events.append((use.at, 2, "use", use))
    for scan in bundle.scans:
        events.append((scan.arrived_at, 3, "scan", scan))
    events.sort(key=lambda e: (e[0], e[1]))
    for moment, _prio, kind, payload in events:
        if until is not None and moment > until:
            continue
        if kind == "wash":
            svc.ingest_wash(payload)  # type: ignore[arg-type]
        elif kind == "part_change":
            svc.ingest_part_change(payload)  # type: ignore[arg-type]
        elif kind == "use":
            svc.ingest_use(payload)  # type: ignore[arg-type]
        else:
            svc.ingest_scan(payload)  # type: ignore[arg-type]
    return svc


def build_service(bundle: RecordBundle, until: Optional[datetime] = None) -> ExchangeService:
    return ingest_bundle(ExchangeService(), bundle, until)
