"""记录模型:《容器交换记录》各类凭据的结构化表示。

口径依据《容器交换记录.md》:
- 器具登记册按壳身刻号识别实体,标签(issuer+mark)在领用周次内才有对应关系;
- 纸面点收以壳号、接收单位及交接凭证号为依据;
- reported_at 是到达平台的时间,at 为作业记载的发生时间;
- wash 的 coverage 是作业当时接触到的部件编号,不延伸至后来安装的部件;
- 证据撤回由 issuer、ref、部件集合标识;fresh 表示有独立未使用出库凭据。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional


def parse_instant(value: str) -> datetime:
    """解析带时区的时刻;缺时区视为坏数据。"""
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        raise ValueError(f"时刻缺少时区: {value!r}")
    return instant


def parse_day(value: str) -> date:
    return date.fromisoformat(value)


@dataclass(frozen=True)
class Shell:
    """壳身登记:壳号及出厂接触部件(卸料阀、内胆)。"""

    id: str
    valve: str
    liner: str


@dataclass(frozen=True)
class Label:
    """标签刻印:issuer+mark 在 [valid_from, valid_to) 内对应壳身(左闭右开)。"""

    issuer: str
    mark: str
    shell: str
    valid_from: date
    valid_to: date

    def covers(self, moment: datetime) -> bool:
        day = moment.date()
        return self.valid_from <= day < self.valid_to


@dataclass(frozen=True)
class Scan:
    """装卸扫描:handoff 内某方(role)对某实体的录入。

    role 为 "from"(交出方)或 "to"(接收方);身份材料可以是壳号、
    标签 mark、纸面凭证号的任意组合;replaces_shell 表示本条是对
    同 handoff 同 role 早前扫描的补正。
    """

    handoff: str
    from_party: str
    to_party: str
    role: str
    at: datetime
    shell: Optional[str] = None
    mark: Optional[str] = None
    paper: Optional[str] = None
    replaces_shell: Optional[str] = None
    reported_at: Optional[datetime] = None

    @property
    def arrived_at(self) -> datetime:
        """到达平台的时间;缺省视同即时上报。"""
        return self.reported_at or self.at


@dataclass(frozen=True)
class Wash:
    """清洗作业凭据;revokes 非空时本条为对该作业的部分撤回。"""

    issuer: str
    ref: str
    at: datetime
    coverage: tuple[str, ...] = ()
    procedure: Optional[str] = None
    revokes: Optional[str] = None


@dataclass(frozen=True)
class PartChange:
    """换件材料:replaced 旧件换下,actual_part 实际新件装上。"""

    shell: str
    replaced: str
    actual_part: str
    at: datetime
    new_evidence: Optional[str] = None
    fresh: bool = False


@dataclass(frozen=True)
class Use:
    """用途(灌装)记录。"""

    shell: str
    class_: str
    at: datetime


@dataclass(frozen=True)
class Purpose:
    """用途材料:允许的上次接触类别、所需部件的适用证明、是否接受 fresh 内胆。"""

    id: str
    permitted_previous_classes: tuple[str, ...]
    required_parts: tuple[str, ...]
    procedures: tuple[str, ...]
    allow_fresh_liner: bool = False


@dataclass(frozen=True)
class RecordBundle:
    """一批本地交换记录。"""

    shells: tuple[Shell, ...]
    labels: tuple[Label, ...]
    scans: tuple[Scan, ...]
    washes: tuple[Wash, ...]
    part_changes: tuple[PartChange, ...]
    uses: tuple[Use, ...]
    purposes: tuple[Purpose, ...]


def load_bundle(path: str | Path) -> RecordBundle:
    """从 JSON 文件载入交换记录(仅标准库)。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return RecordBundle(
        shells=tuple(
            Shell(id=s["id"], valve=s["valve"], liner=s["liner"])
            for s in raw.get("shells", [])
        ),
        labels=tuple(
            Label(
                issuer=l["issuer"],
                mark=l["mark"],
                shell=l["shell"],
                valid_from=parse_day(l["valid_from"]),
                valid_to=parse_day(l["valid_to"]),
            )
            for l in raw.get("labels", [])
        ),
        scans=tuple(
            Scan(
                handoff=s["handoff"],
                from_party=s["from"],
                to_party=s["to"],
                role=s["role"],
                at=parse_instant(s["at"]),
                shell=s.get("shell"),
                mark=s.get("mark"),
                paper=s.get("paper"),
                replaces_shell=s.get("replaces_shell"),
                reported_at=parse_instant(s["reported_at"]) if s.get("reported_at") else None,
            )
            for s in raw.get("scans", [])
        ),
        washes=tuple(
            Wash(
                issuer=w["issuer"],
                ref=w["ref"],
                at=parse_instant(w["at"]),
                coverage=tuple(w.get("coverage", ())),
                procedure=w.get("procedure"),
                revokes=w.get("revokes"),
            )
            for w in raw.get("wash", [])
        ),
        part_changes=tuple(
            PartChange(
                shell=p["shell"],
                replaced=p["replaced"],
                actual_part=p["actual_part"],
                at=parse_instant(p["at"]),
                new_evidence=p.get("new_evidence"),
                fresh=bool(p.get("fresh", False)),
            )
            for p in raw.get("parts", [])
        ),
        uses=tuple(
            Use(shell=u["shell"], class_=u["class"], at=parse_instant(u["at"]))
            for u in raw.get("uses", [])
        ),
        purposes=tuple(
            Purpose(
                id=p["id"],
                permitted_previous_classes=tuple(p.get("permitted_previous_classes", ())),
                required_parts=tuple(p.get("required_parts", ())),
                procedures=tuple(p.get("procedures", ())),
                allow_fresh_liner=bool(p.get("allow_fresh_liner", False)),
            )
            for p in raw.get("purposes", [])
        ),
    )
