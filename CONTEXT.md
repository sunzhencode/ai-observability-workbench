# AI Incident Operations Platform Domain Language

本上下文定义 F29 目标平台的统一语言。当前代码仍处于 F29-A 迁移前基线；术语以这里为目标，
实现差异看 `SYSTEM_SPEC.md`。平台把多个告警来源收敛到 Incident Operations Console，同时保留确定性的
来源边界、Occurrence 响应流程和只读监控边界。

## 来源与采集

**Event Source（事件来源）**：
向工作台提供 Alert 的稳定逻辑来源；来源身份不随地址、名称或凭证变化。
_Avoid_: Environment、环境、Alertmanager URL、数据源实例

**Alertmanager Source（Alertmanager 来源）**：
类型为 Alertmanager 的 Event Source，可由一个或多个 Endpoint 共同提供同一逻辑告警域。
_Avoid_: Alertmanager 实例、环境

**Endpoint（访问端点）**：
一个 Alertmanager Source 下可独立探测的实际访问地址；多个 Endpoint 可以属于同一 HA 来源。
_Avoid_: Event Source、独立环境

**历史与指标证据（Thanos）**：
一个 Event Source 上**可选的**只读指标地址，用于启动时有界回填，以及用户查看/调查时取得确定性
指标证据。它是来源配置里的一个字段，不是独立资源：一个来源最多一个，留空即没有回填或指标证据。
_Avoid_: Historical Data Source、历史数据源、绑定、范围模式、跨来源复用

**来源配置（Source Configuration）**：
一个 Event Source 在任意时刻**唯一的一份**当前配置：地址、备用地址、认证、历史数据与高级项。
保存即生效，没有第二个动作。
_Avoid_: 候选配置、快照、应用、revision、发布

**Adoption（收养）**：
升级时把旧连接配置一次性导入为受管数据源，并把历史数据的 legacy 来源标识重指向到它的过程。
收养结果一律未启用；用户可先测试再显式启用，但测试是诊断动作，不是启用前置条件。
_Avoid_: 迁移、导入向导、自动启用

**Source Scope（来源范围）**：
规则、通知策略或历史查询明确适用的一组 Event Source；“全部来源”也是一种显式范围。
_Avoid_: Environment matcher、隐式环境过滤

## 操作者与服务

**Interactive Operator（交互操作者）**：
通过浏览器/API 主动执行人工命令的人。当前不对应账号或身份；`InteractiveOperatorActor` 只证明请求经由
交互 API 的内部 witness，不证明“是谁”。
_Avoid_: User、Team、ADMIN、assignee、把 scheduler/worker/AI 伪装为人

**Service（服务）**：
最小服务目录对象，包含名称、slug、关键度、链接、状态与 revision。它用于影响归类、SLA 与检索，
不表示责任团队，也不改变 Alert 聚合身份。
_Avoid_: CMDB、部署、Kubernetes workload、group key

**Service Mapping Rule（服务映射规则）**：
按来源、alertname 和稳定标签确定性地把 Incident 投影到 Service 的首命中规则。
它支持 preview/publish，但不参与 Aggregation Rule，也不造成跨来源合并。
_Avoid_: AI 自动归属、第二套聚合规则、label enrichment 写回上游

## 告警与事件

**Alert**：
从一个 Event Source 归一化得到的单条上游告警最新状态，同一上游 fingerprint 在不同来源中是不同 Alert。
_Avoid_: Incident、Notification

**Incident（事件）**：
同一 Event Source 内，经一条 Aggregation Rule 确定性收敛的一条或多条 Alert 的生命周期对象。
_Avoid_: 跨来源事件、Alert、告警通知

**Occurrence（发生周期）**：
同一 Incident 分组从一次 live firing 开始的独立响应周期。上游真实 recovered 改变 Signal State，
但 Occurrence 仍可继续调查和处置；恢复后再次 live firing 会开启下一周期。
_Avoid_: Incident version、Alert occurrence

**Signal State（信号状态）**：
上游与来源可观测性的状态：`FIRING / RECOVERED / UNKNOWN / STALE`。它只能由确定性采集逻辑推进。
_Avoid_: Acknowledged、Resolved、处理完成

