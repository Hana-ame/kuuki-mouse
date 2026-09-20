# registry 并发写会丢更新（已修：事务 + 原子写）

**症状**：多个操纵端同时往同一个 registry 写，各 add 25 台，最后只剩 26 台。

根因：load → 改 → save 不是原子的。两个进程读到同一份，各自改完写回，后写的覆盖
先写的。

修法：`Registry.transaction()` —— 在文件锁内**重新读盘**、修改、**原子写**
（`os.replace`）。

## Windows 上的三个附加坑

1. **文件锁不跨线程** —— 同一进程内的多线程不认文件锁，要额外补 `threading.Lock`
2. **判断锁文件为空不能用 `read`** —— 会 `PermissionError` 导致**静默退化成无锁**，
   这比没有锁更危险（你以为有）
3. **`os.replace` 的目标被占用会 `WinError 5`**；临时文件名只带 pid 会互相 truncate
   （多进程同 pid 不会撞，但嵌套/short-lived 进程会）

修复后实测：线程 50/50、进程 50/50 都不丢。
