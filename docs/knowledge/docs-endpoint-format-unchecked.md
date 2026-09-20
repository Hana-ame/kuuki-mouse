# 文档命令：parser 通过 ≠ 运行期能跑

README 里曾经写过：

```bash
python -m remote.ctl machines add pc1 --transport ws --endpoint 192.168.1.5:8765
```

`check_ctl_docs.py` **判它通过**（parser 眼里 endpoint 就是个字符串），但运行期
`validate_endpoint` 会拒绝 —— ws 的 endpoint 必须 `ws://` 开头。peerjs 传输则相反，
endpoint 是**裸 peer id**（`kuuki-mouse-ABCDE`），写成 `peerjs://...` 一样不对。

这条是新写 README 时抄错的（别的文档里本来就写对了），所以：

- 校验脚本补足的方向是「顺带验 endpoint 格式」，而不只是验 parser
- 写文档时涉及**传输 + endpoint** 的组合，去 `docs/ctl-selfhost-runbook.md` 抄现成的，
  别手打
