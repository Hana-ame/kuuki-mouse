# 中文输出在非中文 Windows 上会崩

**症状**：英文版 Windows 的控制台代码页是 cp1252，一个中文都编码不了 ——
argparse 打印帮助（`--help` / `--version`）时直接 `UnicodeEncodeError` 崩掉。

本地中文系统（cp936）**复现不出来**，所以这个问题只在 CI 上暴露。

修法：`remote/__main__.py` 里的 `force_utf8_stdio()`

```python
def main():
    force_utf8_stdio()          # 必须在 parse_args 之前
    args = build_parser().parse_args()
```

**为什么必须在 `parse_args` 之前**：`--help` / `--version` 是在 `parse_args()` **内部**
输出并退出的，等到它返回就太晚了。流没有 `reconfigure` 时静默跳过，`errors="replace"`
兜底（宁可显示成问号，也不要崩）。

后续又有两个脚本踩到同一个坑：`check_bundle.py`（分组名是中文）、
`check_ctl_docs.py`（文档里的中文命令）、`test_attitude.py`（结果输出）。
