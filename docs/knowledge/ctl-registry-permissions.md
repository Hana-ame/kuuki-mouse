# registry 里有 token 明文，文件权限要收紧

`machines add --token xxx` 会把 token 明文写进 registry。新建文件时调
`restrict_permissions()`：

- POSIX：`chmod 600`
- Windows：**先 `icacls /grant:r <user>:(F)` 再 `/inheritance:r`** ——
  **顺序不能反**，反了会先把继承关系断开，可能把 ACL 清空，等于自己锁死自己

全程 `try/except` 吞异常：权限收紧失败不该让「加机器」这个主操作挂掉，但下一次打开
的 registry 权限不对时应该被发现。
