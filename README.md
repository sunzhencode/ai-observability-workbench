# AI Incident Operations Platform

本仓库是一个本地运行、面向单操作者的 Incident Operations 工作台。它从多个 Alertmanager Event Source
只读采集告警，在来源内按确定性规则形成 Incident；再把 Service、响应状态、任务、Note、Timeline、指标证据、
历史、通知和受控 AI 调查放到同一个可审计工作流中。

默认产品已经完成 F29 一次性切换：`./start.sh` 启动模块化单体、统一 `/api/v1` 和
`operations-console/`。唯一启动入口是 `./start.sh`。
旧 Workbench 数据库已归档，最终数据库由 Alembic 新基线创建；没有双写、在线迁移或长期旧 API。

## 产品边界

- 单操作者、单进程 Uvicorn、SQLite、默认 localhost；可显式监听 `0.0.0.0`，但当前没有账号、RBAC、TLS
  或代理认证，网络可达者拥有完整权限。
- Alertmanager、Thanos 和 Grafana 只读。平台不创建或修改 silence、route、receiver、dashboard、
  datasource、告警规则或基础设施。
- Incident 聚合只发生在同一个 EventSource 内；同一来源可有多个 HA Endpoint。
- 上游 recovered 不等于人工 resolved。响应只保留“待确认 → 处理中 → 已结束”；任务、Note 和 Timeline
  记录实际过程。
- 通知是用户显式配置的旁路协作通道；既有 `Alertmanager → 通知` 仍是权威链路。
- AI 只读取冻结证据并输出有证据归属的假设、反证、缺失证据与人工下一步；不改状态、不执行命令，
  不声称自动找到根因。
- OpenAI、DeepSeek、Moonshot/Kimi、GLM 使用明确 Provider Profile；CUSTOM 只承诺 best-effort
  OpenAI-compatible。模型凭证加密且永不回显。

设计、决策与验收记录保存在本地 `docs/`，不随仓库提交。此处说明可运行的公开版本；具体行为可从代码与测试核对。

## 本地启动

环境要求：仓库根 `.venv`（Python 3.14）、Node 25 / npm 11；默认本地监控模式还需要 Docker。

首次建立基础环境（在仓库根目录执行）：

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -e 'backend[dev]'
npm --prefix operations-console ci
```

```bash
./start.sh              # 本地 Prometheus + Alertmanager；静态同源 UI :8100
./start.sh --mock       # Python fixture；Fake 模型和 Fake 通知
./start.sh --configured # 使用 backend/data/workbench.db 中手工保存的配置
./start.sh --dev        # 后端 :8100 + Vite :5174
```

默认打开 <http://127.0.0.1:8100>。`--dev` 才打开 <http://127.0.0.1:5174>。

数据库选择：

- 默认 local：`backend/data/incident-operations-local.db`；
- `--mock`：`backend/data/incident-operations-mock.db`；
- `--configured`：`backend/data/workbench.db`；
- 显式 `INCIDENT_OPERATIONS_DATABASE_PATH` 覆盖模式默认路径，适合隔离验收。

默认 local 模式启动仓库随附的 loopback Prometheus `:9090` 和 Alertmanager `:9093`。Prometheus 求值
示例规则，Alertmanager 只有无外发 integration 的 `local-null` receiver；退出启动器时只清理本次创建的
专用容器和网络。这是真实本地进程/HTTP/规则求值证据，不是外部监控源或生产 paging 证明。

`--mock` 在 `:9999` 启动 Python fixture，覆盖 source-a/source-b、HA、PARTIAL/FAILED、recovery、Watchdog
和 Thanos 故障；它是确定性故障证据，不冒充真实监控进程。

配置优先级：进程中的 `INCIDENT_OPERATIONS_*` → 可选 `backend/.env` → 模式默认值。
`.env` 只读取数据库路径、host、后端端口、前端端口四项，不执行 shell；相对数据库路径以仓库根为基准。
旧 `ALERT_WORKBENCH_*` 引导键会明确报迁移错误，应按新的 `.env.example` 修改。
若只想改端口，请省略数据库路径，保留 local/mock/configured 三库隔离。主密钥和 trusted hosts 只支持进程级覆盖。

local/mock 每次启动都会重设同名演示来源、重新启用，并更新其监控连接；mock 还准备演示 Service/Mapping 和 Fake 模型。
数据库和主密钥退出后保留，不是一次性临时数据。显式指定已有库时，同样会执行这些 seed 写入。
默认 local 强制 Fake 通知，模型仍是操作者显式触发的真实模型；mock 两者均 Fake；configured 使用真实保存的通道，
未完成的通知投递可能随后台运行恢复。启动摘要会再次展示这些差异。

启动器确认后端和可选 Vite 就绪后才打印 Ready/访问地址；seed 或子进程失败会非零退出并清理本次资源。
Ctrl-C/TERM 会停止本次后端、fixture、Vite 进程组。新建监控资源带本次启动标识，失败也会清理；已运行的完整栈复用后保留。
发现部分或已停止的同名监控栈时明确报错，要求先核对容器状态，不自动删除已有资源。

### 可信网络访问

```bash
INCIDENT_OPERATIONS_HOST=0.0.0.0 \
INCIDENT_OPERATIONS_TRUSTED_HOSTS=ops-host.example \
./start.sh
```

浏览器访问 `http://ops-host.example:8100`；`0.0.0.0` 只是监听地址。允许列表必须是明确的主机名/IP，
不能用通配符。Host/Origin/CSRF 护栏不是身份认证；不可信网络上不要暴露该端口。

