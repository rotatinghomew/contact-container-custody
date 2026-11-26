"""本地联调演示:用合成记录走一遍身份解析、交接对照、撤回处置与回查。

运行:python3 -m container_exchange.demo
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .builder import build_service
from .records import load_bundle

DATA = Path(__file__).resolve().parents[1] / "data" / "source_records.json"


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def show(title: str) -> None:
    print(f"\n== {title} ==")


def main() -> None:
    bundle = load_bundle(DATA)
    svc = build_service(bundle)

    show("交接对照(含补正)")
    for handoff in ("D31", "D32", "D35"):
        status = svc.handoff_status(handoff)
        print(handoff, status["state"], status["shells"],
              "被补正取代:", status["superseded"] or "无")

    show("保管现状(确认 / 据报)")
    for shell in svc.known_shells():
        print(shell, svc.current_holder(shell), "/", svc.reported_holder(shell))

    show("清洗部分撤回的处置通知")
    for n in svc.notices:
        print(n.kind, n.shell, "->", n.party, n.detail)

    show("为何可以再用(证据链)")
    for shell, moment in (("V11", "2026-11-26T10:30:00+08:00"),
                          ("V12", "2026-11-26T12:30:00+08:00")):
        report = svc.why_reusable(shell, "binder", ts(moment))
        print(shell, moment, "可用" if report.eligible else "不可用", report.reasons or "")
        for part in report.parts:
            print("   ", part.role, part.part, part.kind, part.ref or "", part.note)

    show("补传扫描归属哪次保管期间")
    correction = next(s for s in bundle.scans if s.replaces_shell)
    attribution = svc.attribute_scan(correction)
    print(correction.handoff, correction.shell, correction.at, "->", attribution)

    show("处理进度回查(V13)")
    status = svc.processing_status("V13")
    print("确认保管:", status["confirmed_holder"], "限制:", status["hold"])
    print("隔离交接单:", status["isolation_orders"])


if __name__ == "__main__":
    main()
