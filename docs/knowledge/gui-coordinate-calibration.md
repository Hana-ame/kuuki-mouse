# calibrate: 别相信缩放系数, 把它测出来

## 现象

`locate` 报出目标在图上的位置之后, 要乘一个系数才能得到能喂给 `mouse.click` 的
屏幕坐标。这个系数来自帧头 (`source_width` / 帧宽), 而它有两个不信的地方:

1. **它可能是错的** —— WS 的二进制帧头曾经漏写 `source_width`, 系数恒为 1.0,
   定位出来的坐标整体偏掉一个缩放比 (见 `env-ws-frame-missing-source-width.md`);
2. **就算它是对的, 也不够** —— 乘算只能表达缩放, 表达不了平移。多显示器上虚拟
   桌面向左向下扩展, 图的 (0, 0) 可能是鼠标的 (-1920, 0), 每个点都该减去它, 而
   帧头里根本没有这一项。

第 2 条尤其阴: 每一个算出来的数字看着都很合理 (毕竟只是差一个常数), 只有把鼠标
真的移过去才会发现点空了。

## 办法: 闭环实测

`remote/calibrate.py` (纯 Pillow, 零新依赖) 不猜, 直接测。用的全是仓库里已有的
东西 —— `mouse.move` / `screen.capture(draw_cursor=True)` / `vision.diff` /
`vision.find_color`:

```
鼠标移到已知座标点 → 抓一帧把光标画上去 → 帧差找出"这一屏哪儿变了"
  → 在变化区里认出那个红十字 → 拿一排点对拟合 screen = a × frame + b
```

九个默认靶点按 3×3 网格铺开。`fit()` 是两个轴各自的最小二乘直线拟合,
`Calibration.to_screen()` / `to_frame()` 提供双向换算。

## 三个踩出来的坑 (都已堵在模块里)

### 1. 前后帧不能都画光标

最初两帧都带 `draw_cursor`, 于是帧差里有**两个**同样合法的红十字 —— 旧位置一个、
新位置一个, 认哪个全凭运气。改成前帧不画、只后帧画, 变化区域才唯一。

这条是设计问题不是实现 bug: 帧差的前提是"除了我们引入的变化, 别的东西都没动",
而自己在两帧里各动一下就破坏了这个前提。

### 2. 认标记必然会有错认, 所以拟合必须稳健

屏幕上动的不止光标: 动画、进度指示器、视频里的红色 logo —— 尺寸凑巧接近就会混
进来。本机实测一度 9 个点里错 4 个, **直接拟合给出的结果看起来完整实则全错**
(系数差 19%, 残差 426px)。

所以走 `fit_robust()`: 从所有点对里挑三个拟合, 统计有多少点支持这个模型
(RANSAC 式的三点投票), 用票数最多的那组**全部内点**重拟合一次, 剩下的点连同误差
放进 `outliers` 让调用方复查。**票数凑不够多数就直接报错** —— 三点总能算出一条
直线, 那不叫标定。

配套地, `verdict` 不是"命令跑成功了":

| verdict | 含义 | 有没有 `calibration` |
|---|---|---|
| `aligned` | 残差在容差内 | 有 |
| `drifted` | 拟合出来了但残差超容差 —— 缩放和平移对不上, 别信 | 有, 但别用 |
| `too_few_samples` | 认出的标记不够拟合 | **没有** (宁可让你重跑, 也不给假的) |
| `inconsistent` | 点之间互相不一致到凑不出多数票 | **没有** |

### 3. 贴边的标记会被裁一半

光标那个十字的手臂是 `_ARM = 12` 像素, 画在**缩放之后**的图上。所以贴屏幕边缘的
靶点上, 十字有两笔落在图外, 找到的重心会往里偏。`plan_points()` 的 `min_margin`
为此按像素算: 所需的屏幕留边 = `(arm + 余量) / 缩放系数`, 取 `margin` 比例与它的
较大者。

顺带说一句这种"两边必须一致"的值: 标记的颜色与手臂长度提到了
`screen.CURSOR_COLOR` / `screen.CURSOR_ARM` 两个模块级常量, calibrate 引用它们而
不是各写一份 —— 否则改了一边的颜色会表现为"忽然认不出标记", 而根因在另一个文件里。

## 三传输的对齐

`screen.calibrate` 在 WS / PeerJS 上是透传, gRPC 走 `Calibrate` RPC。它的 proto
reply (`CalibrateReply`) 形状刻意做得与 service 返回的 dict 逐字段一致, 就是为了
能被 `test_new_ops_agree_across_transports` 直接比对 —— 这条用例抓出来的问题:

> proto3 普通标量分不清"显式给 0"与"缺省", 而 calibrate 的**每个 0 都表示"用服务端
> 默认值"** (`cols=0` `settle=0` `tolerance=0` `max_width=0` 全是这个意思)。gRPC 侧
> 必须**非零才写进 args**, 否则同一条命令两条传输跑出不同结果。见
> `protocol-proto3-optional.md`。

## 用法与副作用

```bash
python -m remote.client ws calibrate --max-width 1280 --save /tmp/calib.json
python -m remote.client ws locate --saturated --calib /tmp/calib.json
```

**它会动鼠标**, 这是副作用, 所以默认跑完把光标挪回原位 (`restore=true`,
报告里 `restored` 告诉你到底挪没挪回去)。正在用这台机器的时候别跑。

本机实测 (1680×1050 单屏, `--max-width 1280`): 9/9 全中, `declared_scale` 1.3125
对 `fit_scale` 1.3114, 残差 max 0.44px, `origin ≈ (0.5, -0.7)` —— 单屏本来就没有
平移, `origin` 这一项要在多显示器上才能体现价值。
