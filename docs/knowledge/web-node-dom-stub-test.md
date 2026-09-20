# 用 node + 最小 DOM 桩测试真的 `web/script.js`

`web/` 曾经零测试覆盖。做法是**拿 node 直接 eval 真的 script.js**，配一个最小 DOM 桩：

```js
// 在 IIFE 结尾注入, 劫持发送
send = msg => __sent.push(msg);
```

然后断言 `__sent` 的内容。覆盖了：键盘 22 项（大小写 / 组合键 / Fn 层 / 键名都能在
`controller.special_keys` 里查到）、token 握手、vendor 文件在磁盘上存在、
**元素 id 两边对齐**（script.js 里 `$('xxx')` 的 id 必须在 index.html 里存在）。

最后那条最值：HTML 和 JS 改名不同步时，页面会在运行时 `TypeError: null`，测不出来
就会被带到线上。

**注入失败要报错，不能静默通过** —— 桩没搭好却显示全绿，比没有测试更糟。
