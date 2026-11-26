"""交接服务联调测试:以 data/source_records.json 及小型合成夹具验证业务口径。"""
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from container_exchange import (
    ExchangeService,
    IdentityConflict,
    IdentityError,
    PartChange,
    Purpose,
    RequestStateError,
    Scan,
    Shell,
    StaleQualification,
    Use,
    Wash,
    build_service,
    load_bundle,
)

DATA = Path(__file__).resolve().parents[1] / "data" / "source_records.json"
TZ8 = timezone(timedelta(hours=8))


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 26, hour, minute, tzinfo=TZ8)


class DatasetCase(unittest.TestCase):
    """基于合成交换记录的端到端口径。"""

    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(DATA)

    def build(self, until=None) -> ExchangeService:
        return build_service(self.bundle, until=until)

    def test_identity_windows_and_conflicts(self):
        svc = self.build()
        # 旧标复用按窗口(左闭右开)区分壳身
        self.assertEqual(svc.identity.shells_for_mark("196", ts("2026-11-19T12:00:00+08:00")), {"V11"})
        self.assertEqual(svc.identity.shells_for_mark("196", ts("2026-11-20T00:00:00+08:00")), {"V12"})
        self.assertEqual(svc.identity.shells_for_mark("196", ts("2026-12-01T00:00:00+08:00")), set())
        self.assertEqual(svc.identity.shells_for_mark("ED201", at(10, 1)), {"V11"})
        # 纸面点收 K081 对应 V12
        self.assertEqual(svc.identity.paper_shell("K081"), "V12")
        # 壳号与当期标签冲突:拒绝入帐,不产生第二只容器
        with self.assertRaises(IdentityConflict):
            svc.ingest_scan(Scan("X1", "a", "b", "from", at(12, 30), shell="V11", mark="196"))
        # 纸面凭证与壳号冲突
        with self.assertRaises(IdentityConflict):
            svc.ingest_scan(Scan("X2", "a", "b", "from", at(12, 30), shell="V13", paper="K081"))
        # 过期标记无法解析
        with self.assertRaises(IdentityError):
            svc.ingest_scan(Scan("X3", "a", "b", "from", ts("2026-12-05T08:00:00+08:00"), mark="196"))
        self.assertEqual(svc.known_shells(), ("V11", "V12", "V13"))

    def test_duplicate_scan_keeps_one_container(self):
        svc = self.build()
        before = svc.version
        dup = Scan("D31", "wash-a", "plant-b", "from", at(10, 1), shell="V11", mark="ED201")
        result = svc.ingest_scan(dup)
        self.assertEqual(result["shell"], "V11")
        self.assertEqual(svc.known_shells(), ("V11", "V12", "V13"))
        self.assertTrue(svc.ledger.matched("D31", "V11"))
        self.assertEqual(svc.current_holder("V11"), "plant-b")
        self.assertEqual(svc.version, before + 1)

    def test_dispute_window_before_correction(self):
        svc = self.build(until=at(11, 0))
        self.assertEqual(svc.handoff_status("D31")["state"], "matched")
        d32 = svc.handoff_status("D32")
        self.assertEqual(d32["state"], "open")
        self.assertEqual(d32["shells"]["V12"], {"from": False, "to": True, "matched": False})
        self.assertEqual(d32["shells"]["V13"], {"from": True, "to": False, "matched": False})
        # 未吻合的箱被圈定;同车已相互确认的箱不受影响
        self.assertEqual(svc.ledger.unmatched_handoff("V12"), "D32")
        self.assertIsNone(svc.ledger.unmatched_handoff("V11"))
        self.assertIsNone(svc.ledger.unmatched_handoff("V13"))  # 最近一次 D35 已吻合
        self.assertEqual(svc.transferable("wash-a"), [])
        self.assertEqual(svc.transferable("plant-b"), ["V11"])
        # 单边录入只构成据报保管
        self.assertEqual(svc.current_holder("V12"), "wash-a")
        self.assertEqual(svc.reported_holder("V12"), "plant-b")

    def test_correction_scopes_impact_and_keeps_history(self):
        svc = self.build()
        d32 = svc.handoff_status("D32")
        self.assertEqual(d32["state"], "matched")
        self.assertEqual(list(d32["shells"]), ["V12"])
        self.assertEqual(len(d32["superseded"]), 1)
        self.assertEqual(d32["superseded"][0]["shell"], "V13")
        # 补正后保管按原出箱时刻落定
        self.assertEqual(svc.current_holder("V12"), "plant-b")
        timeline = svc.custody_timeline("V12")
        self.assertEqual([(p.holder, p.end) for p in timeline],
                         [("wash-a", at(10, 10)), ("plant-b", None)])
        # 补传扫描可核清发生在哪次保管期间
        correction = next(s for s in self.bundle.scans if s.replaces_shell)
        attribution = svc.attribute_scan(correction)
        self.assertEqual(attribution["shell"], "V12")
        self.assertEqual(attribution["holder"], "wash-a")
        self.assertEqual(attribution["period_end"], at(10, 10))
        self.assertEqual(svc.custody_period_at("V12", at(10, 30)).holder, "plant-b")
        # 期间的保管与领用事实仍可查
        self.assertTrue(any(not e.effective for e in svc.ledger.all_entries()))
        self.assertTrue(svc.processing_status("V12")["history"])

    def test_revocation_scopes_to_actual_parts(self):
        svc = self.build()
        by_kind = {n.kind: n for n in svc.notices}
        # 已领未灌装:通知现保管人停止使用
        self.assertEqual(by_kind["stop_use"].shell, "V12")
        self.assertEqual(by_kind["stop_use"].party, "plant-b")
        # 已灌装:按实物开出隔离交接单
        self.assertEqual(by_kind["isolation_order"].shell, "V13")
        self.assertEqual(by_kind["isolation_order"].party, "plant-c")
        # 已更换且有独立证据的接触部位不受影响:V11 无任何通知
        self.assertFalse(any(n.shell == "V11" for n in svc.notices))
        self.assertEqual(svc.holds["V12"]["kind"], "stop_use")
        self.assertEqual(svc.holds["V13"]["kind"], "isolation")
        self.assertEqual(svc.claimable("binder", at(12, 30)), [])

    def test_reuse_evidence_chain(self):
        svc = self.build()
        report = svc.why_reusable("V11", "binder", at(10, 30))
        self.assertTrue(report.eligible)
        evidence = {p.role: p for p in report.parts}
        self.assertEqual(evidence["shell"].kind, "wash")
        self.assertEqual(evidence["shell"].ref, "W80")
        self.assertEqual(evidence["valve"].kind, "wash")
        # 内胆已换为 L21,凭独立未使用出库凭据
        self.assertEqual(evidence["liner"].kind, "fresh")
        self.assertEqual(evidence["liner"].ref, "R7")
        # 灌装后须重新清洗
        self.assertFalse(svc.why_reusable("V11", "binder", at(12, 30)).eligible)
        # P12 被撤回后 V12 的阀缺少适用证明
        v12 = svc.why_reusable("V12", "binder", at(12, 30))
        self.assertFalse(v12.eligible)
        self.assertIn("P12", {p.part for p in v12.parts if p.kind == "missing"})

    def test_isolation_return_and_rewash_lifts_hold(self):
        svc = self.build()
        order = next(o for o in svc.isolation_orders.values() if o.shell == "V13")
        svc.confirm_isolation_return(order.id, by="wash-a", at=at(15, 0))
        self.assertEqual(order.status, "returned")
        self.assertEqual(svc.current_holder("V13"), "wash-a")
        self.assertIn("V13", svc.holds)  # 未重新清洗前限制仍在
        svc.ingest_wash(Wash("wash-a", "W90", at(16, 0), ("V13", "P13", "L13"), "plant-food"))
        self.assertNotIn("V13", svc.holds)
        self.assertTrue(svc.why_reusable("V13", "binder", at(16, 30)).eligible)
        self.assertIn("V12", svc.holds)  # 未覆盖 P12 的清洗不影响 V12 的停用

    def test_stale_qualification_cannot_release(self):
        svc = self.build(until=at(10, 30))
        svc.open_request("RQ1", "plant-d", "binder")
        qualification = svc.qualify("RQ1", at(10, 30))
        self.assertEqual(set(qualification.candidates), {"V11", "V13"})
        svc.claim("RQ1", "V13")
        svc.ingest_use(Use("V13", "binder", at(10, 41)))  # 接触记录更新
        with self.assertRaises(StaleQualification):
            svc.release("RQ1", "V13", "D40", at(10, 50))
        refreshed = svc.qualify("RQ1", at(10, 50))
        self.assertNotIn("V13", refreshed.candidates)  # 已灌装,退出候选


