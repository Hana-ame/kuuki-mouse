# -*- coding: utf-8 -*-
"""生成 docs/knowledge/README.md 索引。

按文件名前缀分组，标题取每个文件的第一行 H1 —— 这样以后增删知识点文件不用手动改索引。
"""
import pathlib

ROOT = pathlib.Path(r"D:\WorkPlace\kuuki-mouse\docs\knowledge")

GROUPS = [
    ("arch", "架构与产品定位", "改代码前该先知道的几条硬约束"),
    ("protocol", "协议与 op 实现", "同一条 op 经三条传输必须等价，这里的坑都是这么抓出来的"),
    ("transport", "传输开关与依赖", "三种传输按需开启之后的语义、校验与依赖分级"),
    ("net", "网络 / ICE / PeerJS", "WebRTC 相关的对策与被 fork 的性质逼出来的格式"),
    ("ctl", "控制端 ctl 与 registry", "多机编排：并发、原子写、权限、退出码"),
    ("ci", "打包与 CI", "英文 CI 上才暴露的问题占了一半"),
    ("docs", "文档维护", "让文档真的跟着代码走，而不是写一次就烂"),
    ("web", "Web 前端 / Pages", "手机配对页与 GitHub Pages 部署"),
    ("gui", "GUI 自动化实战", "用这套工具真实操控桌面的可行套路"),
    ("env", "本机环境", "Windows / Git Bash 这一侧反复踩的坑"),
    ("todo", "未修 / 未做清单", "已知未修的 bug 与未做的验证，都在这儿"),
]


def main():
    files = sorted(p for p in ROOT.glob("*.md") if p.name != "README.md")

    lines = [
        "# 知识点索引",
        "",
        "> 一个文件一个知识点，都是**踩过之后才记下来**的，不是设计文档的转写。",
        "> 每条都写了「当时是什么现象 / 为什么 / 该怎么避开」，按前缀分组，也可以直接用",
        "> `grep -rl <关键词> docs/knowledge/` 全文检索。",
        "",
    ]

    unknown = []
    total = 0
    for prefix, title, desc in GROUPS:
        members = [p for p in files if p.name.startswith(prefix + "-")]
        if not members:
            continue
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"*{desc}*")
        lines.append("")
        for p in members:
            head = p.read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip()
            lines.append(f"- [{head}]({p.name})")
            total += 1
        lines.append("")

    leftovers = [p for p in files
                 if not any(p.name.startswith(g[0] + "-") for g in GROUPS)]
    if leftovers or unknown:
        lines.append("## 其它")
        lines.append("")
        for p in leftovers:
            head = p.read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip()
            lines.append(f"- [{head}]({p.name})")
            total += 1
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"共 **{total} 条**。新增知识点文件时按上述前缀命名，索引会自动接住它。")

    # newline="\n" 不能省: 不写的话 Path.write_text() 走的是 text mode 默认值,
    # Windows 上会把每一个 \n 换成 \r\n, 生成的索引就成了混进仓库里的唯一一个
    # CRLF 文件 —— 其它知识条目是 LF, 两边对着 diff 时会平白多出一整摞改动。
    (ROOT / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"索引已生成，共 {total} 条")


if __name__ == "__main__":
    main()
