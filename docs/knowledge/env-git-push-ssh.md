# HTTPS push 会永久卡死，push 走 SSH

**症状**：`git push` 显示 `Pushing to https://...` 后再无任何输出，永不结束。

定位（`GIT_TRACE=1` + `GIT_CURL_VERBOSE=1`）：GitHub 回 `401 Unauthorized` →
git 调 `git credential-helper-selector get`（全局配置里的 WorkBuddy 注入项）→
没有已存凭据，助手在**无 TTY** 环境下等待输入，永远不返回。
`GIT_TERMINAL_PROMPT=0` / `GCM_INTERACTIVE=never` 都救不回来。

**本机 `~/.ssh/id_rsa` 已在 GitHub 授权**，所以把 push 改走 SSH：

```bash
git remote set-url --push origin git@github.com:Hana-ame/kuuki-mouse.git
```

（fetch 仍走 HTTPS —— 读操作不需要凭据，正常。）

本地分支名与远端不一致时（扁平名 ↔ 带斜杠名），要么配
`remote.origin.push <local>:refs/heads/<remote>`，要么每次用显式 refspec。

## 另一个假死：`timeout -s KILL` + 管道

`timeout 30s git push | tail -25` 会跑 3～6 分钟不结束 —— git 的子进程（凭据助手）
**继承了管道写端**，父进程被杀但子进程没有，`tail` 就一直等 EOF。

**不要在长时间命令外面套管道。** 重定向到文件后台跑，再轮询文件。
