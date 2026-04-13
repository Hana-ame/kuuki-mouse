# server.py
import asyncio
import ssl
import websockets
import http.server
import os
import json
import subprocess
from pathlib import Path
from datetime import datetime

# from cert import generate_self_signed_cert # 移除自签名证书生成

from app import App
from get_local_ip import get_local_ip

# --- 服务器配置 ---
HOST = "0.0.0.0"
HTTP_PORT = 8443
WWW_DIRECTORY = "www"

# --- 远程证书配置 ---
# 请在此处填写远程服务器的 SSH 地址 (例如: root@d.moonchan.xyz 或 SSH config 中的 Host 别名)
# 因为 id_rsa 已加载，这里不需要密码，直接使用 ssh/scp 即可。
CERT_SOURCE_SERVER = "root@vps.moonchan.xyz"  # 请根据实际情况修改此处

# 远程证书路径 (根据 acme.sh 的默认规则，目录名通常为域名，此处按你的描述配置)
# 注意：acme.sh 通常会将 * 转义为 _ ，即目录可能是 _.d.moonchan.xyz
# 如果你的服务器上确实是 *.d.moonchan.xyz，请保持原样。
REMOTE_CERT_DIR = "/root/.acme.sh/*.d.moonchan.xyz"
REMOTE_KEY_FILE = "*.d.moonchan.xyz.key"
REMOTE_CERT_FILE = "fullchain.cer"

# 本地保存路径
LOCAL_KEY_FILE = "key.pem"
LOCAL_CERT_FILE = "cert.pem"

# --- 3. 实例化与辅助函数 ---
app = App()


def get_data(x, y, z, alpha, beta, gamma):
    app.update_data(x, y, z, alpha, beta, gamma)


def mouse_event(message: str):
    app.click_mouse(message)


def text_event(message: str):
    app.paste_text(message)


def key_event(message: str):
    app.tap_key(message)


def key_down(key: str):
    app.key_down(key)


def key_up(key: str):
    app.key_up(key)


# --- WebSocket 处理器 ---
# --- 4. WebSocket 处理器 (重写以适配新前端) ---
# --- 4. WebSocket 处理器 ---
async def websocket_handler(websocket, path):
    print(f"WebSocket 连接已建立：{path}")
    try:
        async for message in websocket:
            try:
                data_dict = json.loads(message)
                msg_type = data_dict.get("type")

                if msg_type == "key":
                    key = data_dict.get("key")
                    action = data_dict.get("action")
                    if action == "down":
                        app.key_down(key)
                    elif action == "up":
                        app.key_up(key)

                elif msg_type == "mouse":
                    button = data_dict.get("button")
                    action = data_dict.get("action")
                    if action == "down":
                        app.press_mouse(button)
                    elif action == "up":
                        app.release_mouse(button)

                elif msg_type == "wheel":
                    app.scroll_mouse(data_dict.get("delta", 0))

                elif msg_type == "text":
                    app.paste_text(data_dict.get("text"))

                elif msg_type == "gyro":
                    get_data(
                        0,
                        0,
                        0,
                        data_dict.get("alpha"),
                        data_dict.get("beta"),
                        data_dict.get("gamma"),
                    )

            except Exception as e:
                print(datetime.now(), "handle error:", e)

    except websockets.exceptions.ConnectionClosed as e:
        print(f"WebSocket 连接已关闭: {e}")
    except Exception as e:
        print(f"WebSocket 处理中发生错误: {e}")


