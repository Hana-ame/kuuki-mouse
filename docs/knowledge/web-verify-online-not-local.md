# 改完前端要在线上验，不能只看本地

配对前端发布在 GitHub Pages（`pages.yml` 部署 `web/`）：

```
https://hana-ame.github.io/kuuki-mouse/#/<房间码>?token=<口令>
```

Python 端 `guess_page_url()` 从 **git remote 推断**出同一地址（不是硬编码 ——
fork 之后地址会变），拼上 hash 路由后 HTTP 200。

**验证流程**（改完前端必做）：

```bash
sleep 75                     # 等 workflow(~17s) + CDN 生效
curl https://hana-ame.github.io/kuuki-mouse/          | grep -c pairHint
curl https://hana-ame.github.io/kuuki-mouse/script.js | grep -c pairHint
curl https://hana-ame.github.io/kuuki-mouse/style.css | grep -c ".hint"
curl -o /dev/null -w "%{http_code}" https://hana-ame.github.io/kuuki-mouse/.nojekyll
```

在新出现的关键字上比对 —— 「bresponder 返回 200」只能说明站点还在，说明不了新代码
上线了。**本地跑通和线上生效是两件事。**
