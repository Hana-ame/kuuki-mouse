# 判断模块有没有打进包，要看 PYZ toc

**别用「`_internal/` 下有没有这个目录」来判断** —— 纯 Python 包（peerjs / pynput /
aiortc / aioice）打包后进入的是 **PYZ 归档**，`_internal/` 下不会出现它们的目录。
按目录查会**假报警**（说没打进去，其实进去了）。

正确做法是读 PYZ 存档的 `PYZ-*.pyz` 里的 `PYZ-*.toc`：

- **它是 repr 文本格式**，用 `ast.literal_eval` 读
- **不是 pickle**（很容易想当然以为是，然后 loads 炸掉）

`packaging/check_bundle.py` 做这件事，其中一组专门盯着 `remote/proto/*_pb2.py`
（gRPC 存根）—— 它们被 `.gitignore` 挡了一道（靠 `!remote/proto/*.py` 例外救回来），
**真丢的话服务端起得来、一连就炸**，必须单独检查。
