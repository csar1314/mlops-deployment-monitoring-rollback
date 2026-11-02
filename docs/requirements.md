# 需求与验收映射

本版交付为单机模型运维系统。以下功能均有实际代码，测试结果与实测记录见 [测试报告](test-report.md)。

| 编号 | 实现与验收 | 证据 |
| --- | --- | --- |
| FR-01 | 训练与登记不可覆盖版本，保存评估指标和工件校验值 | `test_training_registry_and_immutable_versions` |
| FR-02 | 四特征预测、输入校验、失败返回；预测与真实标签关联 | `test_invalid_input_is_rejected_without_polluting_predict_metrics`、`test_inference_feedback_and_prometheus_export` |
| FR-03 | 存活、就绪、当前实际服务版本查询；探针不污染指标 | `test_dashboard_docs_and_readiness`、`test_health_checks_do_not_count_as_predictions` |
| FR-04 | 加载、哈希、协议与推理预检；失败保留原版本 | `test_corrupt_candidate_is_rejected_and_original_kept` |
| FR-05 | 预测窗口指标、Prometheus、控制台、Grafana 配置 | `test_inference_feedback_and_prometheus_export`、`test_grafana_dashboard_and_prometheus_rules` |
| FR-06 | 配置阈值、最小样本量、连续评估次数与持续时间 | `test_same_requests_cannot_inflate_consecutive_breaches`、`test_minimum_duration_prevents_early_rollback` |
| FR-07 | 具备预期部署 ID 的人工回滚、切换后验证 | `test_concurrent_duplicate_rollbacks_execute_once` |
| FR-08 | 持续错误、延迟和带真实标签效果下降触发自动回滚 | `test_error_rate_triggers_exactly_one_verified_rollback`、`test_latency_rollback_with_real_sleep`、`test_accuracy_requires_labels_then_triggers_rollback`；三份 HTTP 演练报告 |
| FR-09 | 冷却、幂等、工件失效、失败停止与人工恢复 | `test_cooldown_blocks_redeploy_and_auto_flapping`、`test_failed_rollback_stops_automation_until_manual_recovery`、`test_emergency_recovery_closes_failed_rollback_path` |
| FR-10 | 版本、时间、原因、操作与部署 ID、恢复证据可追溯 | `test_full_demo_report_uses_observed_rollback`、`/api/events`、`/api/deployments`、`/api/experiments` |

## 非功能验收

- **可复现**：Python 3.11、固定依赖快照、固定随机种子、自带公开数据；无需另行下载模型或申请 API Key。
- **可维护**：API、领域引擎、存储、训练、控制器与静态控制台分模块；跨平台 CI。
- **可观察**：错误/延迟/效果与监控失联分开报告，无样本不等于健康；记录结构化操作事件。
- **可恢复**：发布前后均校验；失败保留旧指针；重启恢复持久状态；紧急恢复关闭故障处理死路。
- **操作保护**：管理令牌、同源控制台、无上传接口、OS 单实例锁、受控故障 TTL、演练超时清理；凭据与运行目录不提交。

## 最终演示验收步骤

1. 双击 `start.cmd`，确认 v1 就绪与两个模型登记信息。
2. 运行三种“一键完整闭环”，观察 v2 发布、真实请求、持续异常、一次自动回滚和恢复结果。
3. 下载报告，核对候选部署 ID、回滚原因、耗时、恢复版本和带标签结果。
4. 展示低流量不触发、无标签不判断准确率、重复告警不重复回滚、回滚失败进入人工处理等测试证据。
5. 使用 Docker 的环境另行验证 API、独立控制器、Prometheus 与 Grafana；本机未安装 Docker 时以实际 CI 结果为容器验收依据。
