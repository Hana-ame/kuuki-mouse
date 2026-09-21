kuuki-agent —— kuuki-mouse 受控端 (装在"被操作的那台机器"上)

两种装法:

  1) 安装包 (推荐)  双击 kuuki-agent-setup.exe -> 下一步 -> 完成
     装到 %LOCALAPPDATA%\Programs\kuuki-agent, 开始菜单里有入口,
     "设置 -> 应用" 里能卸干净。**不需要管理员权限** (不弹 UAC)。
     装完默认不开开机自启 —— 需要的话在安装时勾一下, 或自己把快捷方式
     放进 shell:startup。

  2) 便携 zip      解压后双击 kuuki-agent\kuuki-agent.exe
     什么都不写进系统, 删掉目录就没了。

启动与参数 (两种装法都一样):
  双击 kuuki-agent.exe
  命令行看全部参数: kuuki-agent.exe --help

启动后默认只开一个通道:
  PeerJS     注册到公开 broker, 启动时会给出房间码 (手机扫码配对用),
             不需要本机端口、不需要防火墙放行

要用本机端口就自己开 (可以一次开多个, 给了哪个就开哪个):
  --ws            WebSocket  ws://127.0.0.1:8765
  --grpc          gRPC       127.0.0.1:50051
  --ws --grpc     两个本机端口都要
  --ws --peerjs   WebSocket + PeerJS

常用参数:
  --ws / --grpc / --peerjs        开启对应通道 (默认只有 PeerJS)
  --ws-port <端口>                换 WebSocket 端口 (要配 --ws)
  --grpc-port <端口>              换 gRPC 端口 (要配 --grpc)
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
