# Agent 与 Checkpoint

Python Agent 主循环，以及基于 pack 归档和容器文件增量的检查点保存、导入和续跑。

## 环境与入口

需要 Python 3.11+；运行容器捕获和恢复还需要 Docker、任务镜像及相应部署依赖。模型凭据通过环境变量提供，不写入源码。

```bash
python main.py --inspect

export MODEL_BASE_URL="https://你的兼容接口/v1"
export MODEL_NAME="你的模型"
# 通过运行环境安全设置 MODEL_API_KEY
python main.py "你的任务"
```

模型协议由 MODEL_API_MODE 设置为 chat_completions 或 responses。供应商需要时设置 MODEL_THINKING=auto。

## 检查点

设置 SCIENCE_CHECKPOINT_CONTROL_DIR 可启用 Agent 状态保存。完整容器文件捕获还需要 harness/env_watch.py 的宿主 watcher；它由 harness/task_runs.py 接入。

```bash
python scripts/restore_checkpoint.py \
  --archive /path/harness-checkpoints.tar.gz \
  --checkpoint CHECKPOINT_ID \
  --image TASK_IMAGE \
  --workspace /
```

上面的脚本创建任务容器、还原文件并验证状态导入。真实续跑另由 main.py --resume 或 scripts/verify_real_continue.py 执行；工作区、基础环境和模型配置须匹配检查点。

```bash
# 在已经还原任务文件的环境中
python main.py --resume /path/to/checkpoint --resume-timeout 600
```

container-full-access 权限模式只适用于受控 Linux 任务容器，并要求 HARNESS_EXECUTOR=contained。

## 目录

- harness/：Agent 循环、模型、上下文、工具、执行器、安全接口及宿主运行编排。
- science_checkpoint/：保存策略、pack 归档和环境文件增量。
- checkpoint_plugin/：状态契约、冻结、模型兼容校验和导入。
- safety/：路径边界与审计支持。
- scripts/：环境捕获、恢复及续跑验证入口。
- tests/：离线回归测试。
- harbor_agent.py、harbor_capture.py：Harbor 部署适配与产物收集。

技能 SKILL.md、references 和 prompts 是运行时资源，需要保留。

## 测试与限制

安装 pytest 后运行 `python -m pytest tests -q`。当前离线测试为 111 passed；未据此宣称真实模型与 Docker 完整联合恢复已通过。恢复脚本依赖共享 pack 和环境层，不支持旧 CAS/tree-linked 格式；联合保存一致性、增量父链、挂载覆盖与 pack 完整性仍有待加强。检查点不保存运行程序内存和网络连接。
