; kuuki-agent 安装脚本 (Inno Setup 6)
;
; 为什么要有这个 —— 之前的分发物是一个 zip:
;     下载 -> 解压 -> 进目录 -> 双击 exe
; 对"被操作的那台机器"的使用者来说, 前三步每一步都会丢人。安装器把它们收成
; "双击 -> 下一步 -> 完成", 并顺带给出卸载入口 (控制面板 / 设置里能卸干净)。
;
; 三条定死的设计:
;
; 1. **不需要管理员权限** (PrivilegesRequired=lowest, 装到 %LOCALAPPDATA%\Programs)。
;    UAC 弹窗是"一键安装"最大的绊脚石, 而受控端没有必须以系统级运行的理由 ——
;    它只服务当前登录的这个用户。
;
; 2. **不碰防火墙**。受控端默认只绑 127.0.0.1, 而 PeerJS 是本机连出去注册到
;    公开 broker 的 (出方向), 两种都不需要入站放行。只有 --allow-remote 才需要,
;    而那是"我明确要暴露到局域网" —— 有意暴露就该由人手动放行, 不该被安装器
;    偷偷做成默认。这条也写在安装完成页上。
;
; 3. **开机自启默认不开**。能远程控制这台机器的东西, 自启是一个要人点头的选择,
;    不是默认值。
;
; 编译 (CI 里跑的):
;   ISCC.exe packaging\installer.iss "/DMyAppVersion=v1.2.3"
; 本地复现需要装 Inno Setup 6, 且本仓库要先 pyinstaller 出 dist/kuuki-agent。
;
; 注意: 这个文件是 UTF-8 **带 BOM** 的 —— Inno Setup 6 靠 BOM 认 UTF-8,
; 没有 BOM 会按 ANSI 读, 界面上的中文就糊了。改它时别把 BOM 弄丢。

#define MyAppName "kuuki-agent"
#define MyAppPublisher "kuuki-mouse"
#define MyAppURL "https://github.com/Hana-ame/kuuki-mouse"
#define MyAppExeName "kuuki-agent.exe"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif

[Setup]
; AppId 必须固定: 它是"已安装"的唯一身份, 每次编译变一次的话, 装三遍就是三个
; 互相看不见的实例, 卸载也卸不干净。
AppId={{4565562D-BB9E-4B03-8D73-20E5B054D0B9}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=
; lowest = 按当前用户权限装, 不触发 UAC
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..
OutputBaseFilename=kuuki-agent-setup
SetupIconFile=
Compression=lzma
SolidCompression=yes
WizardStyle=modern
WizardResizable=no
UninstallDisplayName={#MyAppName} {#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}
Uninstallable=yes
; 同一个 exe 已经在跑时, 先让它自己退出, 别让用户在"文件占用"上卡住
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no

[Languages]
; 只声明 english (它的 Default.isl 每个 Inno 发行版都有)。下面的界面文案与
; 说明文字仍然写中文 —— 那是我们的字符串, 不依赖语言包。
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "在桌面上放一个快捷方式"; GroupDescription: "附加图标:"; Flags: unchecked
Name: "startup"; Description: "登录后自动启动受控端"; GroupDescription: "其他任务:"; Flags: unchecked

[Files]
Source: "..\dist\kuuki-agent\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\kuuki-agent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "agent-README.txt"; DestDir: "{app}"; DestName: "README.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\kuuki-agent"; Filename: "{app}\{#MyAppExeName}"; Comment: "启动受控端"
Name: "{group}\使用说明"; Filename: "{app}\README.txt"
Name: "{group}\卸载 kuuki-agent"; Filename: "{uninstallexe}"
Name: "{autodesktop}\kuuki-agent"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; 开机自启只写当前用户 (HKCU), 与"不需要管理员"保持一致
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "{#MyAppName}"; \
    ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "现在就启动受控端"; \
    Flags: nowait postinstall skipifsilent unchecked

[Messages]
; 完成页补一句"默认没开端口、要暴露要自己放行": 拿到这台机器控制权的是对端,
; 使用者得知道边界在哪, 而不是装完以为它没联网。
FinishedHeadingLabel=安装完成
FinishedLabelNoIcons=kuuki-agent 已装好, 开始菜单里可以启动它。%n%n它默认只监听本机 (127.0.0.1), 不会开对外端口; 要让局域网里的控制端连进来, 得手动带 --allow-remote --token <口令> 启动, 并自己在防火墙里放行端口。%n%nPeerJS 通道是本机连出去注册到公开 broker 的, 不需要防火墙放行。
ClickFinish=完成
