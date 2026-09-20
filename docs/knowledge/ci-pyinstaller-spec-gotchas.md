# PyInstaller：onedir + 路径基准

## 只用 onedir，不用 onefile

aiortc / av 带一堆 DLL，onefile 每次启动要把 124MB 解压到临时目录 ——
启动慢到不能接受。产物是**目录**形式（zip 57.2MB，解压 124MB）。

## spec 里的相对路径是相对 SPECPATH，不是 CWD

```python
# 错：会被解析成 packaging/packaging/agent_entry.py
a = Analysis(["packaging/agent_entry.py"], ...)

# 对
a = Analysis([os.path.join(SPECPATH, "..", "packaging", "agent_entry.py")], ...)
```

## hiddenimports 要列全惰性 import

spec 的 `hiddenimports` 里必须列三处**运行期惰性 import**：peerjs fork / app /
controller —— 静态分析扫不到它们，漏了就是「打包成功、运行 ImportError」。

配套 `packaging/check_bundle.py` 用来验证（见下一条）。
