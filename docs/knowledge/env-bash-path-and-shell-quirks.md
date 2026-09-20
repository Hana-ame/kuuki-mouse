# Git Bash / Windows 侧的零碎坑

- **`/d/...` 路径传不进 Windows 原生 python.exe**（报 `directory does not exist`），
  要 `cygpath -w` 转换。MSYS 程序（sed / ls）用原路径即可
- **pip 会走系统代理变量**：环境里 `HTTP_PROXY=http://127.0.0.1:10809/`，而那个代理
  常常没在监听，pip 报 `WinError 10061`。装包前清掉：
  `env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy <命令>`
- **heredoc 遇到「中文 + 引号」组合偶尔报** `unexpected EOF while looking for
  matching "'"` —— 改用「临时文件 + `cat file >> 目标`」更稳
- **PowerShell 工具的输出捕获失效**（exit 0 但 stdout 恒空），不要用它取结果
- **`gen_proto.sh` 里别写死 `python3`**：Windows 上要依次探测 `$PYTHON / python3 / python`
- **Bash 工具的 PATH 没有 coreutils**，需要时前置 PortableGit 的 bin 目录
