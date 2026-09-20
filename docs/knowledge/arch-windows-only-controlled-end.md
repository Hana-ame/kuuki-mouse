# 受控端只支持 Windows（产品定位）

**这是产品定位，不是临时限制。** 受控端 = 被操作的那台机器 = 服务端，必须原生跑在
Windows 上。控制端（客户端）不挑平台 —— WSL / Linux / 手机都只能当控制端。

落地为两部分，改动时两边都要跟：

1. **代码门禁** `remote/__main__.py`
   - `SUPPORTED_PLATFORMS = ("win32",)`，`platform_refusal()` 返回 `None` 表示放行
   - 调用点在 `main()` 里、`parse_args()` 之后 —— 于是 `--help` / `--version` 在
     parse 阶段就退出、**不受门禁影响**；`--selftest` 在其后、**会被拦**（自检同样
     碰屏幕和输入，放宽它没有意义）
   - 拒绝时**退出码 2**，与既有的「`--allow-remote` 缺 token」拒绝保持一致
   - `platform` 参数只为测试注入存在，测试靠它模拟非 Windows，不用真跑别的平台
2. **文档口径**：README / remote/README / docs 里凡提到平台都要一致，别写
   「跨平台」这类泛泛说法 —— 会让人把 WSL 当服务端用。

## 非 Windows 代码路径：保留但标注「不再维护」

用户明确选择**不删**，只加注释说明是死代码：

- `remote/input.py`：`_LINUX`（X11 键预检 `_x11_reason` 等）、`paste_text` 的 darwin 分支
- `remote/screen.py`：`grab_image` 里的 Linux `xdisplay` 分支