class RequestFlowCase(unittest.TestCase):
    """小型合成夹具:并发申请、撤回、实物领用与换件失效。"""

    def make_service(self) -> ExchangeService:
        svc = ExchangeService()
        svc.register_shell(Shell("W1", "P1", "L1"))
        svc.register_shell(Shell("W2", "P2", "L2"))
        svc.add_purpose(Purpose("jam", ("fruit",), ("shell", "valve", "liner"), ("hot-wash",)))
        svc.ingest_wash(Wash("depot", "W100", at(8), ("W1", "P1", "L1", "W2", "P2", "L2"), "hot-wash"))
        for shell in ("W1", "W2"):
            svc.ingest_scan(Scan("H0", "supplier", "depot", "from", at(9), shell=shell))
            svc.ingest_scan(Scan("H0", "supplier", "depot", "to", at(9, 5), shell=shell))
        return svc

    def test_concurrent_requests_claim_and_withdraw(self):
        svc = self.make_service()
        svc.open_request("RQ1", "plant-x", "jam")
        svc.open_request("RQ2", "plant-y", "jam")
        q1 = svc.qualify("RQ1", at(10))
        q2 = svc.qualify("RQ2", at(10))
        self.assertEqual(set(q1.candidates), {"W1", "W2"})
        self.assertEqual(set(q2.candidates), {"W1", "W2"})
        svc.claim("RQ1", "W1")
        with self.assertRaises(RequestStateError):
            svc.claim("RQ2", "W1")  # 并发申请不能重复预留同一只
        svc.claim("RQ2", "W2")
        svc.withdraw("RQ1")  # 撤回后预留释放
        self.assertEqual(svc.request_state("RQ1")["state"], "withdrawn")
        svc.open_request("RQ3", "plant-z", "jam")
        svc.qualify("RQ3", at(10, 20))
        svc.claim("RQ3", "W1")

    def test_release_and_physical_claim_moves_custody(self):
        svc = self.make_service()
        svc.open_request("RQ1", "plant-x", "jam")
        svc.qualify("RQ1", at(10))
        svc.claim("RQ1", "W1")
        svc.release("RQ1", "W1", "H1", at(10, 30))
        self.assertEqual(svc.current_holder("W1"), "depot")  # 未确认前保管不动
        with self.assertRaises(Exception):
            svc.confirm_physical_claim("H1", "W1", "plant-y", at(11))  # 非交接对象
        svc.confirm_physical_claim("H1", "W1", "plant-x", at(11))
        self.assertEqual(svc.current_holder("W1"), "plant-x")
        self.assertEqual(svc.request_state("RQ1")["state"], "fulfilled")

    def test_part_change_invalidates_qualification(self):
        svc = self.make_service()
        svc.open_request("RQ1", "plant-x", "jam")
        svc.qualify("RQ1", at(10))
        svc.claim("RQ1", "W2")
        svc.ingest_part_change(PartChange("W2", "L2", "L9", at(10, 45)))
        with self.assertRaises(StaleQualification):
            svc.release("RQ1", "W2", "H2", at(11))
        refreshed = svc.qualify("RQ1", at(11))
        self.assertNotIn("W2", refreshed.candidates)  # 新内胆无清洗亦无 fresh 凭据

    def test_unilateral_record_blocks_transfer(self):
        svc = self.make_service()
        svc.ingest_scan(Scan("H9", "depot", "plant-z", "from", at(10), shell="W1"))
        self.assertEqual(svc.ledger.unmatched_handoff("W1"), "H9")
        self.assertEqual(svc.transferable("depot"), ["W2"])  # 同批已吻合的不受牵连
        svc.open_request("RQ1", "plant-x", "jam")
        qualification = svc.qualify("RQ1", at(10, 30))
        self.assertNotIn("W1", qualification.candidates)
        # 接收方确认后解除
        svc.ingest_scan(Scan("H9", "depot", "plant-z", "to", at(10, 40), shell="W1"))
        self.assertIsNone(svc.ledger.unmatched_handoff("W1"))
        self.assertEqual(svc.current_holder("W1"), "plant-z")


if __name__ == "__main__":
    unittest.main()
