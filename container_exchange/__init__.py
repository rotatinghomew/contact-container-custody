"""食品周转容器跨厂交接服务(仅标准库)。

- records:     交换记录模型与 JSON 载入
- identity:    壳号/标签/纸面点收的身份解析,重复扫描不产生第二只容器
- ledger:      双方对照、补正、保管期间与未吻合判定
- eligibility: 接触部件时间线、清洗证据与用途资格
- service:     交接服务门面(申请、发放、撤回处置、现场接口、回查)
- builder:     按到达顺序接入一批记录
"""
from .builder import build_service, ingest_bundle
from .eligibility import EligibilityReport, PartEvidence
from .identity import IdentityConflict, IdentityError
from .ledger import CustodyPeriod
from .records import (
    Label,
    PartChange,
    Purpose,
    RecordBundle,
    Scan,
    Shell,
    Use,
    Wash,
    load_bundle,
)
from .service import (
    BoxRequest,
    ExchangeService,
    IsolationOrder,
    Notice,
    Qualification,
    RequestStateError,
    ServiceError,
    StaleQualification,
    TransferBlocked,
)

__all__ = [
    "BoxRequest",
    "CustodyPeriod",
    "EligibilityReport",
    "ExchangeService",
    "IdentityConflict",
    "IdentityError",
    "IsolationOrder",
    "Label",
    "Notice",
    "PartChange",
    "PartEvidence",
    "Purpose",
    "Qualification",
    "RecordBundle",
    "RequestStateError",
    "Scan",
    "ServiceError",
    "Shell",
    "StaleQualification",
    "TransferBlocked",
    "Use",
    "Wash",
    "build_service",
    "ingest_bundle",
    "load_bundle",
]
