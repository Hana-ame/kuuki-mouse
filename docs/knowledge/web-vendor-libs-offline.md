# 第三方库自托管到 `web/vendor/`，别用 CDN

手机页面原本从 unpkg 拉 peerjs / mqtt —— **国内连不上就是白屏**，而且
`<script src>` 是阻塞渲染的，页面卡住不动却不报错，看 network 才知道。

解法：自托管到 `web/vendor/`（约 422KB）。

## 加 `defer` 要连带检查

mqtt 加 `defer` 之后，**`script.js` 也必须跟着 `defer`** —— 否则 script.js 先执行、
mqtt 还没定义，兜底通道（MQTT 降级）就没了，而你会以为是 broker 的问题。

改动 `<script>` 标签时，把**所有** script 一起看，别只改新增的那个。
