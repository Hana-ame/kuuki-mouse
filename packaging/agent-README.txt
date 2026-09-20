kuuki-agent —— kuuki-mouse 受控端 (装在"被操作的那台机器"上)

用法:
  双击 kuuki-agent\kuuki-agent.exe
  命令行看全部参数: kuuki-agent\kuuki-agent.exe --help

启动后默认同时开三个通道:
  WebSocket  ws://127.0.0.1:8765
  gRPC       127.0.0.1:50051
  PeerJS     注册到公开 broker, 启动时会给出房间码 (手机扫码配对用)

常用参数:
  --no-peerjs                    不开 PeerJS, 只留本机两个端口
  --token <口令>                 要求客户端带口令 (跨机 / 公网务必加)
  --allow-remote --token <口令>  绑 0.0.0.0 (必须同时带 --token)
  --qr                          额外打印配对二维码
  --selftest                    自检: 报告环境 + 抓一帧, 不动鼠标

安全:
  默认只绑 127.0.0.1。但 PeerJS 是本机连出去注册到公开 broker 的,
  别人拿到房间码就能连进来 —— 不可信网络下一定要加 --token。
  房间码只是配对用的, 不是凭证。

控制端不在这个包里 (它在开发环境跑源码):
  python -m remote.client ws ping
  python -m remote.ctl ping --all

详见仓库 docs/ 与 remote/README.md。