## 首次配置

local/mock 会自动登记演示来源；`--configured` 从空库启动时，六个一级入口仍可正常打开：

- **Incidents**：Queue、详情、响应、任务、Note、Timeline 和 AI 调查；
- **告警**：原始告警与确定性聚合结果；
- **Automation**：Service、Service Mapping、聚合规则、维护窗口和指标模板；
- **通知**：通道、策略与 Delivery；
- **Analytics**：代码计算的运营指标；
- **系统设置**：来源、Grafana 与模型通道。

业务配置只存 SQLite。真实 secret 由操作者在 Web 中输入；页面/API 永不回显。模型连接测试只返回诊断结果，
成功或失败都不改变启用状态。读取模型目录、远程测试和正式调查都必须由操作者显式触发，可能产生费用。
Agent 不代填凭证，也不代点 Test/Activate/Send。

主密钥由后端首次安全创建在 `backend/data/master.key`，权限 `0600`，已存在绝不覆盖。数据库与主密钥应
一起备份；丢失主密钥后，已保存凭证会 fail closed。

## AI 调查

点击开始调查后，代码先冻结 `EvidenceSnapshotV2`，持久 Job Runner 管理 lease、幂等与取消，单一
Incident Investigator 只能调用三个仓库自有的封闭只读指标工具。工具接受 `MetricReadPlan`，PromQL 由
代码校验并组装；模型不接触任意 URL、shell、文件、浏览器、MCP 或写能力。

证据分层：L1 摘要和有界 L2 采样可进入模型；L3 全量序列只落库供 UI 绘图。输出必须通过
`InvestigationReportV2` 校验，evidence ID 必须命中本次快照。Provider/工具/Guardrail/输出修复均不隐式
重试可能计费的请求；429、超时或不确定响应会保留证据并明确降级，等待操作者再次显式调查。

## 验证

```bash
(cd backend && ../.venv/bin/python -m pytest -q)
(cd backend && ../.venv/bin/python -m mypy)
npm --prefix operations-console run test
npm --prefix operations-console run build
npm --prefix operations-console run codegen:check
git diff --check
```

浏览器验收：

```bash
# 终端 1
./start.sh --mock

# 终端 2；默认验收 http://127.0.0.1:8100
npm --prefix operations-console run acceptance
```

脚本覆盖六个一级页面、800px 无横向溢出、console 0 warning/error、password 0 回显、深链/刷新/后退，
并显式创建一次 Fake V2 调查。它不会发送真实通知或访问真实模型。

本地监控与故障矩阵：

```bash
./start.sh --validate-monitoring
.venv/bin/python scripts/mock_fault_matrix.py
```

## 数据恢复

F29 默认切换后的恢复是停机操作：先 dry-run，再校验 manifest；目标文件存在时拒绝覆盖，
`master.key` 永不进入 archive 文件集。详细演练记录保存在本地 `docs/`。

## 代码入口

- [`CONTEXT.md`](CONTEXT.md) 解释 EventSource、Incident、Occurrence 等核心术语。
- `backend/app/` 包含领域、应用、适配器与 `/api/v1` 接口；`backend/tests/` 为离线回归用例。
- `operations-console/` 是当前前端；`frontend/` 是保留的旧版源码，默认启动器不挂载。
- `local-monitoring/` 与 `scripts/` 提供本地监控和故障矩阵验证。

`docs/`、`output/` 与 `.superpowers/` 仅留在开发机，不在仓库提交；详细设计和验收记录不随公开版本提供。

F30 已完成事件内指标、调查历史与证据定位、主操作入口和筛选/编辑修复，详见现行 Product Spec 的 CAP-14.1。
历史没有调查快照时明确显示缺失，不会用当前告警补造历史；当前没有活动 SDD。

F31 已完成单一启动入口收敛，启动行为见上方「本地启动」。
