# CI 端到端要用随机端口 —— 连着 push 时两个 run 会同时起

## 现象

一次 push 里连着两个 commit（间隔一两分钟），第二个 run 挂在
「端到端 —— 源码客户端真连一次打包后的 WS / gRPC」上，`exit 1`；而它前面的
单元测试、打包、产物检查、冒烟全绿。重跑一次（下一个 commit）又全绿。

## 根因

GitHub 会把这两个 run **同时启动**：查 API 能看到两个 run 的
`run_started_at` 是同一秒（`concurrency: cancel-in-progress: true` 并没有把
前一个取消掉）。而端到端那一步起 exe 用的是**写死的端口**（18765 / 50052）:

```bash
"$EXE" --ws --grpc --ws-port 18765 --grpc-port 50052 &
```

后起的那个 exe 绑不上端口 → `agent_e2e.log` 里没有「已启动」/「仅回环安全」，
或者 client 连到了**先起的那个** run 的服务上 → 断言失败。跟代码改动无关，
是典型的 flaky。

## 怎么做

端口随机，别写死:

```bash
WS_PORT=$(( 18000 + RANDOM % 8000 ))
GRPC_PORT=$(( WS_PORT + 1 ))
"$EXE" --ws --grpc --ws-port "$WS_PORT" --grpc-port "$GRPC_PORT" &
# 后面所有 client 命令都用 ws://127.0.0.1:$WS_PORT / 127.0.0.1:$GRPC_PORT
```

顺带把默认端口（8765 / 50051）也避开 —— 那是 runner 上别的东西可能占的。

## 排查 CI 的手法（本机没有 gh、也没有 token）

匿名拿不到 job logs（`GET /actions/jobs/{id}/logs` 返回 403），但**看得到结论**:

```bash
# 最近几次 run 的结论与启动时间 —— 两个 run 同一秒启动 = 并发 = 端口冲突嫌疑
curl -s "https://api.github.com/repos/<o>/<r>/actions/runs?per_page=4" \
  | grep -E '"display_title"|"conclusion"|"run_started_at"'

# 哪一步红的
curl -s "https://api.github.com/repos/<o>/<r>/actions/runs/<run_id>/jobs" \
  | grep -E '"name"|"conclusion"'

# annotations（错误通常只有一句 "Process completed with exit code 1"，够定位步骤）
curl -s "https://api.github.com/repos/<o>/<r>/check-runs/<check_run_id>/annotations"
```

看不到日志正文时，替代办法是**把嫌疑路径也加进端到端**再推一次：新覆盖的
那几行绿了，就说明原实现没问题，红的是环境（本次就是靠加 `screen.monitors`
与 `screenshot --monitor 0` 两条确认的）。
