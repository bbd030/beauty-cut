"""Self-contained HTML reports: embedded data/assets, relative image references."""

import html
import json
from pathlib import Path


def _escape(value):
    return html.escape(str(value), quote=True)


def _document(title, body, script=""):
    assets = Path(__file__).with_name("assets")
    css = (assets / "report.css").read_text(encoding="utf-8")
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{_escape(title)}</title><style>{css}</style></head>'
            f'<body><main>{body}</main>{script}</body></html>')


def write_index(path, report):
    summary = report["summary"]
    rows = []
    for index, item in enumerate(report["results"], 1):
        name = _escape(Path(item["file"]).name)
        link = f'<a href="{_escape(item["report"])}">查看镜头与边界</a>' if item["report"] else "未生成"
        error = f'<pre>{_escape(item["error"])}</pre>' if item["error"] else ""
        rows.append(f'<tr><td>{index}</td><td>{name}{error}</td>'
                    f'<td>{"生成完成" if item["status"] == "passed" else "失败"}</td>'
                    f'<td>{len(item["shots"])}</td><td>{link}</td></tr>')
    body = (f'<p class="eyebrow">BEAUTYCUT · 本地镜头报告</p><h1>镜头预览与结果检查</h1>'
            f'<p>共 {summary["total"]} 条视频，生成完成 {summary["passed"]} 条，失败 {summary["failed"]} 条。</p>'
            '<p class="notice">生成完成只表示处理成功。请结合边界画面和完整播放检查误切、漏切；'
            '中间帧不保证清晰，渐变转场仍需人工核查。所有画面仅保存在本地。</p>'
            '<div class="table-wrap"><table><thead><tr><th>序号</th><th>视频</th><th>状态</th>'
            '<th>检测镜头数</th><th>报告</th></tr></thead><tbody>' + "".join(rows) +
            '</tbody></table></div><p><a href="results.json">查看机器可读 JSON 清单</a></p>')
    path.write_text(_document("BeautyCut 镜头报告", body), encoding="utf-8")


def write_video(path, item):
    cards = []
    for shot in item["shots"]:
        preview = shot["preview"]
        boundary = (f'<a href="#boundary-{shot["start_frame"]}">检查开始边界</a>'
                    if shot["id"] > 1 else '<span>视频起点</span>')
        cards.append(f'<article class="card" id="shot-{shot["id"]}">'
                     f'<img loading="lazy" src="{_escape(preview["image"])}" '
                     f'alt="镜头{shot["id"]}中间帧，第{preview["frame"]}帧">'
                     f'<div class="card-body"><h3>镜头 {shot["id"]}</h3>'
                     f'<p>{shot["start_seconds"]:.3f} – {shot["end_seconds"]:.3f} 秒'
                     f' · {shot["duration_seconds"]:.3f} 秒</p>'
                     f'<p class="muted">预览：第 {preview["frame"]} 帧 · {preview["time_seconds"]:.6f} 秒</p>'
                     f'<div class="actions"><button data-start="{shot["start_seconds"]}" '
                     f'data-end="{shot["end_seconds"]}" disabled>播放此镜头</button>{boundary}</div></div></article>')
    boundaries = []
    for boundary in item["boundary_previews"]:
        figures = []
        for ref in boundary["images"]:
            owner = next(s["id"] for s in item["shots"]
                         if s["start_frame"] <= ref["frame"] < s["end_frame_exclusive"])
            label = "新镜头第一帧" if ref["frame"] == boundary["frame"] else ""
            figures.append(f'<figure class="{"cut" if label else ""}"><img loading="lazy" '
                           f'src="{_escape(ref["image"])}" alt="第{ref["frame"]}帧">'
                           f'<figcaption>第 {ref["frame"]} 帧 · 镜头 {owner}<br>'
                           f'{ref["time_seconds"]:.6f} 秒 <strong>{label}</strong></figcaption></figure>')
        boundaries.append(f'<details id="boundary-{boundary["frame"]}"><summary>'
                          f'边界 · {boundary["time_seconds"]:.6f} 秒 · 新镜头第 {boundary["frame"]} 帧</summary>'
                          '<div class="boundary-strip">' + "".join(figures) + '</div></details>')
    source = {"source": item["source"], "playback": item["playback"],
              "video_duration": item["analysis"]["duration_seconds"]}
    # HTML parser must never see a closing script tag originating in a filename.
    encoded = json.dumps(source, ensure_ascii=True).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    player = Path(__file__).with_name("assets").joinpath("player.js").read_text(encoding="utf-8")
    script = f'<script id="report-data" type="application/json">{encoded}</script><script>{player}</script>'
    body = (f'<a href="../index.html">← 返回批次报告</a><p class="eyebrow">BEAUTYCUT · 本地核查</p>'
            f'<h1>{_escape(item["source"]["name"])}</h1>'
            f'<p>{len(item["shots"])} 个镜头 · {item["analysis"]["duration_seconds"]:.3f} 秒'
            ' · 帧号从 0 开始 · 区间包含开始、不包含结束</p>'
            '<section class="player-panel"><h2>原视频播放</h2>'
            '<p>选择对应原视频，文件由本地浏览器读取。未选择文件时仍可浏览全部图片。</p>'
            '<label class="file-label">选择本地原视频 <input id="source-file" type="file" accept="video/*,.mkv,.mov,.avi"></label>'
            '<p id="player-status" role="status" aria-live="polite">尚未选择原视频。</p>'
            '<video id="player" controls preload="metadata" playsinline></video>'
            '<p class="muted">播放器定位和停止均为近似值，请使用边界图片核查逐帧位置。'
            '可用播放器控制条连续观看全片，特别检查没有被切开的长镜头。</p></section>'
            '<p class="notice">预览取镜头时间中点所在帧，不保证清晰或无遮挡。'
            '检测结果尚需人工确认；渐变、叠化、连续产品运动和细微跳切可能误切或漏切。</p>'
            '<nav><a href="#shots">镜头列表</a> · <a href="#boundaries">边界连续帧</a></nav>'
            '<h2 id="shots">镜头列表</h2><div class="shots">' + "".join(cards) + '</div>'
            '<h2 id="boundaries">边界连续帧</h2><p>边界前后各最多两帧，绿色边框表示新镜头第一帧。</p>'
            + ("".join(boundaries) or '<p>本条视频没有检测到内部边界。</p>'))
    path.write_text(_document(item["source"]["name"] + " · BeautyCut", body, script), encoding="utf-8")
