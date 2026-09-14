#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================


from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# 默认数据库路径：脚本所在目录 / data / db / bird_observations.db（避免硬编码绝对路径）
_vd4ca91d7 = Path(__file__).resolve().parent
_v29bac30f = _vd4ca91d7 / "data" / "db" / "bird_observations.db"

# 面板页面（单页应用：页面数据全部通过 /api/* 接口异步加载）
_v8b538b6f = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>鸟类观察数据库面板</title>
<style>
  /* ===== 全局样式 ===== */
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: "Microsoft YaHei", sans-serif; background: #f4f6f9; color: #24303f; }
  header { background: #1f6f54; color: #fff; padding: 14px 28px; display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  header h1 { font-size: 20px; font-weight: 600; }
  nav { display: flex; gap: 8px; margin-left: 12px; }
  nav button { border: none; background: rgba(255,255,255,.15); color: #fff; padding: 8px 18px; border-radius: 18px; cursor: pointer; font-size: 14px; }
  nav button.active { background: #fff; color: #1f6f54; font-weight: 600; }
  main { max-width: 1180px; margin: 22px auto; padding: 0 18px; }
  .hint { font-size: 13px; color: #7b8794; margin-left: auto; }

  /* ===== 统计卡片 ===== */
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 20px; }
  .card { background: #fff; border-radius: 12px; padding: 18px 20px; box-shadow: 0 1px 4px rgba(0,0,0,.06); }
  .card .num { font-size: 30px; font-weight: 700; color: #1f6f54; }
  .card .lbl { font-size: 13px; color: #7b8794; margin-top: 4px; }

  /* ===== 表格 ===== */
  table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.06); font-size: 13.5px; }
  th { background: #eef3f0; text-align: left; padding: 10px 12px; white-space: nowrap; color: #44586b; }
  td { padding: 9px 12px; border-top: 1px solid #eef1f4; vertical-align: top; }
  tr:hover td { background: #f7faf8; }
  .muted { color: #8a95a1; }
  .tag { display: inline-block; background: #e6f2ec; color: #1f6f54; border-radius: 10px; padding: 1px 9px; font-size: 12px; margin-right: 4px; }

  /* ===== 工具条 ===== */
  .toolbar { display: flex; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; align-items: center; }
  .toolbar input[type=text] { flex: 1; min-width: 220px; padding: 9px 12px; border: 1px solid #d4dbe2; border-radius: 8px; font-size: 14px; }
  .toolbar select { padding: 9px 10px; border: 1px solid #d4dbe2; border-radius: 8px; font-size: 14px; background: #fff; }
  .pager { display: flex; gap: 8px; align-items: center; margin: 14px 0; font-size: 13.5px; color: #55606c; }
  .pager button { padding: 7px 16px; border: 1px solid #d4dbe2; background: #fff; border-radius: 8px; cursor: pointer; }
  .pager button:disabled { opacity: .4; cursor: default; }

  /* ===== 照片画廊 ===== */
  .gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 14px; }
  .ph { background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.06); }
  .ph img { width: 100%; height: 150px; object-fit: cover; display: block; cursor: zoom-in; background: #e8ecef; }
  .ph .cap { padding: 9px 11px; font-size: 12.5px; line-height: 1.55; }
  .ph .cap b { color: #1f6f54; }
  .ph .cap a { color: #2563ab; text-decoration: none; }

  /* ===== 大图预览 ===== */
  #mask { position: fixed; inset: 0; background: rgba(10,14,18,.88); display: none; align-items: center; justify-content: center; z-index: 99; cursor: zoom-out; }
  #mask img { max-width: 94vw; max-height: 92vh; border-radius: 6px; }
  .empty { padding: 46px; text-align: center; color: #8a95a1; background: #fff; border-radius: 12px; }
</style>
</head>
<body>
<header>
  <h1>鸟类观察数据库面板</h1>
  <nav>
    <button data-tab="stats" class="active">总览</button>
    <button data-tab="obs">观察记录</button>
    <button data-tab="photos">照片</button>
    <button data-tab="batches">批次</button>
  </nav>
  <span class="hint" id="dbpath"></span>
</header>
<main>
  <div id="tab-stats"></div>

  <div id="tab-obs" style="display:none">
    <div class="toolbar">
      <input type="text" id="obs-q" placeholder="搜索物种 / 地点 / 观察者 / 描述...">
      <select id="obs-taxon"><option value="">全部类群</option></select>
      <select id="obs-size">
        <option value="50">每页 50 条</option>
        <option value="20">每页 20 条</option>
        <option value="200">每页 200 条</option>
      </select>
    </div>
    <div id="obs-table"></div>
    <div class="pager">
      <button id="obs-prev">上一页</button>
      <span id="obs-page"></span>
      <button id="obs-next">下一页</button>
    </div>
  </div>

  <div id="tab-photos" style="display:none">
    <div class="toolbar">
      <input type="text" id="ph-q" placeholder="搜索物种 / 文件名...">
      <select id="ph-taxon"><option value="">全部类群</option></select>
      <select id="ph-size">
        <option value="60">每页 60 张</option>
        <option value="24">每页 24 张</option>
        <option value="120">每页 120 张</option>
      </select>
    </div>
    <div id="ph-gallery"></div>
    <div class="pager">
      <button id="ph-prev">上一页</button>
      <span id="ph-page"></span>
      <button id="ph-next">下一页</button>
    </div>
  </div>

  <div id="tab-batches" style="display:none"></div>
</main>

<div id="mask"><img id="mask-img" alt=""></div>

<script>
/* ================= 通用工具 ================= */
// HTML 转义，防止特殊字符破坏页面
function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
// 调用后端接口，返回 JSON
async function api(path) {
  const r = await fetch(path);
  return await r.json();
}
// 标签页切换
document.querySelectorAll('nav button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    ['stats','obs','photos','batches'].forEach(t =>
      document.getElementById('tab-' + t).style.display = (t === btn.dataset.tab ? '' : 'none'));
    if (btn.dataset.tab === 'stats') loadStats();
    if (btn.dataset.tab === 'obs') loadObs();
    if (btn.dataset.tab === 'photos') loadPhotos();
    if (btn.dataset.tab === 'batches') loadBatches();
  };
});
// 点击大图遮罩关闭
document.getElementById('mask').onclick = () => document.getElementById('mask').style.display = 'none';
function showBig(src) {
  document.getElementById('mask-img').src = src;
  document.getElementById('mask').style.display = 'flex';
}

/* ================= 总览 ================= */
async function loadStats() {
  const s = await api('/api/stats');
  document.getElementById('dbpath').textContent = s.db_path;
  document.getElementById('tab-stats').innerHTML = `
    <div class="cards">
      <div class="card"><div class="num">${s.observations}</div><div class="lbl">观察记录</div></div>
      <div class="card"><div class="num">${s.photos}</div><div class="lbl">照片</div></div>
      <div class="card"><div class="num">${s.taxa}</div><div class="lbl">类群</div></div>
      <div class="card"><div class="num">${s.batches}</div><div class="lbl">下载批次</div></div>
    </div>
    <table><tr><th>类群</th><th>学名</th><th>观察数</th><th>照片数</th></tr>
    ${s.per_taxon.map(t => `<tr><td><span class="tag">${esc(t.common_name) || '—'}</span></td>
      <td><i>${esc(t.scientific_name)}</i> <span class="muted">#${t.taxon_id}</span></td>
      <td>${t.obs}</td><td>${t.photos}</td></tr>`).join('')}
    </table>
    <div style="height:14px"></div>
    <table><tr><th>批次</th><th>时间</th><th>类群成功</th><th>观察数</th><th>照片数</th><th>下载成功</th><th>失败</th></tr>
    ${s.recent_batches.map(b => `<tr><td>#${b.id}</td><td class="muted">${esc(b.batch_time)}</td>
      <td>${b.taxa_succeeded}/${b.taxa_requested}</td><td>${b.total_observations}</td>
      <td>${b.total_photos}</td><td>${b.total_downloaded}</td><td>${b.total_failed}</td></tr>`).join('')}
    </table>`;
}

/* ================= 观察记录 ================= */
let obsOffset = 0;
function obsState() { return { q: document.getElementById('obs-q').value.trim(),
  taxon: document.getElementById('obs-taxon').value, size: +document.getElementById('obs-size').value }; }
async function loadObs() {
  const st = obsState();
  const d = await api(`/api/observations?q=${encodeURIComponent(st.q)}&taxon_id=${st.taxon}&limit=${st.size}&offset=${obsOffset}`);
  // 下拉框只首次填充
  const sel = document.getElementById('obs-taxon');
  if (sel.options.length === 1) {
    d.taxa.forEach(t => sel.insertAdjacentHTML('beforeend',
      `<option value="${t.taxon_id}">${esc(t.common_name)} ${esc(t.scientific_name)}</option>`));
  }
  const rows = d.rows.map(o => `<tr>
    <td><a href="${esc(o.uri)}" target="_blank">#${o.observation_id}</a><br><span class="muted">${esc(o.uuid || '')}</span></td>
    <td><b>${esc(o.common_name)}</b><br><i>${esc(o.scientific_name)}</i></td>
    <td>${esc(o.observed_on || '—')}</td>
    <td>${esc(o.place_guess || '—')}${o.latitude ? `<br><span class="muted">${o.latitude.toFixed(3)}, ${o.longitude.toFixed(3)}</span>` : ''}</td>
    <td>${esc(o.observer_login || o.observer_name || '—')}</td>
    <td><span class="tag">${esc(o.quality_grade)}</span>${o.photo_count ?? ''} 张</td>
    <td class="muted" style="max-width:220px">${esc((o.local_photo_files || '').split('|')[0] || '')}</td>
  </tr>`).join('');
  document.getElementById('obs-table').innerHTML = d.rows.length ?
    `<table><tr><th>观察</th><th>物种</th><th>观察日期</th><th>地点</th><th>观察者</th><th>质量</th><th>照片文件</th></tr>${rows}</table>`
    : `<div class="empty">没有匹配的观察记录</div>`;
  document.getElementById('obs-page').textContent = `第 ${obsOffset / st.size + 1} 页 / 共 ${d.total} 条`;
  document.getElementById('obs-prev').disabled = obsOffset <= 0;
  document.getElementById('obs-next').disabled = obsOffset + st.size >= d.total;
}
document.getElementById('obs-q').oninput = () => { obsOffset = 0; loadObs(); };
document.getElementById('obs-taxon').onchange = () => { obsOffset = 0; loadObs(); };
document.getElementById('obs-size').onchange = () => { obsOffset = 0; loadObs(); };
document.getElementById('obs-prev').onclick = () => { obsOffset = Math.max(0, obsOffset - obsState().size); loadObs(); };
document.getElementById('obs-next').onclick = () => { obsOffset += obsState().size; loadObs(); };

/* ================= 照片 ================= */
let phOffset = 0;
function phState() { return { q: document.getElementById('ph-q').value.trim(),
  taxon: document.getElementById('ph-taxon').value, size: +document.getElementById('ph-size').value }; }
async function loadPhotos() {
  const st = phState();
  const d = await api(`/api/photos?q=${encodeURIComponent(st.q)}&taxon_id=${st.taxon}&limit=${st.size}&offset=${phOffset}`);
  const sel = document.getElementById('ph-taxon');
  if (sel.options.length === 1) {
    d.taxa.forEach(t => sel.insertAdjacentHTML('beforeend',
      `<option value="${t.taxon_id}">${esc(t.common_name)} ${esc(t.scientific_name)}</option>`));
  }
  document.getElementById('ph-gallery').innerHTML = d.rows.map(p => {
    const src = `/photo?path=${encodeURIComponent(p.local_path)}`;
    return `<div class="ph"><img loading="lazy" src="${src}" data-big="${src}&full=1"
      onclick="showBig(this.dataset.big)">
      <div class="cap"><b>${esc(p.common_name)}</b> <i>${esc(p.scientific_name)}</i><br>
      ${p.observed_on || ''} · ${esc(p.observer_login || '')}<br>
      <a href="${esc(p.uri)}" target="_blank">观察页面</a> · ${p.width || '?'}×${p.height || '?'}</div></div>`;
  }).join('') || `<div class="empty">没有匹配的照片</div>`;
  document.getElementById('ph-page').textContent = `第 ${phOffset / st.size + 1} 页 / 共 ${d.total} 张`;
  document.getElementById('ph-prev').disabled = phOffset <= 0;
  document.getElementById('ph-next').disabled = phOffset + st.size >= d.total;
}
document.getElementById('ph-q').oninput = () => { phOffset = 0; loadPhotos(); };
document.getElementById('ph-taxon').onchange = () => { phOffset = 0; loadPhotos(); };
document.getElementById('ph-size').onchange = () => { phOffset = 0; loadPhotos(); };
document.getElementById('ph-prev').onclick = () => { phOffset = Math.max(0, phOffset - phState().size); loadPhotos(); };
document.getElementById('ph-next').onclick = () => { phOffset += phState().size; loadPhotos(); };

/* ================= 批次 ================= */
async function loadBatches() {
  const d = await api('/api/batches');
  document.getElementById('tab-batches').innerHTML = `
    <table><tr><th>批次</th><th>UUID</th><th>时间</th><th>类群</th><th>质量等级</th><th>观察</th><th>照片</th><th>成功</th><th>失败</th><th>输出目录</th></tr>
    ${d.rows.map(b => `<tr><td>#${b.id}</td><td class="muted" style="font-size:11px">${esc(b.batch_uuid)}</td>
      <td class="muted">${esc(b.batch_time)}</td><td>${b.taxa_succeeded}/${b.taxa_requested}</td>
      <td><span class="tag">${esc(b.quality_grade || '')}</span></td><td>${b.total_observations}</td>
      <td>${b.total_photos}</td><td>${b.total_downloaded}</td><td>${b.total_failed}</td>
      <td class="muted" style="font-size:12px">${esc(b.output_dir || '')}</td></tr>`).join('')}
    </table>`;
}

/* 初始加载总览页 */
loadStats();
</script>
</body>
</html>
"""


class _v623a5ec1(BaseHTTPRequestHandler):

    # 数据库路径与允许读取的根目录，在启动时由 serve() 注入
    main_db: Path = _v29bac30f
    allow_root: Path = _vd4ca91d7
    # 照片文件名索引：文件名 -> 绝对路径（首次访问照片时扫描构建）
    photo_index: dict | None = None

    # ---------- 基础工具 ----------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8")

    def log_message(self, fmt, *args):  # 静默默认访问日志，保持控制台干净
        pass

    # ---------- 请求分发 ----------
    def do_GET(self):  # noqa: N802（http.server 规定的方法名）
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            if route == "/" or route == "/index.html":
                self._send(200, _v8b538b6f.encode("utf-8"), "text/html; charset=utf-8")
            elif route == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            elif route == "/api/stats":
                self._api_stats()
            elif route == "/api/observations":
                self._api_observations(parse_qs(parsed.query))
            elif route == "/api/photos":
                self._api_photos(parse_qs(parsed.query))
            elif route == "/api/batches":
                self._api_batches()
            elif route == "/photo":
                self._serve_photo(parse_qs(parsed.query))
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:  # 统一兜底：任何接口异常返回 500 信息
            msg = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
            self._send(500, msg, "application/json; charset=utf-8")

    # ---------- 数据库连接 ----------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.main_db.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------- 接口实现 ----------
    def _api_stats(self) -> None:
        with self._connect() as conn:
            one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
            taxa_rows = conn.execute(
                """SELECT t.taxon_id, t.scientific_name, t.common_name,
                          COUNT(DISTINCT o.id) AS obs,
                          (SELECT COUNT(*) FROM photos p JOIN observations o2
                             ON o2.observation_id = p.observation_id
                            WHERE o2.taxon_id = t.taxon_id) AS photos
                     FROM taxa t LEFT JOIN observations o ON o.taxon_id = t.taxon_id
                    GROUP BY t.id ORDER BY obs DESC, t.taxon_id""").fetchall()
            batches = conn.execute(
                "SELECT * FROM batch_runs ORDER BY id DESC LIMIT 8").fetchall()
        self._json({
            "db_path": str(self.main_db),
            "observations": one("SELECT COUNT(*) FROM observations"),
            "photos": one("SELECT COUNT(*) FROM photos"),
            "taxa": one("SELECT COUNT(*) FROM taxa"),
            "batches": one("SELECT COUNT(*) FROM batch_runs"),
            "per_taxon": [dict(r) for r in taxa_rows],
            "recent_batches": [dict(r) for r in batches],
        })

    def _api_observations(self, qs) -> None:
        q = qs.get("q", [""])[0].strip()
        taxon = qs.get("taxon_id", [""])[0].strip()
        limit = min(int(qs.get("limit", ["50"])[0]), 500)
        offset = max(int(qs.get("offset", ["0"])[0]), 0)
        where, params = [], []
        if q:
            # 关键词模糊匹配多个字段
            like = f"%{q}%"
            where.append("(o.common_name LIKE ? OR o.scientific_name LIKE ? "
                         "OR o.species_guess LIKE ? OR o.place_guess LIKE ? "
                         "OR o.observer_login LIKE ? OR o.description LIKE ?)")
            params += [like] * 6
        if taxon:
            where.append("o.taxon_id = ?")
            params.append(int(taxon))
        cond = ("WHERE " + " AND ".join(where)) if where else ""
        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM observations o {cond}", params).fetchone()[0]
            rows = conn.execute(
                f"""SELECT o.observation_id, o.uuid, o.uri, o.quality_grade,
                           o.scientific_name, o.common_name, o.observed_on,
                           o.latitude, o.longitude, o.place_guess, o.description,
                           o.observer_login, o.observer_name, o.photo_count,
                           o.local_photo_files
                      FROM observations o {cond}
                     ORDER BY o.observed_on DESC, o.observation_id DESC
                     LIMIT ? OFFSET ?""", params + [limit, offset]).fetchall()
            taxa = conn.execute(
                "SELECT taxon_id, scientific_name, common_name FROM taxa ORDER BY common_name").fetchall()
        self._json({"total": total, "rows": [dict(r) for r in rows],
                    "taxa": [dict(r) for r in taxa]})

    def _api_photos(self, qs) -> None:
        q = qs.get("q", [""])[0].strip()
        taxon = qs.get("taxon_id", [""])[0].strip()
        limit = min(int(qs.get("limit", ["60"])[0]), 500)
        offset = max(int(qs.get("offset", ["0"])[0]), 0)
        where, params = ["p.download_ok = 1"], []
        if q:
            like = f"%{q}%"
            where.append("(o.common_name LIKE ? OR o.scientific_name LIKE ? OR p.local_path LIKE ?)")
            params += [like] * 3
        if taxon:
            where.append("o.taxon_id = ?")
            params.append(int(taxon))
        cond = "WHERE " + " AND ".join(where)
        with self._connect() as conn:
            total = conn.execute(
                f"""SELECT COUNT(*) FROM photos p
                      JOIN observations o ON o.observation_id = p.observation_id {cond}""",
                params).fetchone()[0]
            rows = conn.execute(
                f"""SELECT p.photo_id, p.local_path, p.width, p.height,
                           o.observation_id, o.uri, o.common_name, o.scientific_name,
                           o.observed_on, o.observer_login
                      FROM photos p JOIN observations o ON o.observation_id = p.observation_id
                      {cond}
                     ORDER BY o.observed_on DESC, p.photo_id DESC
                     LIMIT ? OFFSET ?""", params + [limit, offset]).fetchall()
            taxa = conn.execute(
                "SELECT taxon_id, scientific_name, common_name FROM taxa ORDER BY common_name").fetchall()
        self._json({"total": total, "rows": [dict(r) for r in rows],
                    "taxa": [dict(r) for r in taxa]})

    def _api_batches(self) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM batch_runs ORDER BY id DESC").fetchall()
        self._json({"rows": [dict(r) for r in rows]})

    def _serve_photo(self, qs) -> None:
        raw = qs.get("path", [""])[0]
        if not raw:
            self._send(400, b"missing path", "text/plain; charset=utf-8")
            return
        resolved_root = self.allow_root.resolve()
        path = Path(unquote(raw))

        # 1) 绝对路径：直接使用（需位于数据目录内）
        # 2) 相对路径：先按数据根目录拼接，再退回文件名索引查找
        if path.is_absolute():
            path = path.resolve()
        else:
            candidate = (resolved_root / path).resolve()
            if not candidate.is_file():
                candidate = self._lookup_by_name(path.name)
            path = candidate

        # 安全检查：只允许读取数据根目录下的文件
        if path is None or (resolved_root not in path.parents and path != resolved_root):
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        if not path.is_file():
            self._send(404, b"photo not found", "text/plain; charset=utf-8")
            return
        suffix = path.suffix.lower()
        ctype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".gif": "image/gif", ".webp": "image/webp"}.get(suffix, "application/octet-stream")
        self._send(200, path.read_bytes(), ctype)

    @classmethod
    def _lookup_by_name(cls, name: str) -> Path | None:
        if not name:
            return None
        if cls.photo_index is None:
            index: dict = {}
            # 扫描数据目录下所有 photos 文件夹中的图片文件
            for item in cls.allow_root.rglob("*"):
                if item.is_file() and item.suffix.lower() in (
                        ".jpg", ".jpeg", ".png", ".gif", ".webp"):
                    index.setdefault(item.name, item.resolve())
            cls.photo_index = index
        return cls.photo_index.get(name)


def _v6308aa23(main_db: Path = _v29bac30f, port: int = 8765,
          open_browser: bool = True) -> None:
    main_db = Path(main_db).resolve()
    if not main_db.exists():
        print(f"[错误] 主数据库不存在：{main_db}")
        print("请先运行 python main.py init-db 或下载数据后再启动面板。")
        sys.exit(1)

    # 允许读取的根目录 = 数据库所在目录的上一级（即 data/ 数据目录）
    _v623a5ec1.main_db = main_db
    _v623a5ec1.allow_root = main_db.parent.parent

    server = ThreadingHTTPServer(("127.0.0.1", port), _v623a5ec1)
    url = f"http://127.0.0.1:{port}/"
    print(f"数据库面板已启动：{url}")
    print(f"数据库文件：{main_db}")
    print("按 Ctrl+C 停止服务。")
    if open_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n面板已停止。")
    finally:
        server.server_close()


def _v9a12dab4() -> int:
    parser = argparse.ArgumentParser(description="鸟类观察数据库交互面板")
    parser.add_argument("--main-db", type=Path, default=_v29bac30f,
                        help="主数据库路径（默认 data/db/bird_observations.db）")
    parser.add_argument("--port", type=int, default=8765, help="服务端口（默认 8765）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args()
    _v6308aa23(main_db=args.main_db, port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(_v9a12dab4())
