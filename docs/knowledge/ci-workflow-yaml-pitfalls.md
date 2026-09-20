# CI workflow 里两处会「静默失效」的写法

## 1. `permissions` 不支持 `${{ }}` 表达式

```yaml
permissions:
  contents: ${{ startsWith(github.ref, 'refs/tags/v') && 'write' || 'read' }}  # 错
```

写表达式会让**整个 workflow 文件解析失败**：run 0 秒 failure、报 "workflow file issue"、
**而且取不到日志** —— 你只会看到一个红的 0 秒构建，看不到任何原因。

只能给字面值 `read` / `write` / `none`。

## 2. `if-no-files-found: ignore` 会掩盖路径错误

warn 日志的实际路径在 `workpath/<spec 名>/` 下（不是 workpath 根），路径写错就一直
skip —— 而 `if-no-files-found: ignore` 让它**一声不吭**。

**凡是用了 ignore 的上传步骤，都要单独确认一次文件真的存在过**，否则「从来没有
artifact」会被当成「一切正常」。
