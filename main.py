# kuuki-mouse 桌面端
#
# 职责(只有两个): ① 姿态解算 -> 控制鼠标  ② 出二维码给手机扫码配对
# 配对/传输全部走公开服务, 不再自己 host:
#   - PeerJS 公开 cloud broker (0.peerjs.com): 主数据通道 (手机 -> 本机, WebRTC)
#     (peerjs 库已 fork 到项目 peerjs/ 目录, 含 py3.12 兼容补丁, 见 README)
#   - MQTT 公开 broker (HiveMQ broker.hivemq.com, 与 my-node-app 同款): 房间公告 + 兜底数据通道
#   - 手机页面发布在 GitHub Pages, 二维码内容 = 页面 URL + "#/<房间码>"
#
# 运行: python main.py
import asyncio
import datetime
import json
import os
import sys
import threading

# 裸代理环境变量(如 socks_proxy=172.29.80.1:10808, 无 scheme)会被 websockets/urllib 误当 HTTP 代理,
# 而 0.peerjs.com / broker.hivemq.com 都可直连, 故启动时清掉这类裸 host:port 的代理变量。
for _k in list(os.environ):
    if "proxy" in _k.lower() and os.environ[_k] and "://" not in os.environ[_k]:
        del os.environ[_k]

from peerjs.peer import Peer, PeerOptions
from peerjs.enums import ConnectionEventType, PeerEventType
import paho.mqtt.client as mqtt
import qrcode

from app import App, handle_message

# ---------------- 配置 ----------------
PEER_PREFIX = "kuuki-mouse"                # PeerJS id 前缀, 完整 id = f"{PEER_PREFIX}-{room}"
MQTT_HOST = "broker.hivemq.com"            # HiveMQ 公共 broker (my-node-app 用同款服务)
MQTT_PORT = 1883
MQTT_ROOT = "kuuki-mouse"                  # MQTT topic 根
PAGE_URL = "https://hana-ame.github.io/kuuki-mouse/"   # GitHub Pages 实际地址 (部署 web/ 后生效)

ROOM_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # 去掉易混淆字符, 与 my-node-app 同款
ROOM_LEN = 5
HEARTBEAT_INTERVAL = 2.0                   # MQTT 房间公告心跳(秒)

# ---------------- 工具 ----------------
def gen_room_code() -> str:
    return "".join(ROOM_ALPHABET[b % len(ROOM_ALPHABET)] for b in os.urandom(ROOM_LEN))


def show_qr(url: str, room: str) -> None:
    """在终端打印二维码并保存 PNG + 尝试打开图片, 供手机扫码。"""
    qr = qrcode.QRCode(border=2, box_size=8)
    qr.add_data(url)
    qr.make(fit=True)

    png = f"pair_{room}.png"
    qr.make_image().save(png)
    print("\n========== 手机配对 ==========")
    print(f"房间码: {room}")
    print(f"二维码内容: {url}")
    print(f"已保存: {png}")
    try:
        qr.print_ascii(tty=True)
    except Exception:
        pass
    try:  # 尝试打开二维码图片 (必须非阻塞, 否则会被 snap firefox / xdg-open 卡住)
        import subprocess
        if sys.platform.startswith("win"):
            subprocess.Popen(["cmd", "/c", "start", "", png])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", png])
        else:
            subprocess.Popen(
                ["xdg-open", png],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception:
        pass
    print("================================")


# ---------------- 主流程 ----------------
async def run(room: str) -> None:
    peer_id = f"{PEER_PREFIX}-{room}"
    app = App()

    peer_online = threading.Event()  # PeerJS 数据连接是否在线 (决定是否忽略 MQTT 兜底)

    # 消息入口: 任何来源的任何 JSON 消息统一在这里路由
    def dispatch(msg, src: str) -> None:
        if not isinstance(msg, dict):
            return
        # PeerJS 在线时, MQTT 只做公告/兜底, 不重复处理数据, 避免鼠标双倍位移
        if src == "mqtt" and peer_online.is_set():
            return
        try:
            handle_message(msg)
        except Exception as e:
            print("消息处理异常:", e)

    # ---------- MQTT (HiveMQ 公开 broker) ----------
    mqttc = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"kuuki-{room.lower()}-{os.getpid()}",
    )
    mqttc.on_connect = lambda *_: print("MQTT 已连接 (HiveMQ 公开 broker)")
    mqttc.on_message = lambda _c, _u, m: dispatch_from_mqtt(m)
    mqtt_ready = threading.Event()

    def dispatch_from_mqtt(m):
        try:
            payload = m.payload
            # 兜底数据在 data topic 下是 JSON
            try:
                msg = json.loads(payload)
                dispatch(msg, "mqtt")
            except Exception:
                pass
        except Exception:
            pass

    data_topic = f"{MQTT_ROOT}/{room}/data"
    try:
        mqttc.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        mqttc.loop_start()
        mqttc.subscribe(data_topic, qos=0)
        mqtt_ready.set()
    except Exception as e:
        print("MQTT 连接失败(不影响 PeerJS 配对):", e)

    # MQTT 房间公告心跳 (my-node-app 同款: ROOM_TOPIC + "/" + roomCode)
    announce_topic = f"{MQTT_ROOT}/rooms/{room}"

    async def heartbeat():
        while True:
            try:
                if mqtt_ready.is_set():
                    mqttc.publish(
                        announce_topic,
                        json.dumps({"peerId": peer_id, "ts": int(time_sec())}),
                        qos=0, retain=False,
                    )
            except Exception:
                pass
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    # ---------- PeerJS 主机 (0.peerjs.com 公开 cloud broker) ----------
    # 注意: Python 移植版 PeerOptions.secure 默认 False, 但 cloud broker 只接受 wss(443),
    #       必须显式 secure=True (浏览器版会自动按页面 https 推断)。
    peer = Peer(peer_id, PeerOptions(secure=True))

    def on_conn(conn):
        peer_online.set()
        print(f"手机已配对并连接 (PeerJS {conn.peerId})")

        def on_data(data):
            if isinstance(data, (bytes, bytearray)):   # 二进制帧暂不支持(需 msgpack 解码), 忽略
                return
            dispatch(data, "peerjs")

        def on_close(*_):
            peer_online.clear()
            print("手机连接已断开, 等待重连...")

        conn.on(ConnectionEventType.Data, on_data)
        conn.on(ConnectionEventType.Close, on_close)

    peer.on(PeerEventType.Connection, on_conn)

    # ---------- 启动 ----------
    await peer.start()
    print(f"PeerJS 主机已注册: {peer_id} (公开 cloud broker 0.peerjs.com)")
    hb_task = asyncio.create_task(heartbeat())

    if not mqtt_ready.is_set():
        mqttc.loop_stop()

    try:
        await asyncio.Event().wait()   # 永久运行
    finally:
        hb_task.cancel()
        try:
            await peer.destroy()
        except Exception:
            pass
        if mqtt_ready.is_set():
            mqttc.loop_stop()
            mqttc.disconnect()


def time_sec() -> float:
    return datetime.datetime.now().timestamp()


def main() -> None:
    # 可选: python main.py <room> 指定房间码, 否则随机生成
    room = (sys.argv[1] if len(sys.argv) > 1 else None) or gen_room_code()
    # 生成二维码
    show_qr(f"{PAGE_URL}#/{room}", room)
    try:
        asyncio.run(run(room))
    except KeyboardInterrupt:
        print("\n已退出。")
    except Exception as e:
        print("运行出错:", e)
        raise


if __name__ == "__main__":
    main()