**Response State（响应状态）**：
人对本次 Occurrence 的处置进度：`UNACKNOWLEDGED / IN_PROGRESS / RESOLVED`，分别表示待确认、处理中、
已结束。实际调查、缓解和核验过程由 Evidence、Task、Note 与 Timeline 表达，不再让操作者维护中间状态。
对本平台协作通知的唯一映射是：UNACKNOWLEDGED 保持首次/升级/reminder/recovered；IN_PROGRESS 暂停
reminder 但保留升级/recovered；RESOLVED 终止当前 occurrence 的平台通知。它不改变权威
Alertmanager→飞书；再次 live firing 的新 Occurrence 建立新通知生命周期。
_Avoid_: firing、recovered、用一个 status 混合上游和人工状态

**Resolution Code（解决分类）**：
Response 进入 RESOLVED 时必填的人工结论：`FIXED / SELF_RECOVERED / FALSE_POSITIVE / DUPLICATE /
NO_ACTION`。
_Avoid_: Alertmanager resolved、HTTP 状态码、AI verdict

**Signal Severity（信号严重度）**：
由成员 Alert 的最高严重度确定性维护。人工不再维护第二套 P1–P4；Service tier 与 Ack SLA 表达响应目标，
Queue 使用 SLA 紧迫度与 Signal Severity 确定性排序。
_Avoid_: Response Priority、人工修改上游严重度

**Ack SLA（确认时限）**：
从平台第一次可信 `LIVE_POLL + COMPLETE` 创建 Occurrence 的 `detected_at` 起算，在创建时按 Service tier
冻结。上游 startsAt、backfill、partial、来源重启或 Signal recovery 不会启动、重置或暂停它。
_Avoid_: 把 Alertmanager startsAt 当平台发现时间、映射规则发布后改写历史 SLA

**Incident Task（处置任务）**：
属于一个 Occurrence 的人工或外部 Runbook 步骤，包含标题、说明、截止时间、状态和结果，不含 assignee。
首期不执行命令或基础设施写操作。

**Manual Note（人工备注）**：
Occurrence Timeline 中 append-only 的纯文本事实；更正通过新 Note 表达。HTTPS 链接只展示，后端不访问。
_Avoid_: 可覆写评论、让 AI 修改 Note、把链接当取数通道
_Avoid_: 自动修复、模型 tool call、Job

**Timeline（时间线）**：
一次 Occurrence 中 signal、response、assignment、task、notification、investigation 等事实的 append-only
事件序列。
_Avoid_: 可编辑备注、通用应用日志、配置审计

**发生记录**：
一次 Occurrence 结束（真实 recovered）时写下的不可变记录，承载那一次发生的开始/恢复时间、成员数、
成员最高严重度与结束时的人工处理结论。它是"过去发生过什么"的唯一载体；Incident 只保留当前状态。
_Avoid_: Incident、审计、告警历史（"审计"指人工处理状态的迁移流水，不是发生本身）

**Aggregation Rule（聚合规则）**：
定义 Alert 匹配条件、优先级和有序分组标签的独立规则，可应用于全部或部分 Event Source。
_Avoid_: Alert type config、环境规则

**Stale（已暂停更新）**：
来源停用或归档时，对最后已知 Incident 状态的新鲜度描述；重新启用后只有 COMPLETE live poll 能恢复
FRESH。PARTIAL/FAILED 不把未观察对象改为恢复，也不清除 STALE。
_Avoid_: Resolved、Recovered、Closed

## Watchdog

**Watchdog Monitor（Watchdog 监测）**：
一个 Alertmanager Source 上可独立启停的心跳监测配置，不创建 Incident。
_Avoid_: 全局 Watchdog 开关、普通告警规则

**Monitored Cluster（受监测集群）**：
在某个 Watchdog Monitor 下自动发现或人工声明、持续参与健康判定的 cluster 身份。
_Avoid_: 全局 cluster、30 天临时缓存

**Inventory State（清单状态）**：
受监测集群是否由系统发现、由用户明确声明或已被用户忽略的管理状态。
_Avoid_: Healthy、Missing、Unknown

**Health State（健康状态）**：
受监测集群基于来源可观测性和 Watchdog 新鲜度派生的 Healthy、Missing 或 Unknown。
_Avoid_: Inventory State、Incident state

## 通知与配置

**Notification Policy（通知策略）**：
按 Incident 稳定字段和 Source Scope 选择一个或多个 Notification Channel 的确定性首命中策略。
_Avoid_: Alertmanager route、逐 Alert 通知

**Notification Channel（通知通道）**：
工作台旁路发送 Incident 消息的稳定逻辑目标，**有一个 Provider（渠道类型）**。
一个通道代表一个固定去处（一个飞书群、一组收件人、一个端点）；换去处要新建通道，不是改配置。
_Avoid_: Alertmanager receiver、值班表、升级链

