# 测试与实验报告

## 验证环境与方式

- 日期：2026-11-02（北京时间）；Python 3.11.9，Windows 本机 CPU 运行。
- API 与控制器以两个独立 Python 进程运行；HTTP 客户端不调用规则评估接口，不直接调用回滚接口。
- 自动化测试：**44 项通过，0 失败，0 错误**。pytest 原始 XML 位于本机 `runtime/test-results.xml`；公开汇总见 [summary.json](../reports/validation/summary.json)。
- 浏览器控制台：实际点击“一键运行完整闭环”，观察到“闭环验证通过”，并核对服务端实验记录。
- 本机未安装 Docker；完整容器构建、四服务编排、三类 HTTP 演练、Prometheus 采集和 Grafana 健康检查已在 [GitHub Actions](https://github.com/csar1314/mlops-deployment-monitoring-rollback/actions/runs/41382638894) 通过。容器原始实验报告见 [reports/ci](../reports/ci)。

## 三类真实 HTTP 演练

| 故障类型 | 结果 | 故障注入到回滚验证完成（秒） | 回滚执行（毫秒） | 候选请求数 |
| --- | --- | --- | --- | --- |
| 服务错误 | passed | 3.178 | 13.507 | 33 |
| 延迟升高 | passed | 3.856 | 12.823 | 18 |
| 带标签效果下降 | passed | 2.792 | 12.658 | 28 |

“故障恢复时间”包含异常证据积累、控制器评估周期及切换验证；“回滚执行时间”仅覆盖稳定模型加载、校验、指针切换和状态提交。数值是本次教学实验实测，不是 SLA 或性能上限。

原始记录：[服务错误](../reports/validation/demo-error.json)、[延迟升高](../reports/validation/demo-latency.json)、[效果下降](../reports/validation/demo-accuracy.json)、[浏览器演练](../reports/validation/browser-demo.json)。每份记录包含来源部署 ID、真实事件序列、恢复版本、候选/恢复指标和逐项检查结果。

## 自动化测试覆盖

1. 模型训练登记、版本不可覆盖、哈希损坏拒绝发布、候选与稳定版本分离。
2. 服务错误、实际延迟和带标签效果异常触发一次自动回滚。
3. 无数据、低样本、短暂异常、未达持续时间、重复指标快照与冷却期保护。
4. 候选部署间指标隔离、稳定版本自身异常只告警、并发过期回滚拒绝。
5. 回滚预检失败、切换后验证失败、停止自动重试、修复后人工回滚及紧急恢复。
6. 重启恢复、OS 单实例锁、标签不可覆盖、健康晋升条件。
7. 管理鉴权、非法/非有限输入返回合法 422、健康探针不污染预测指标、Prometheus 指标导出。
8. 演练报告不能提前通过、取消恢复规则、浏览器离开后超时清理、伪造实验 ID 不更改部署。
9. 空令牌文件自动生成有效随机凭据，拒绝重复实例时不改变正在运行服务的令牌；CLI 真实启动及内置控制器验证通过。
10. Docker 组件与挂载路径、Grafana JSON、Prometheus 规则配置的静态验证。

## 可复现命令

```bash
python -m pytest -q --junitxml=runtime/test-results.xml
python -m ruff check src scripts tests
python scripts/demo.py --kind all
```

第三条需要先启动 API 与控制器。测试只使用各自临时目录，不修改真实运行环境。CI 另行在 Windows/Linux 运行测试，并在 Linux Docker 环境启动四个服务、执行三类 HTTP 演练、核对 Prometheus 目标与 Grafana 健康状态。

## 尚不属于本次实测的结论

没有测试生产持续大流量、多机故障、Kubernetes、真实在线标签延迟或跨版本滚动升级。因此不据此承诺生产高可用或零停机。效果下降演练使用已知标签的公开测试数据和显式故障开关，不声称检测了真实线上概念漂移。
