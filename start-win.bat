@echo off
REM kuuki-mouse remote —— Windows 侧启动脚本
REM
REM 为什么在 Windows 上跑
REM --------------------
REM WSL 里的 pynput 只能操作 WSLg 的那个 X 屏幕, 而它上面没有任何窗口管理器
REM (实测 X 程序都连得上却一个都不 map, 截屏恒定 5.5KB 全黑)。所以要让 agent
REM 真正"看屏幕 / 操作这台电脑", 就在 Windows 上直接跑这个 Python 服务 ——
REM screen.py / input.py 在这边一行都不用改:
REM   - 截屏走 Pillow.ImageGrab (不需要 ffmpeg/ffmpeg 后端)
REM   - 输入走 pynput 的 Windows 后端 (中文/emoji/f13 都能用)
REM
REM 首次准备:
REM   E:\Python310\python.exe -m venv .venv-win
REM   .venv-win\Scripts\python.exe -m pip install pynput Pillow websockets grpcio protobuf
REM
REM 用法:
REM   start-win.bat                     默认: WS 8765 + gRPC 50051 (PeerJS 也开)
REM   start-win.bat --no-grpc           只要 WS + PeerJS
REM   start-win.bat --token secret      三个传输都要求 token
REM   start-win.bat --no-ws --no-grpc   只开 PeerJS (房间码配对, 不需要端口)

setlocal
cd /d "%~dp0"

set PY=.venv-win\Scripts\python.exe

if not exist "%PY%" (
    echo [!] 找不到 %PY%
    echo     先建环境:
    echo       E:\Python310\python.exe -m venv .venv-win
    echo       .venv-win\Scripts\python.exe -m pip install pynput Pillow websockets grpcio protobuf
    exit /b 1
)

REM 裸代理变量会把 websockets/urllib 误当 HTTP 代理 (与 main.py 同款处理)
set socks_proxy=
set SOCKS_PROXY=
set no_proxy=
set NO_PROXY=

"%PY%" -m remote %*
endlocal
