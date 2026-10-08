#!/usr/bin/env python3
"""Rebuild the merged reading copies (Markdown + offline HTML) from the package files.

Requires the `markdown` package (tables, fenced_code). The merged copies include
`local-only/`, so they are PRIVATE reading copies and must never be published.
Usage: uv run --with markdown python tools/build_merged_docs.py
"""
from __future__ import annotations
import html, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT.parent
VERSION = "v1.1"
DATE = "2026-10-07"
ORDER = [
    "00_START_HERE.md",
    *[f"docs/{n}" for n in sorted(p.name for p in (ROOT / "docs").glob("*.md"))],
    "local-only/LAN_PLAN.md",
    "local-only/LOCAL_SOURCES.md",
    "local-only/SERVER_INVENTORY.md",
    "AGENTS.md",
    "CODEX_START_PROMPT.md",
    "contracts/README.md",
    "deploy/README.md",
    "tools/README.md",
]

def title_of(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    raise ValueError("no H1")

docs = []
for i, rel in enumerate(ORDER):
    text = (ROOT / rel).read_text(encoding="utf-8").rstrip() + "\n"
    docs.append({"id": f"doc-{i:02d}", "rel": rel, "title": title_of(text), "text": text})
anchor_by_rel = {d["rel"]: d["id"] for d in docs}

def rewrite_links(text: str, rel: str) -> str:
    base = (ROOT / rel).parent
    def repl(m):
        label, dest = m.group(1), m.group(2)
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", dest) or dest.startswith("#"):
            return m.group(0)
        target = (base / dest.split("#")[0]).resolve()
        try:
            key = target.relative_to(ROOT).as_posix()
        except ValueError:
            return m.group(0)
        if key in anchor_by_rel:
            return f"[{label}](#{anchor_by_rel[key]})"
        return f"[{label}](`{key}`)" if False else f"{label}（`{key}`）"
    return re.sub(r"(?<!!)\[([^\]]*)\]\(([^\s)]+)(?:\s+\"[^\"]*\")?\)", repl, text)

header = f"""# NexLoop｜完整 PRD 与 Codex 开发交接文档

**版本：{VERSION}　｜　决策日期：{DATE}　｜　状态：开发规格，尚未实施部署**

本文件合并产品需求、架构、数据、对话到本体、上下文、运行可靠性、上游整合、接口、工作台、双机部署、本地CI、开源、安全、验收、开发顺序和运维。机器契约、任务清单、示例与部署模板在配套ZIP的独立文件中。

**此合并版本包含用户提供的私有LAN部署附件与本机源码路径。用于Codex与维护者交接，不应整体推送公开仓库。公开版需要移除私有附件并保留通用部署模板。**

23个逻辑业务模块不是23个微服务。43项开发任务均为not_started，60个产品验收场景均为not_run。已完成的是文档/契约静态检查，不是服务器部署、软件实现或持续付费验证。

## 目录

""" + "\n".join(f"- [{d['title']}](#{d['id']})" for d in docs) + "\n"

md_parts = [header]
for d in docs:
    md_parts.append(f"\n---\n\n<a id=\"{d['id']}\"></a>\n\n" + rewrite_links(d["text"], d["rel"]))
(OUT_DIR / f"NexLoop_完整交接文档_{VERSION}.md").write_text("".join(md_parts), encoding="utf-8")

try:
    import markdown
except ImportError:
    sys.exit("markdown package missing: uv run --with markdown python tools/build_merged_docs.py")

old_html = sorted(OUT_DIR.glob("NexLoop_完整交接文档_v*.html"))
if not old_html:
    sys.exit("no previous HTML to borrow the stylesheet from")
head = old_html[-1].read_text(encoding="utf-8").split("<body", 1)[0]
head = re.sub(r"<title>.*?</title>", f"<title>NexLoop｜完整开发交接文档 {VERSION}</title>", head)

nav = "".join(f'<a href="#{d["id"]}"><span>{i+1:02d}</span>{html.escape(d["title"])}</a>' for i, d in enumerate(docs))
hero = (f'<header class="hero"><div class="eyebrow">Open-source continuous operations</div><h1>NexLoop</h1>'
        f'<div class="title">完整 PRD 与 Codex 开发交接文档</div>'
        f'<p>长期目标与关系持续存在，Agent 按需执行。先完成真实、可靠的运营闭环，再开展语义演进与消费者模拟。</p>'
        f'<div class="meta"><span>{VERSION} · {DATE}</span><span>23 个逻辑模块</span><span>43 项开发任务</span><span>60 个验收场景</span></div>'
        f'<div class="notice"><strong>状态：开发规格，尚未实施部署。</strong><br>此合并文档含私有 LAN 附件与本机源码路径，不应整体推送公开仓库。机器契约与配置模板位于配套 ZIP；静态文档校验不代表实际运行验收。</div></header>')
articles = []
for d in docs:
    body = markdown.markdown(rewrite_links(d["text"], d["rel"]), extensions=["tables", "fenced_code"])
    articles.append(f'<article id="{d["id"]}"><div class="source-label">{html.escape(d["rel"])}</div>{body}\n</article>')
footer = f'<div class="footer">NexLoop {VERSION} · 本文档独立离线可读，不依赖外部脚本或字体文件。</div>'
out = head + f'<body><nav aria-label="文档目录"><div class="logo">NexLoop</div><div class="sub">PRD · ARCHITECTURE · DELIVERY</div>{nav}</nav><main>{hero}' + "".join(articles) + footer + "</main></body></html>"
(OUT_DIR / f"NexLoop_完整交接文档_{VERSION}.html").write_text(out, encoding="utf-8")
print(f"wrote {len(docs)} articles -> NexLoop_完整交接文档_{VERSION}.md/.html")