# --- HTTP 文件服务处理器 ---
class FileHandler(http.server.SimpleHTTPRequestHandler):
    """处理HTTP请求并提供文件的处理器。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WWW_DIRECTORY, **kwargs)


# --- 证书下载逻辑 ---
def download_certs_via_scp():
    """使用 SSH cat 和 find 从远程服务器动态定位并下载证书文件。"""
    print(f"正在从 {CERT_SOURCE_SERVER} 下载证书...")
    import os
    try:
        # 使用数组传参和 stdout 重定向，彻底避开 Windows cmd.exe 对管道符 | 的错误本地拦截
        key_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", CERT_SOURCE_SERVER, 'find /root/.acme.sh -name "*d.moonchan.xyz.key" | head -n 1 | xargs cat']
        cert_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", CERT_SOURCE_SERVER, 'find /root/.acme.sh -name "fullchain.cer" -path "*d.moonchan.xyz*" | head -n 1 | xargs cat']
        
        print(f"执行命令获取私钥...")
        with open(LOCAL_KEY_FILE, "wb") as f:
            subprocess.run(key_cmd, stdout=f, check=True)
        
        print(f"执行命令获取证书...")
        with open(LOCAL_CERT_FILE, "wb") as f:
            subprocess.run(cert_cmd, stdout=f, check=True)
        
        if os.path.exists(LOCAL_KEY_FILE) and os.path.getsize(LOCAL_KEY_FILE) > 0:
            print("证书下载成功。")
            return True
        else:
            print("证书下载失败：文件为空或不存在。")
            return False
            
    except Exception as e:
        print(f"证书下载发生错误: {e}")
        return False


# --- 主函数 ---
async def main():
    """主函数，用于启动服务器。"""

    # 尝试下载证书
    if not download_certs_via_scp():
        if os.path.exists(LOCAL_CERT_FILE) and os.path.exists(LOCAL_KEY_FILE):
            print("警告：无法更新证书，但检测到本地已存在证书文件，跳过下载并继续启动。")
        else:
            print("错误：无法下载证书且本地无旧证书，服务器启动失败。")
            return

    # 检查证书文件是否存在
    if not os.path.exists(LOCAL_CERT_FILE) or not os.path.exists(LOCAL_KEY_FILE):
        print("错误：下载后仍未找到证书文件。")
        return

    # 配置SSL上下文
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(LOCAL_CERT_FILE, LOCAL_KEY_FILE)
    print("SSL 上下文已配置。")

    # 创建并运行WebSocket服务器
    async with websockets.serve(
        websocket_handler,
        HOST,
        HTTP_PORT,
        ssl=ssl_context,
        process_request=http_server_handler,
    ):
        print(
            f"HTTPS 和 WebSocket 服务器正在运行，请访问: https://laptop.d.moonchan.xyz:{HTTP_PORT}"
        )
        await asyncio.Future()  # 永远运行


def http_server_handler(path, request_headers):
    """
    一个回退函数，用于处理非WebSocket的HTTP请求。
    """

    if "Upgrade" in request_headers and request_headers["Upgrade"] == "websocket":
        return  # 让 `websockets` 库处理它

    if path == "/":
        path = "/index.html"

    path = path.split("?")[0]

    file_path = os.path.abspath(os.path.join(WWW_DIRECTORY, path.lstrip("/")))

    if os.path.commonpath(
        [file_path, os.path.abspath(WWW_DIRECTORY)]
    ) != os.path.abspath(WWW_DIRECTORY):
        return (http.HTTPStatus.FORBIDDEN, [], b"Forbidden")

    if os.path.exists(file_path) and os.path.isfile(file_path):
        content_type = {
            ".html": "text/html",
            ".css": "text/css",
            ".js": "application/javascript",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".json": "application/json",
        }.get(Path(file_path).suffix, "application/octet-stream")
        with open(file_path, "rb") as f:
            content = f.read()
        return (http.HTTPStatus.OK, [("Content-Type", content_type)], content)
    else:
        return (http.HTTPStatus.NOT_FOUND, [], b"Not Found")


if __name__ == "__main__":
    # 根据 host 在 laptop.d.moonchan.xyz 的要求，提示用户
    # 假设 DNS 已解析到本机 IP
    # print(f"请访问 https://{get_local_ip()}:{HTTP_PORT}")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n服务器正在关闭。")