**Provider（渠道类型）**：
一个通道用什么协议把消息送出去，决定它的配置形状、渲染形态和"测试"的含义。
首批 `FEISHU_CUSTOM_BOT` / `SMTP` / `GENERIC_WEBHOOK`（F24 / ADR 0007）。
类型在通道创建后不可更改。
_Avoid_: 把 Provider 说成"渠道"本身——渠道是那个去处，Provider 是送达它的方式

**变更历史（Change History）**：
某个数据源上按时间倒序的配置变更与生命周期动作记录，只读、不可编辑，secret 永不回显。
它是"改了什么"的唯一去处——配置本身只有当前值。
_Avoid_: 未应用更改、Unapplied Change、Draft、DRAFT、草稿 revision、版本回滚

这里的 Avoid 只约束**数据源配置**。通知通道/策略为“测试后激活”和“影响确认”保留的 DRAFT/ACTIVE/
RETIRED 是通知安全状态，不是来源候选配置。

## 证据与调查（F27/F28 由 F29 吸收）

**主曲线（Primary Curve）**：
从**这条告警自己的官方表达式**派生出来的那条曲线，零配置、每条告警最多一条。
_Avoid_: 默认曲线、推荐指标——它不是被推荐的，它就是这条告警在看的那个东西

**辅助曲线（Auxiliary Curve）**：
由用户勾选的**指标模板**按标签匹配产生的旁证曲线，回答"周边还有什么不对劲"。
_Avoid_: 关联指标、相关性——系统不做任何相关性计算，匹配纯粹靠标签

**表达式来源分层（Expression Origin）**：
一条曲线的查询语句从哪来：`RULES_API`（Thanos rules 端点的结构化规则，权威）→
`GENERATOR_URL`（从告警链接里解析出来的，可能失效）→ `TEMPLATE`（用户配置的指标模板）。
**这个来源必须显示给用户**，它决定这条线值得多信。F29 目标只保留 `RULES_API`、
`GENERATOR_URL` 与 `TEMPLATE`；历史 `MODEL` 查询在 F29-A 后退出产品。
_Avoid_: 把几层混称为"告警表达式"——它们的可信度不同；也 Avoid 让 `MODEL` 与其余视觉同形

**模型撰写查询（已退出 F29 目标）**：
F27 曾允许模型读指标目录并撰写辅助 PromQL 字符串。用户在 F29 设计中确认这种自由查询语言形态没有
足够实际价值，最终由 ADR 0019 取代。主曲线、用户模板、Grafana 导入模板和手工查询继续存在；PLANNER
选择类型化 `MetricReadPlan` 不属于“模型撰写查询”，PromQL 只由代码组装。
_Avoid_: 把删除自由 PromQL 描述成删除指标证据或禁止模型选择下一条只读观察；继续把 Suggested Query
当作 F29 产品入口

**降级档位（Tier）**：
主曲线画的是什么：`THRESHOLD`（值 + 阈值线，**含比较方向**）/ `METRIC`（指标本身，
**只有唯一指标名时**）/ `EXPRESSION_RESULT`（表达式的返回结果）。
_Avoid_: `CONDITION`、"条件成立/不成立"、布尔曲线——PromQL 的普通比较**过滤 series 而不返回 0/1**，
最后一档返回空的含义是"条件在这个窗口内从未成立"；也 Avoid "降级失败"——降级是正常路径

**指标模板（Metric Query Template）**：
用户可勾选、可新增的一条参数化查询，声明它需要哪些标签。**模板的查询语句由作者写全**，
系统不再为它自动包 `rate()`。
_Avoid_: 面板、Dashboard、Panel——它不是可视化配置，是一条查询

**服务商（Model Vendor）**：
提供受支持模型接口的那一方（OpenAI / DeepSeek / 百炼 / 智谱 / Kimi / 自定义）。
选它预填地址与 provider profile；协议、thinking/reasoning、structured-output 策略和
目录能力按 `provider + model + protocol` 版本固定，不从 URL 猜。它不改变权限：
kind 决定可以去哪里，provider profile 只决定怎么调用。
_Avoid_: 厂家、供应商、平台——「厂家」是实体制造业的词，这里既没有工厂也没有货；
也 Avoid 把它说成一种安全 kind，kind 决定能去哪，服务商协议只决定怎样调用该目标

