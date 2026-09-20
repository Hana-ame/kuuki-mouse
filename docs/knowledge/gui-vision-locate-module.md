# remote/vision: 让"看"也归属 repo 的定位模块

## 是什么

`remote/vision.py` + `python -m remote.client ws locate`。四条定位路线, 全部
纯 Pillow 实现 (没有 numpy/OpenCV, 零新增依赖), 输出统一带两组坐标:
`x/y` 是截图自身的展示坐标, `screen.x/screen.y` 是乘过缩放系数、能直接喂给
`mouse.*` 的真实屏幕坐标:

| 模式 | 命令 | 用途 |
|---|---|---|
| describe | `locate --describe` | 网格概览: 每块主色 + 内容量, 读不了图的调用方的"眼睛" |
| color | `locate --color '#1a73e8'` | 找已知颜色的按钮/描边/图标 |
| saturated | `locate --saturated` | 找任何彩色图标 (favicon 排、实心按钮) |
| template | `locate --template icon.png` | 形状匹配, 模板用 `--save-template --box` 自举 |
| diff | `locate --diff-with prev.png` | 找变化区域, 验证"上一步生效了吗" |

## 实测效果 (2026-09-20)

- 1000x625 帧 + 40x60 模板的 match_template 约 0.7-5s (降采样粗筛 + 精匹配两级);
- describe 8x5 = 40 块能区分"浏览器内容区 / 桌面 / 深色编辑器";
- saturated 模式在 1680x90 的标签栏窄带上, 一次找出全部 10 个 favicon,
  误差 0px —— 顺带把浏览器自己的大块 UI 图标按尺寸过滤掉了。

## 踩过的实现坑

1. **find_color 的面积估算**: 命中点数是"网格数", 乘 `step²` 才是真实像素,
   乘 `cell²` 会把 12x12 的小方块算成 144 像素, min_pixels 形同虚设。
2. **match_template 的两份模板字节**: 粗匹配用降采样模板, 精匹配必须用原尺寸
   模板的 tobytes; 混用直接 IndexError。
3. **scale 必须由 source_width 算** (见 `env-ws-frame-missing-source-width.md`),
   vision.scale_of 拿不到 source_width 时返回 1.0 而不是猜。
4. 合成图测试就能覆盖全部逻辑: 画已知坐标的色块, 断言找回来 —— 不需要真机。

## 语义边界 (为什么它不能单独完成任务)

视觉只能回答"这里有个像 X 的东西", 回答不了"这是不是目标应用" —— 见
`gui-window-focus-gap.md`。视觉定位 + 窗口管理(缺失) + OCR(缺失) 才是完整拼图。
