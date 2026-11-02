# 交付清单与阶段完成情况

| 阶段 | 实际交付 | 状态 |
| --- | --- | --- |
| 项目与需求 | GitHub 仓库、需求映射、架构设计 | 完成 |
| 模型与版本 | 固定划分训练、两类模型、指标、指纹、不可覆盖登记与校验 | 完成 |
| 服务与发布 | 预测/反馈、健康检查、管理鉴权、发布/晋升/人工恢复 | 完成 |
| 监控与展示 | 中文控制台、请求曲线、窗口指标、Prometheus、Grafana 配置 | 完成 |
| 回滚闭环 | 连续异常规则、部署隔离、回滚验证、冷却、失败停止、持久化与重启恢复 | 完成 |
| 故障实验 | 浏览器/CLI HTTP 演练、三类实测 JSON 报告、恢复验证 | 完成 |
| 工程与文档 | Windows 一键启动、Docker 编排、CI、自动化测试、运行与答辩材料 | 完成；容器运行状态见 CI |

## 交付入口

- 应用入口：`start.cmd` / `python -m src --demo --open-browser`
- 完整演练：控制台“闭环演练” / `python scripts/demo.py --kind all`
- 容器入口：`deploy/compose.yml`
- 原始实测：`reports/validation/`
- 答辩材料：`docs/project-report.md`
- 技术与排障：`docs/architecture.md`、`docs/runbook.md`

## 范围控制

首版没有将备选工具名称当作已实现功能：采用自建 SQLite 模型注册库，未集成 MLflow；没有 Kubernetes、多副本灰度、生产权限体系或自动再训练。以上均作为后续拓展，而非本次答辩中的完成项。
