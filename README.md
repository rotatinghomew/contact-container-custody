# 食品周转容器的跨厂履历与停用处置

《容器交换记录》说明对接方已有的资料字段及业务口径。`data/source_records.json` 保存可供本地使用的合成记录,无真实机构数据和凭据。
使用 Python 3.11,资料读取、联调及测试只用标准库。用 `python3 -m json.tool data/source_records.json` 查阅本地交换记录。

在项目根目录运行 `python3 -m unittest discover -s tests -v` 检查合成资料的关联关系及已有的联调行为。

## 交接服务 `container_exchange`

在合成记录之上提供跨厂交接服务,全部为标准库实现:

- `records` —— 交换记录模型与 JSON 载入;`at` 为作业发生时间,`reported_at` 为到达平台时间。
- `identity` —— 身份解析:壳号为主,标签按有效期窗口(左闭右开)对应壳身,纸面点收以凭证号对应壳号与接收单位;重复扫描不产生第二只容器,身份材料冲突时拒绝入帐。
- `ledger` —— 保管台账:同一 handoff 内交出/接收双方录入相互对照;补正扫描(replaces_shell)取代旧录入且原记录保留可查;只有双方吻合才移动确认保管,单边录入仅构成据报保管。
- `eligibility` —— 资格判定:按实际接触部件(壳体/卸料阀/内胆)、上次用途与清洗覆盖范围评估;清洗证明须晚于上次用途且晚于装件时刻,fresh 内胆凭独立未使用出库凭据替代。
- `service` —— 服务门面:
  - 出箱申请 `open_request` / `qualify` / `claim` / `withdraw` / `release`,可并发、可撤回;资格结果与台账版本绑定,交接或接触记录更新后不得沿用旧结果发出交接;
  - 清洗部分撤回自动圈定:未交接的退出可领清单,已领未灌装的通知现保管人停用,已灌装的开出按实物识别的隔离交接单;范围外及已换件且有独立证据的部位继续可用;
  - 现场接口 `confirm_physical_claim`(实物领用)与 `confirm_isolation_return`(隔离交回);
  - 库管回查 `why_reusable` / `current_holder` / `processing_status`,补传扫描用 `attribute_scan` 核清发生在哪次保管期间。
- `builder` —— 按到达顺序(reported_at)接入一批记录,可截断到某一时刻复现当时视角。

运行 `python3 -m container_exchange.demo` 可看到交接对照、撤回处置、证据链与保管期间归属的完整走查。
