# Pages 部署要加 `web/.nojekyll`

GitHub Pages 默认会把上传目录过一遍 **Jekyll** 再发布。对这个仓库：

- 纯静态目录过一遍只是白白多一次构建（慢）
- **更要紧的是 Jekyll 会跳过下划线开头的目录** —— 以后加个 `_xxx/` 会在 Pages 上
  **静默消失**：本地好好的，上线才不见，而且不看网络面板查不出来

解法：`web/.nojekyll`（空文件），让 Pages 直接按静态文件托管。

验证方式：**`curl .../kuuki-mouse/.nojekyll` 返回 200**，说明文件真的被部署上去了
（.nojekyll 这种点文件容易被别的环节吞掉，所以要验）。