**模型通道（Model Channel）**：
工作台调用大模型的稳定出站目标，**有 kind、创建后不可改、由操作者显式启用**，
与通知通道并列但**不是同一个东西**，存在自己的表里、走自己的地址护栏。连接测试只记录最近一次
诊断结果，不决定能否启用；启用表示允许平台在显式调查时尝试向该目标发送受控数据。
_Avoid_: 把它叫成"通知通道的一种"、AI 配置、API 配置

**模型连接测试（Model Connection Test）**：
对已保存模型通道执行的可选 synthetic 调查。它与正式调查调用同一
`IncidentInvestigationHarness`，只把输入换成固定 snapshot 和 in-memory 只读工具，分开返回
目录可见性、协议、Tool 和 Output 契约结果。
它不发送真实事件数据，也不启用、停用或回滚配置。
_Avoid_: 激活门、发布步骤、把测试成功等同于运行期永久可用

**调查（Investigation）**：
一次用户在 Incident Occurrence 上显式触发的有界只读模型调查。它冻结
`EvidenceSnapshotV2`，由单个 `Incident Investigator` 通过封闭只读工具补充事实，
交付 `InvestigationReportV2`。它不是后台自动 Agent，也不得执行任何写动作。
_Avoid_: 分析、诊断、RCA——这些词暗示了确定性，而调查的结论可能被推翻

**调查范围解析（Investigation Scope Resolution）**：
调查开始时由代码冻结的 source、occurrence、全部 member alert refs、Service mapping 与 catalog revision。
Service 未映射可以显式退化到 source + member-alert scope，但模型永远不能删除或扩大这些边界。
_Avoid_: 让模型猜服务、传任意 URL、把 scope 缺失静默当成全局查询

**调查 Playbook（Investigation Playbook）**：
一份版本化、只读、由代码选择的领域调查手册，声明候选指标族、跟进规则、必需反证、停止条件与降级。
它是可测试的产品资产，不是模型可编辑的 prompt 文件或运行时自由笔记。
_Avoid_: Skill Agent、虚拟文件系统、让模型 read/write 任意文件、未经 revision 冻结的临时经验

**证据快照 / 调查活动 / 调查报告（Evidence Snapshot / Investigation Activity / Investigation Report）**：
新调查的三种持久产物。快照是模型调用前的确定性事实；活动是模型选择了什么只读
工具以及代码实际取得/查空/拒绝了什么；报告是通过本地契约校验的最终假设与建议。
旧 P0/P1/P2、PLANNER 和 ANALYST 仅是 legacy 历史码，不得出现在新调查主界面或新 provider 适配中。
_Avoid_: 框架 step ID、虚假线性进度、把调用轨迹当结论

**Incident Investigator / Incident Investigation Harness**：
Investigator 是唯一模型 Agent，拥有三个类型化只读指标工具和一个强类型报告输出。
Harness 是仓库的 deep module，隔离 Pydantic AI/Harness provider、loop、step journal、预算和 guardrail；
连接测试与正式调查共用它。
_Avoid_: LangChain chain、LangGraph 节点、子 Agent、handoff、MCP、shell/file/browser 工具泄漏到业务

**Metric Read Plan（指标读计划）**：
PLANNER 可表达的封闭查询意图：冻结 alert_ref、已知 metric、合法 label filters、固定窗口、有限 aggregation 与 group_by。
`MetricReadPlanCompiler` 校验 scope/schema/budget 并由代码组装 PromQL，模型从不接触查询字符串。
_Avoid_: PromQL、任意查询 DSL、传任意 URL 的工具参数

**假设与结局（Hypothesis / Verdict）**：
调查输出的最小单位。每个假设必须有一个结局：`SUPPORTED` / `SYMPTOM` / `DISPROVEN` / `BLOCKED`。
**没有结局的假设不进结论。**
_Avoid_: **`ROOT_CAUSE`、"根因"**——指标相关性、告警文本与处置历史证明不了因果，
`SUPPORTED` 在界面上是"证据支持"；也 Avoid 结论、答案——在有结局之前它只是假设

**证据归属（Evidence Attribution）**：
每条假设与建议必须指回**原子事实**，界面上可点回去。**模型不得改写证据里的数值，只能引用。**
身份是两级：**`curve_id`**（一张曲线，用于 UI 分组）与 **`fact_id`**（具体 series 上的某个统计
或某个时间区段）。**模型只能引用 `fact_id`。**
_Avoid_: 引用、参考——归属是强制的结构，不是可选的礼貌；
也 Avoid 只挂整条曲线的宽泛引用——"内存曲线支持这个假设"没有说出任何可核对的东西

