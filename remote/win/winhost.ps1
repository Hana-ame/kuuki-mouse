# kuuki-mouse · Windows 桥 (持久进程, 行式 JSON 协议)
#
# 为什么需要它
# ------------
# 在 WSL 里, pynput 只能操作 WSLg 的那个 X 屏幕 —— 实测那个屏幕上**没有任何窗口管理器**,
# 所有 X 程序 (Tk / xeyes / xclock / xmessage) 都能连上但一个都不 map, 截屏永远是全黑
# (5.5KB 恒定的空白 PNG)。也就是说: 想真正"操作这台电脑 / 看这台电脑的屏幕",
# 截图与输入都必须走 Windows 侧。
#
# 所以这里用 PowerShell + user32 P/Invoke 做一个常驻进程:
#   - 截屏: System.Drawing CopyFromScreen (整个虚拟桌面)
#   - 鼠标: SetCursorPos / mouse_event
#   - 键盘: SendInput + KEYEVENTF_UNICODE (**中文/emoji 也能直接打**)
#
# 协议: 每行一个 JSON 请求, 每行一个 JSON 响应。
#   请求  {"id":1,"op":"move","x":100,"y":200}
#   响应  {"id":1,"ok":true,"result":{...}}
# 常驻是为了省掉每次 ~300-800ms 的 PowerShell 启动 + Add-Type 编译开销。
#
# 单独调试:  powershell.exe -NoProfile -File winhost.ps1   然后手动敲一行 JSON

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
# 必须同时设 InputEncoding: Python 侧按 UTF-8 发中文, PowerShell 5.1 默认按本地代码页
# 解 stdin, 会把中文读成乱码 (实测表现为 type 调用直接卡死)。
[Console]::InputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class KuukiWin {
    [StructLayout(LayoutKind.Sequential)]
    public struct POINT { public int X; public int Y; }

    [StructLayout(LayoutKind.Sequential)]
    public struct MOUSEINPUT {
        public int dx; public int dy; public uint mouseData;
        public uint dwFlags; public uint time; public IntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct KEYBDINPUT {
        public ushort wVk; public ushort wScan;
        public uint dwFlags; public uint time; public IntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct HARDWAREINPUT {
        public uint uMsg; public ushort wParamL; public ushort wParamH;
    }

    [StructLayout(LayoutKind.Explicit)]
    public struct INPUTUNION {
        [FieldOffset(0)] public MOUSEINPUT mi;
        [FieldOffset(0)] public KEYBDINPUT ki;
        [FieldOffset(0)] public HARDWAREINPUT hi;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct INPUT { public uint type; public INPUTUNION u; }

    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, int dx, int dy, uint data, UIntPtr extra);
    [DllImport("user32.dll")] public static extern int GetSystemMetrics(int i);
    [DllImport("user32.dll")] public static extern uint SendInput(uint n, INPUT[] inputs, int size);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(POINT p);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, System.Text.StringBuilder s, int n);

    public const uint MOUSEEVENTF_LEFTDOWN = 0x0002, MOUSEEVENTF_LEFTUP = 0x0004;
    public const uint MOUSEEVENTF_RIGHTDOWN = 0x0008, MOUSEEVENTF_RIGHTUP = 0x0010;
    public const uint MOUSEEVENTF_MIDDLEDOWN = 0x0020, MOUSEEVENTF_MIDDLEUP = 0x0040;
    public const uint MOUSEEVENTF_WHEEL = 0x0800, MOUSEEVENTF_HWHEEL = 0x01000;
    public const uint INPUT_KEYBOARD = 1;
    public const uint KEYEVENTF_KEYUP = 0x0002, KEYEVENTF_UNICODE = 0x0004;

    // holdMs: 按下与抬起之间的间隔。
    // 不能用 0 —— 瞬时 down+up 会被前端框架当成无效点击 (只触发 hover 不触发 click),
    // 实测 DSH 的发送按钮就是这样: 坐标正确、调用成功, 但按钮毫无反应。
    public static void Mouse(uint down, uint up, int clicks, int holdMs) {
        for (int i = 0; i < clicks; i++) {
            mouse_event(down, 0, 0, 0, UIntPtr.Zero);
            if (holdMs > 0) System.Threading.Thread.Sleep(holdMs);
            mouse_event(up, 0, 0, 0, UIntPtr.Zero);
            if (clicks > 1) System.Threading.Thread.Sleep(60);  // 连击间隔, 避免被判双击
        }
    }

    public static void Wheel(int delta) {
        mouse_event(MOUSEEVENTF_WHEEL, 0, 0, (uint)delta, UIntPtr.Zero);
    }

    public static void WheelH(int delta) {
        mouse_event(MOUSEEVENTF_HWHEEL, 0, 0, (uint)delta, UIntPtr.Zero);
    }

    // 一个 UTF-16 code unit 的按下+抬起 (KEYEVENTF_UNICODE 可打中文/emoji)
    static void UniChar(ushort ch, bool up) {
        INPUT[] a = new INPUT[1];
        a[0].type = INPUT_KEYBOARD;
        a[0].u.ki.wVk = 0;
        a[0].u.ki.wScan = ch;
        a[0].u.ki.dwFlags = KEYEVENTF_UNICODE | (up ? KEYEVENTF_KEYUP : 0);
        a[0].u.ki.time = 0;
        a[0].u.ki.dwExtraInfo = IntPtr.Zero;
        SendInput(1, a, Marshal.SizeOf(typeof(INPUT)));
    }

    public static void TypeUnicode(string s) {
        foreach (char c in s) {
            UniChar((ushort)c, false);
            UniChar((ushort)c, true);
        }
    }

    public static void KeyVk(ushort vk, bool up) {
        INPUT[] a = new INPUT[1];
        a[0].type = INPUT_KEYBOARD;
        a[0].u.ki.wVk = vk;
        a[0].u.ki.wScan = 0;
        a[0].u.ki.dwFlags = up ? KEYEVENTF_KEYUP : 0;
        a[0].u.ki.time = 0;
        a[0].u.ki.dwExtraInfo = IntPtr.Zero;
        SendInput(1, a, Marshal.SizeOf(typeof(INPUT)));
    }

    public static string ForegroundTitle() {
        IntPtr h = GetForegroundWindow();
        System.Text.StringBuilder sb = new System.Text.StringBuilder(512);
        GetWindowTextW(h, sb, sb.Capacity);
        return sb.ToString();
    }
}
'@

[void][KuukiWin]::SetProcessDPIAware()

# 键名 -> 虚拟键码
$VK = @{
    'backspace' = 0x08; 'tab' = 0x09; 'enter' = 0x0D; 'return' = 0x0D; 'shift' = 0x10
    'ctrl' = 0x11; 'control' = 0x11; 'alt' = 0x12; 'pause' = 0x13; 'caps_lock' = 0x14
    'esc' = 0x1B; 'escape' = 0x1B; 'space' = 0x20; 'page_up' = 0x21; 'page_down' = 0x22
    'end' = 0x23; 'home' = 0x24; 'left' = 0x25; 'up' = 0x26; 'right' = 0x27; 'down' = 0x28
    'print_screen' = 0x2C; 'insert' = 0x2D; 'delete' = 0x2E; 'win' = 0x5B; 'cmd' = 0x5B
    'num_lock' = 0x90; 'scroll_lock' = 0x91
    'f1' = 0x70; 'f2' = 0x71; 'f3' = 0x72; 'f4' = 0x73; 'f5' = 0x74; 'f6' = 0x75
    'f7' = 0x76; 'f8' = 0x77; 'f9' = 0x78; 'f10' = 0x79; 'f11' = 0x7A; 'f12' = 0x7B
    'f13' = 0x7C; 'f14' = 0x7D; 'f15' = 0x7E; 'f16' = 0x7F; 'f17' = 0x80; 'f18' = 0x81
    'f19' = 0x82; 'f20' = 0x83; 'f21' = 0x84; 'f22' = 0x85; 'f23' = 0x86; 'f24' = 0x87
}

# 字母 / 数字: 单字符键直接按字符码当虚拟键码 (VK_A=0x41 .. VK_Z=0x5A, VK_0=0x30 ..)
foreach ($c in [char[]]'abcdefghijklmnopqrstuvwxyz') { $VK[[string]$c] = [int][char]::ToUpper($c) }
foreach ($c in [char[]]'0123456789') { $VK[[string]$c] = [int]$c }

function Resolve-Vk([string]$name) {
    $k = $name.ToLower().Trim()
    if ($VK.ContainsKey($k)) { return [uint16]$VK[$k] }
    # 兜底: 任何单字符都按大写字符码发 (覆盖符号键如 - = [ ] ; ' , . / \ `)
    if ($k.Length -eq 1) { return [uint16][char]::ToUpper($k[0]) }
    throw "unknown key: $name"
}

function Send-Text([string]$text) { [KuukiWin]::TypeUnicode($text) }

function Send-Key([string]$name, [string]$action) {
    $vk = Resolve-Vk $name
    switch ($action) {
        'press'   { [KuukiWin]::KeyVk($vk, $false) }
        'release' { [KuukiWin]::KeyVk($vk, $true) }
        default   { [KuukiWin]::KeyVk($vk, $false); [KuukiWin]::KeyVk($vk, $true) }
    }
}

function Send-Hotkey([string[]]$keys) {
    $vks = @()
    foreach ($k in $keys) { $vks += (Resolve-Vk $k) }
    foreach ($vk in $vks) { [KuukiWin]::KeyVk($vk, $false) }
    [array]::Reverse($vks)
    foreach ($vk in $vks) { [KuukiWin]::KeyVk($vk, $true) }
}

function Get-Shot([int]$left, [int]$top, [int]$width, [int]$height) {
    if ($width -le 0 -or $height -le 0) {
        $vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
        $left = $vs.Left; $top = $vs.Top; $width = $vs.Width; $height = $vs.Height
    }
    $bmp = New-Object System.Drawing.Bitmap($width, $height)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($left, $top, 0, 0, $bmp.Size)
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $bytes = $ms.ToArray()
    $g.Dispose(); $bmp.Dispose(); $ms.Dispose()
    return , @{ b64 = [Convert]::ToBase64String($bytes); width = $width; height = $height; left = $left; top = $top }
}

function Invoke-Op($req) {
    $op = [string]$req.op
    switch ($op) {
        'ping' { return @{ pong = $true; host = 'windows' } }
        'info' {
            $vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
            return @{
                width = $vs.Width; height = $vs.Height; left = $vs.Left; top = $vs.Top
                dpi_aware = $true
                foreground = [KuukiWin]::ForegroundTitle()
                powershell = $PSVersionTable.PSVersion.ToString()
            }
        }
        'screenshot' {
            $l = if ($null -ne $req.left) { [int]$req.left } else { 0 }
            $t = if ($null -ne $req.top) { [int]$req.top } else { 0 }
            $w = if ($null -ne $req.width) { [int]$req.width } else { 0 }
            $h = if ($null -ne $req.height) { [int]$req.height } else { 0 }
            return Get-Shot $l $t $w $h
        }
        'position' {
            $p = New-Object KuukiWin+POINT
            [void][KuukiWin]::GetCursorPos([ref]$p)
            return @{ x = $p.X; y = $p.Y }
        }
        'move' { [void][KuukiWin]::SetCursorPos([int]$req.x, [int]$req.y); return @{ x = [int]$req.x; y = [int]$req.y } }
        'click' {
            $btn = if ($req.button) { [string]$req.button } else { 'left' }
            $n = if ($req.clicks) { [int]$req.clicks } else { 1 }
            # hold 默认 60ms: 给前端框架留出配对 down/up 的时间窗 (0 会点不动按钮)
            $hold = if ($null -ne $req.hold) { [int]$req.hold } else { 60 }
            switch ($btn) {
                'right'  { [KuukiWin]::Mouse([KuukiWin]::MOUSEEVENTF_RIGHTDOWN, [KuukiWin]::MOUSEEVENTF_RIGHTUP, $n, $hold) }
                'middle' { [KuukiWin]::Mouse([KuukiWin]::MOUSEEVENTF_MIDDLEDOWN, [KuukiWin]::MOUSEEVENTF_MIDDLEUP, $n, $hold) }
                default  { [KuukiWin]::Mouse([KuukiWin]::MOUSEEVENTF_LEFTDOWN, [KuukiWin]::MOUSEEVENTF_LEFTUP, $n, $hold) }
            }
            return @{ button = $btn; clicks = $n; hold = $hold }
        }
        'down' {
            $btn = if ($req.button) { [string]$req.button } else { 'left' }
            if ($btn -eq 'right') { [KuukiWin]::mouse_event([KuukiWin]::MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, [UIntPtr]::Zero) }
            elseif ($btn -eq 'middle') { [KuukiWin]::mouse_event([KuukiWin]::MOUSEEVENTF_MIDDLEDOWN, 0, 0, 0, [UIntPtr]::Zero) }
            else { [KuukiWin]::mouse_event([KuukiWin]::MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero) }
            return @{ state = 'down'; button = $btn }
        }
        'up' {
            $btn = if ($req.button) { [string]$req.button } else { 'left' }
            if ($btn -eq 'right') { [KuukiWin]::mouse_event([KuukiWin]::MOUSEEVENTF_RIGHTUP, 0, 0, 0, [UIntPtr]::Zero) }
            elseif ($btn -eq 'middle') { [KuukiWin]::mouse_event([KuukiWin]::MOUSEEVENTF_MIDDLEUP, 0, 0, 0, [UIntPtr]::Zero) }
            else { [KuukiWin]::mouse_event([KuukiWin]::MOUSEEVENTF_LEFTUP, 0, 0, 0, [UIntPtr]::Zero) }
            return @{ state = 'up'; button = $btn }
        }
        'scroll' {
            $dx = if ($null -ne $req.dx) { [int]$req.dx } else { 0 }
            $dy = if ($null -ne $req.dy) { [int]$req.dy } else { 0 }
            if ($dy -ne 0) { [KuukiWin]::Wheel($dy * 120) }
            if ($dx -ne 0) { [KuukiWin]::WheelH($dx * 120) }
            return @{ dx = $dx; dy = $dy }
        }
        'type' {
            # 文本按 base64(UTF-8) 传进来 —— 绕开 stdin/stdout 的代码页问题。
            # 再用剪贴板 + Ctrl+V 注入: 实测逐字符 UNICODE SendInput 在 ~30 字以上
            # 会把 PowerShell 侧卡死 (10s 超时都等不回来), 剪贴板只需一次热键。
            if ($req.b64) {
                $bytes = [Convert]::FromBase64String([string]$req.b64)
                $text = [System.Text.Encoding]::UTF8.GetString($bytes)
            } else {
                $text = [string]$req.text
            }
            if ($text.Length -eq 0) { return @{ chars = 0; method = 'noop' } }
            Set-Clipboard -Value $text
            Start-Sleep -Milliseconds 80
            Send-Hotkey @('ctrl', 'v')
            Start-Sleep -Milliseconds 100
            return @{ chars = $text.Length; method = 'clipboard' }
        }
        'key' {
            $act = if ($req.action) { [string]$req.action } else { 'tap' }
            Send-Key ([string]$req.key) $act
            return @{ key = [string]$req.key; action = $act }
        }
        'hotkey' { Send-Hotkey ([string[]]$req.keys); return @{ keys = $req.keys } }
        default { throw "unknown op: $op" }
    }
}

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    if ($line.Trim() -eq '') { continue }
    $id = $null
    try {
        $req = $line | ConvertFrom-Json
        $id = $req.id
        $result = Invoke-Op $req
        $resp = @{ id = $id; ok = $true; result = $result }
    } catch {
        $resp = @{ id = $id; ok = $false; error = @{ code = 'windows_error'; message = $_.Exception.Message } }
    }
    [Console]::Out.WriteLine(($resp | ConvertTo-Json -Compress -Depth 6))
    [Console]::Out.Flush()
}
