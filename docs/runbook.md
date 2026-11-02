# 运行、接口与排障手册

## Windows 一键启动

双击根目录 `start.cmd`，或在 PowerShell 中：

```powershell
.\.venv\Scripts\python.exe -m src --demo --open-browser
```

启动器进入项目目录，首次自动训练基线。服务默认监听 127.0.0.1:8765，关闭终端或 Ctrl+C 停止服务。浏览器可在运行时关闭；若正在演练，先点“取消当前演练”，否则控制器在超时后清理。

如端口占用，先关闭这个项目已运行的窗口；或使用 `--port 8766`。CLI 演练须对应加 `--base-url http://127.0.0.1:8766`。不能启动两个实例共用同一 `runtime/`；确需两个独立实例时用不同 `MLOPS_DATA_DIR` 和端口。

## 首次安装与日常开发

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src scripts tests
```

`requirements.txt` 为顶层依赖，`requirements-lock.txt` 为本版已验证完整环境快照（含测试工具）。启动器会复用现有独立环境；更新项目依赖后手动重新执行安装命令。

## 管理鉴权与数据路径

首次启动生成随机令牌到 `runtime/admin-token.txt`，可由 `MLOPS_ADMIN_TOKEN` 显式提供。修改操作需 `X-Admin-Token` 请求头。浏览器通过“管理令牌”设置；一键启动自动通过 URL fragment 交给页面，然后页面移除 fragment。令牌仅保存在 sessionStorage，不写进仓库。

`runtime/` 保存：SQLite、模型工件、私有令牌、单实例锁文件与服务端实验报告。模型工件只能来自本机受控训练。不要将不可信 joblib/pickle 文件放入注册库。备份时停止 API 与控制器，一起备份整个运行目录；不要只拷贝正在写入的 SQLite 主文件。

## 主要接口

| 方法与路径 | 说明 | 管理令牌 |
| --- | --- | --- |
| GET `/health/live`、`/health/ready` | 存活、模型就绪和实际版本 | 否 |
| POST `/api/predict` | `{"features":[5.1,3.5,1.4,0.2]}` | 否 |
| POST `/api/feedback` | 预测 UUID 与真实标签 0/1/2；已有不同标签不可覆盖 | 是 |
| GET `/api/models`、`/api/status` | 注册信息、部署窗口与控制器状态 | 否 |
| POST `/api/models/train` | 新版本与 `logistic` / `forest` 算法 | 是 |
| POST `/api/deploy` | 发布登记版本，不改变稳定指针 | 是 |
| POST `/api/models/{version}/promote` | 健康请求与标签达标后晋升稳定版本 | 是 |
| POST `/api/rollback` | 带 `expected_deployment_id` 的人工回滚 | 是 |
| POST `/api/recover` | 仅异常/无模型状态：紧急指定有效版本恢复 | 是 |
| PUT `/api/policy` | 完整规则对象；未提供项使用默认值 | 是 |
| POST `/api/control/evaluate` | 独立控制器评估入口 | 是 |
| POST `/api/fault` | 演示模式的错误/延迟/效果故障与 TTL | 是 |
| POST `/api/demo/prepare` | 开始演练，保存原规则并恢复 v1 演示基线 | 是 |
| POST `/api/demo/{id}/finish`、`/abort` | 核对实际恢复证据后报告 / 清理取消 | 是 |
| GET `/api/events`、`/api/deployments`、`/api/experiments` | 审计、发布与实验记录 | 否 |
| GET `/metrics` | Prometheus 指标；健康探针不计入预测指标 | 否 |

完整字段与参数校验见运行中的 `/docs`。

## 真实 HTTP 自动演练

保持服务运行，另开项目终端：

```powershell
.\.venv\Scripts\python.exe scripts\demo.py --kind all
```

只演示一种时选 `--kind error` / `latency` / `accuracy`。客户端不调用控制器评估接口，等待正常定时控制器触发。原始报告写入 `reports/validation/`，运行时也保存在 SQLite 和 `runtime/reports/`。

演练会临时切换演示规则和稳定基线，不能与正常发布操作同时进行。CLI 异常退出会尝试取消并恢复；断电或强制杀进程后的未完成演练在服务重启时清理。

## 分离进程运行控制器

Windows 默认在 API 生命周期内运行独立监控线程。需要模拟 Compose 进程分离时，第一窗口设置：

```powershell
$env:MLOPS_CONTROLLER='0'
.\.venv\Scripts\python.exe -m src --demo
```

第二窗口：

```powershell
$env:MLOPS_ADMIN_TOKEN=(Get-Content runtime\admin-token.txt -Raw).Trim()
$env:MLOPS_API_URL='http://127.0.0.1:8765'
.\.venv\Scripts\python.exe -m src.controller
```

控制器只调用鉴权 API，不直接读取/改写 SQLite。不要在已有内置控制器时再启动第二个控制器；服务端锁可防止重复切换，但并非多控制器的分布式选主实现。

## Docker Compose

需要可用的 Docker Engine 与 Compose v2。本项目不会自动修改系统虚拟化或安装 Docker Desktop。

```bash
python scripts/make_env.py
docker compose --env-file .env -f deploy/compose.yml config --quiet
docker compose --env-file .env -f deploy/compose.yml up --build --detach --wait
```

API、独立控制器、Prometheus、Grafana 四个服务；映射端口分别为 8765、无、9090、3000。Grafana 账号 `admin`，密码与管理令牌见本机 `.env`。在控制台“管理令牌”中填写 `.env` 的 `MLOPS_ADMIN_TOKEN`。

停止：`docker compose --env-file .env -f deploy/compose.yml down`。命名卷保留数据；不要为了排障直接删除卷。设置 `MLOPS_ENABLE_DEMO=0` 可关闭演练入口。变更 Grafana 初始密码变量不会覆盖已有持久化用户密码。

## 故障处理

| 现象 | 原因与操作 |
| --- | --- |
| 管理操作 401 | 当前标签页没有有效令牌；从当前环境的令牌文件/.env 填写，别混用本机和 Docker 令牌 |
| 409：冷却期 | 等待冷却结束，或通过受控演练入口明确开始新演练 |
| 409：演练进行中 | 在演练页面取消现有演练；超时会自动清理 |
| 无标签时准确率为空 | 正常行为；需回传真实标签且达到最小标签数 |
| 监控心跳待确认 | 查看控制器是否运行及令牌/地址是否正确；未确认心跳不视为健康 |
| 模型损坏导致回滚失败 | 自动操作停止；修复可信工件后人工回滚，或训练新版本并紧急恢复 |
| 当前与稳定工件均不可用 | 服务 readiness=503；管理 API 仍可训练，随后调用 `/api/recover`，控制台会显示紧急恢复按钮 |
| scikit-learn 版本不匹配 | 使用锁定环境，或重新训练登记新版本；不要跳过校验强行加载 |
| 数据目录已被使用 | 另一实例尚在运行；关闭旧实例或选择独立目录，不能用多 worker 共享模型状态 |
| API 进程整体不可用 | 先恢复进程/容器与磁盘，再根据事件及持久化状态核验模型；不能把 API 自身宕机当成已自动回滚 |

## 上线范围

当前读接口为本地演示公开信息，管理接口有单令牌鉴权；不提供用户/角色隔离。若部署到外网，需补充 TLS、网关与访问控制、凭据轮换、外部可用性恢复、备份和多副本设计。当前示例不处理真实个人数据。