**降级提示（Warning）与失败（Failure）**：
两个不同的出口。曲线画出来了但依据是推断的（回退到链接、类型按名称猜、标签集合靠猜）是
**warning**；计划中的曲线没产生才是 **failure**。
_Avoid_: 把成功的曲线配一条"上游不可达"——那是在误导读者

**初始调查快照（Initial Investigation Snapshot）**：
一次调查开始时记录的不可变上下文，显式分为 occurrence、alerts、metrics、grafana、history、context
六个区块。它是轨迹起点，不是假设系统能提前猜中全部证据。
_Avoid_: 重查上游"还原"当时的证据——那不是当时的证据

**调查轨迹（Investigation Trajectory）**：
一次调查中 append-only 的类型化 activity：模型想看什么、代码实际取得/拒绝/查空了什么、
预算如何消耗以及终止原因。Pydantic AI Harness step journal 是 runtime 恢复/审计细节，
不取代产品 activity/result 表。历史查看冻结快照与持久活动，永不重新取数。
_Avoid_: 聊天记录、通用 EvidenceRecord、`kind + arbitrary payload` 万能事实表

**调查降级（Investigation Degradation）**：
某个依赖不可用时的类型化、可见事实，包含 dependency、safe code、影响、仍保留的证据和下一步。
局部降级不等于整次调查失败；模型不可用时可以诚实停在 evidence-only。
_Avoid_: 静默跳过、统一“加载失败”、把缺失依赖伪装成无异常

**InteractiveOperatorActor / SystemActor**：
前者只由交互 API adapter 内部 `OperatorWitness` 构造，可推进 Response，但不携带虚假身份；后者代表
poll/scheduler/worker。人工 Response command 只接受前者，AI 不能获得 witness 或命令 port。
_Avoid_: 请求 body 自报 actor、用 `user_id="ai"` 伪装、只靠 Python annotation 保证 capability

**Prompt Profile（提示配置）**：
可由操作者选择和版本化发布的调查指导层。可编辑组织背景、关注顺序、术语和风格；不可编辑字段可见性、
工具、PromQL compiler、证据 allow-set、输出 schema、预算或无写安全内核。它与只读 Playbook 分开冻结。
_Avoid_: 事故内自由 system prompt、用 prompt 代替代码护栏、让 Profile 修改 Playbook/tool schema

**相似历史（Similar Incidents）**：
按 Service、alertname、Aggregation Rule 与关键 group labels 确定性评分得到的过去 Occurrence，
每条必须显示匹配原因。
_Avoid_: 向量检索、AI 猜相似、跨来源自动合并

**Job（持久任务）**：
由 scheduler/API 入队、带状态、租约、幂等和崩溃恢复语义的后台工作单元。它负责采集、调查、投递等
执行，不等同于人类处置任务。
_Avoid_: Incident Task、APScheduler 回调本身、内存 future

## 专业操作语言

核心操作流程统一为 `Observe → Qualify → Act → Verify → Audit`。按钮使用明确领域动词：确认本次
Incident、指派、进入缓解、开始观察、解决、抑制本平台通知、重试投递、测试连接、开始证据调查。
_Avoid_: “处理”“操作”“AI 看看”“确定”“操作成功”“加载失败”“根因已找到”；这些词没有说明对象、
后果、事实是否改变或下一步。

## Grafana 查询资产导入（F28）

**参照线 / Baseline（已退出产品）**：
F28 曾实现用户在指标模板上填写常量与好坏方向；真实试用后确认没有实际价值，已由 ADR 0015 撤销。
历史数据库表只作 migration 兼容，当前领域/API/UI 不再使用这个术语。
_Avoid_: 把它当作现行能力，或用 Grafana threshold 颜色、指标名、模型推断一个替代品

**Dashboard 导入（Dashboard Import）**：
从来源上配置的 Grafana **只读**拉取一个 dashboard 的定义，解析成**导入候选**，
由用户逐条确认后落为指标模板。**一次性动作，不是持续同步**；导完不依赖 Grafana。
_Avoid_: 同步、订阅——自动同步意味着上游一改、本地的判断标准就悄悄变了；
也 Avoid 把它说成"取数"——指标一律走 Thanos 直连，Grafana 只提供定义

**导入候选（Import Candidate）**：
一条 panel target 解析出来的待确认模板，带三分类结果：**直接可用 / 需你决定 / 用不了**。
"需你决定"指含 Grafana 宏或模板变量，必须由用户选择绑标签、填死或跳过。
_Avoid_: 面板、Panel——一个 panel 可能产出多条候选；也 Avoid 把未确认的候选叫模板
