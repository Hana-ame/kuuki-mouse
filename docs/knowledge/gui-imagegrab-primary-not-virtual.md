# ImageGrab.grab() 抓的是主显示器, 不是虚拟桌面

## 现象

多显示器上"看图点哪里"整体偏一整块屏: 视觉在图上算出目标坐标, 移过去却落在
旁边那块屏的对应位置; 或者 `region` 裁剪出来的根本不是想要的那一块。

单屏机器上从不出这个问题 —— 因为那时候两者是同一块屏。

## 根因

`PIL.ImageGrab.grab()` 无参数调用走的是 `SM_CXSCREEN` / `SM_CYSCREEN`, 那是
**主显示器的分辨率**; 要虚拟桌面必须显式 `all_screens=True` (Pillow 12 的 Windows
分支: `grabscreen_win32(..., all_screens, hwnd)`, `all_screens` 为假时只取主屏,
返回的 `offset` 也是 (0,0))。

于是有两套坐标:

- **虚拟桌面坐标**: 鼠标用的那一套 (`GetCursorPos` / `MONITORINFO` 的 rect),
  原点可以是**负的** (副屏排在主屏左边时)。
- **帧内坐标**: 截出来的那张图的像素坐标, 原点永远是该帧的左上角。

两者之间差的那个平移量就是 `Capture.origin`:

```
鼠标坐标 = 图坐标 + origin
```

只有"抓的是主屏、且主屏正好在虚拟桌面原点"时 `origin` 才是 (0,0)。主屏在右边、
副屏在左边的常见摆法里, 默认抓主屏时 `origin` 是 (1920, 0) 之类 —— 漏了它,
`region` 与画上去的光标都会偏 1920px。

## 怎么做

- 抓屏时说清抓哪一块: `monitor=<下标>` 抓指定屏、`all_screens=True` 抓虚拟桌面,
  都不给就是主屏 (与 `ImageGrab` 默认一致, 但**要把它当成一个显式选择**)。
- 每一帧都把 `origin` 一起发出去 (`Capture.origin`, WS 的 JSON 与 gRPC 的
  `Image.origin` 都有), 别让控制端去猜"这一帧是哪一块"。
- 所有**输入坐标也在这个坐标系里**: 光标位置 (`GetCursorPos`) 是虚拟桌面坐标,
  画到图上之前先减 `origin`; 判断"光标在不在这一帧里"也要减完再比。
- `region` 保持帧内坐标 (相对该帧左上角), 并在文档里写明 —— 它和 `origin`
  是两个不同层面的东西, 混起来算会双减。
- 下标 `0` 是合法值, 所以 `monitor` / `all_screens` 在 proto 里必须是 `optional`,
  翻译层用 `HasField` 判断"给没给"; `args.get("monitor") or None` 会把"抓第一块
  屏"悄悄变成"没给"。

## 测的时候

真机只有一块屏, 多屏场景只能打桩: 换掉 `remote.monitor.list_monitors` 造一个
"主屏在右、副屏在左"的布局, 再把 `ScreenCapture._grab_bbox` 换成"记下 bbox +
按 bbox 造一张图"。这样不碰真实显示器排列也能验出 `origin` 与光标落点。
