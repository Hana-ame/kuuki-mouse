# 拖拽路径全程只按一次、只松一次

`mouse.drag` 支持 `points` / `path` 走多段路径。

**中途松开会把一次拖拽拆成多次独立拖拽** —— 前台应用（画图、选区间、文件拖放）
看到的是 N 次 press/release，而不是一次连续拖拽，功能就废了。

正确实现：起点 press → 依次 move 到每个中间点 → 终点 release。中间无论多少点，
`press` 和 `release` 各一次。
