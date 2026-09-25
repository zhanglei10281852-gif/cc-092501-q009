# 地下水同位素与污染迁移计算服务

这是一个面向水文地质研究团队和环境监管人员的模块化后端，集中管理地下水井、同位素观测、补给端元、混合源反演、污染物迁移、计算任务、参数版本、结果置信区间、用户权限、会话和审计。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 井点与样本：登记井点坐标、含水层、采样批次和实验室测量结果。
- 同位素计算：处理稳定同位素、溶质浓度、检测限和质量守恒约束，反演多个补给端元比例。
- 污染迁移：计算一维平流、弥散和一阶衰减，提供到达时间和浓度曲线。
- 结果比较：在同一数据集上比较两个模型版本的反演或迁移结果，统一端元与时间轴后给出绝对差、相对差、区间重叠和关键阈值变化；数据或参数不兼容的字段会被明确列出。差异报告保存输入指纹与生成时间，相同请求自动复用已有报告。
- 任务与审计：保存参数版本、计算输入摘要、置信区间、失败重试和结果差异。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

配置项均以 `TOWNSHIP_` 开头。可以复制 `.env.example` 后按需设置，默认数据库位于 `./data/township.db`。

## 初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

首次部署可创建唯一的初始管理员：

```bash
curl -sS -X POST http://127.0.0.1:8432/api/auth/bootstrap   -H 'Content-Type: application/json'   -d '{"username":"admin","password":"Admin!23456","client_label":"initial-setup"}'
```

之后通过 `/api/auth/login` 获取会话令牌，并在管理接口请求头中使用 `Authorization: Bearer <token>`。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、井点样本、同位素约束、迁移计算、任务恢复和数据库时间格式。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 模型版本结果比较

研究负责人在接受模型升级前，可以在同一数据集上比较两个模型版本的计算结果，无需人工下载两份 JSON 再比对。

```bash
# 比较同一支样本上的两个反演任务（补给比例、拟合残差）
curl -sS -X POST http://127.0.0.1:8432/api/hydro/comparisons/inversions \
  -H 'Content-Type: application/json' \
  -d '{"baseline_task_id": 1, "candidate_task_id": 2, "uncertainty_band": 0.05,
       "thresholds": {"rmse_max": 0.5}}'

# 比较同一井点上的两次迁移计算（污染峰值、到达时间、浓度曲线）
curl -sS -X POST http://127.0.0.1:8432/api/hydro/comparisons/transport \
  -H 'Content-Type: application/json' \
  -d '{"baseline_task_id": 1, "candidate_task_id": 2, "align_step_days": 5,
       "thresholds": {"concentration_limit": 0.05, "arrival_time_max": 60}}'

# 稳定排序的摘要列表（kind、dataset_id、id 升序），支持 kind / dataset_id / page / size 过滤
curl -sS 'http://127.0.0.1:8432/api/hydro/comparisons?kind=transport'

# 单份报告的完整摘要与明细
curl -sS http://127.0.0.1:8432/api/hydro/comparisons/1
```

比较规则：

- 反演结果先统一端元（按端元 id 对齐，id 集合不一致时回退到同名端元），再比较各端元补给比例、质量平衡与拟合残差；迁移结果先在两条曲线的公共时间域上按统一步长重采样（线性插值，不外推），再比较峰值浓度、到达时间和逐点浓度。
- 每个数值量都给出绝对差、带符号相对差和区间重叠（Jaccard 系数与较窄区间覆盖率）。区间由 `uncertainty_band` 相对半宽构造；未提供时，端元比例回退到端元自身的标称不确定度。
- `thresholds` 支持 `rmse_max`、`concentration_limit`、`arrival_time_max`，报告会给出越界状态变化（improved / worsened / stays_*）以及首次超标时间的提前或推迟。
- 数据或参数不兼容的字段（单侧端元、端元定义漂移、物理参数不一致、时间轴无交集等）进入报告的 `incomparable` 列表并注明原因；两个任务不属于同一数据集时直接返回 422。
- 报告保存输入指纹（两侧任务的输入与结果哈希）与生成时间；任务对与比较选项完全相同的重复请求复用已有报告，不重复计算。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         灾情、事件、公告、部门和信访业务接口
  hydro/            地下水、同位素反演和污染迁移服务
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。
