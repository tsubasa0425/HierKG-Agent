#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TextSegmentation v2 —— docx → 结构化章节树 + 块级正文 + 字符偏移。

与 v1 的区别：
  * 层级不再硬编码 1..4：标题深度来自编号本身（1 / 1.2 / 1.2.3）或样式，
    用栈做容错挂载；父编号对不上只告警，不丢标题。
  * 同时支持样式（Heading / 标题 / outlineLvl）与正则兜底；目录页自动跳过。
  * 正文按 block 保留（段落 / 表格 / 列表 / 代码 / 公式 / 图表标题），
    每块带 char_start / char_end，偏移指向同目录下 doc_text.txt 的规范文本。
  * 章节 ID 在解析阶段一次性确定：编号唯一 → 用编号；重复或缺失 → path_{sha1}。
  * 输出为对象（含 doc_id / 统计 / sections），不再是裸数组。

输出：
  output/toc_structure.json
  output/doc_text.txt        （规范纯文本，供 evidence 偏移核对）
  output/toc_warnings.log
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import yaml
from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

try:  # 作为包导入
    from .schema_loader import get_schema, make_section_id
except ImportError:  # 直接以脚本运行
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from KGBuild.schema_loader import get_schema, make_section_id

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR / "config"

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULTS: Dict[str, Any] = {
    "DOCX_NAME": "Hello-Agents_0.docx",
    "TOC_NAME": "toc_structure.json",
    "WARN_NAME": "toc_warnings.log",
    "ENCODING": "utf-8",
    "USE_STYLE_FIRST": True,
    "ENABLE_REGEX_FALLBACK": True,
    "MAX_LEVEL": 6,
    "SKIP_TOC": True,
    "MIN_TOC_LINES": 3,
    "MAX_TITLE_LEN": 80,
    "KEEP_PREAMBLE": True,
    "PREAMBLE_TITLE": "前言",
    "SAVE_DOC_TEXT": True,
    "DOC_TEXT_NAME": "doc_text.txt",
    "WARNINGS_PRINT_TOP": 10,
    "SAVE_WARNINGS_FILE": True,
    "NORMALIZE_SPACES": True,
    "REMOVE_TRAILING_PAGE_NO": True,
    "STRIP_COLONS": True,
    "SPACE_SUBSTITUTIONS": [" ", " ", "　"],
    "HEADING_MAP": {},
    "CHAPTER_PATTERN": r"^\s*第[\s　]*([一二三四五六七八九十百零〇0-9]+)[\s　]*章[\s　：:]*([^\s].+?)\s*$",
    "PART_PATTERN": r"^\s*第[\s　]*([一二三四五六七八九十百零〇0-9]+)[\s　]*部分[\s　：:]*([^\s].+?)\s*$",
}


def _load_yaml(p: Path) -> Dict[str, Any]:
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_additional_configs(include_files: Sequence[str], base_dir: Path) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for rel in include_files or []:
        rel_str = str(rel)
        inc_path = Path(rel_str)
        if not inc_path.is_absolute():
            inc_path = (base_dir / rel_str).resolve()
        if not inc_path.exists() and rel_str.startswith("config/"):
            inc_path = (base_dir / rel_str.split("/", 1)[1]).resolve()
        if not inc_path.exists():
            raise FileNotFoundError(f"找不到包含文件：{inc_path}")
        merged.update(_load_yaml(inc_path))
    return merged


def _load_textseg_config() -> Dict[str, Any]:
    cfg_path = CONFIG_DIR / "config.yaml"
    fallback = CONFIG_DIR / "text.yaml"
    merged: Dict[str, Any] = {}
    if cfg_path.exists():
        merged.update(_load_yaml(cfg_path))
        merged.update(_load_additional_configs(
            merged.get("include_files", []) or [], base_dir=CONFIG_DIR))
    if "TextSegConfig" not in merged:
        if not fallback.exists():
            raise FileNotFoundError(
                f"找不到配置：{cfg_path} 或 {fallback}；"
                f"请先 cp {CONFIG_DIR / 'config.yaml.example'} {cfg_path}")
        merged.update(_load_yaml(fallback))
    cfg = dict(DEFAULTS)
    cfg.update(merged.get("TextSegConfig") or {})
    return cfg


TextSegConfig = _load_textseg_config()

logger = logging.getLogger("TextSegmentation")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
if not logger.handlers:
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# 正则与工具
# ---------------------------------------------------------------------------

HAS_LETTER_RE = re.compile(r"[一-鿿A-Za-z]")
CJK_RE = re.compile(r"[一-鿿]")
CHAPTER_RE = re.compile(TextSegConfig["CHAPTER_PATTERN"])
PART_RE = re.compile(TextSegConfig["PART_PATTERN"])
APPENDIX_RE = re.compile(
    r"^\s*附[\s　]*录\s*([A-Za-z0-9一二三四五六七八九十]+)[\s　：:]*([^\s].+?)\s*$")
EN_CHAPTER_RE = re.compile(r"^\s*(?:chapter|part)\s+(\d{1,3})[\s:：.\-]*(.+?)\s*$", re.I)
# 1 / 1.2 / 1 . 2 . 3 / 1.2.3.4 ...
NUM_HEADING_RE = re.compile(
    r"^\s*(?P<num>\d{1,3}(?:\s*[.．]\s*\d{1,3}){0,5})\s*"
    r"(?:(?P<sep>[、:：\-—\s　]+)(?P<title>.+?)"
    r"|(?P<title2>[一-鿿A-Za-z].*?))\s*$"
)
DOT_LEADER_RE = re.compile(r"(?:\.{3,}|…{2,}|·{3,}|_{3,})\s*\d+\s*$")
TOC_STYLE_RE = re.compile(r"^(toc|目录)", re.I)
STYLE_HEADING_RE = re.compile(r"^(?:heading|标题)\s*(\d+)$", re.I)
CAPTION_RE = re.compile(
    r"^\s*(图|表|figure|table)\s*[\d一二三四五六七八九十]+(?:[.\-－]\d+)*\s*[：:.\s]", re.I)
CODE_LINE_RE = re.compile(
    r"^\s*(?:def |class |import |from \w+ import|return\b|for\b|while\b|"
    r"if\b|elif\b|else\s*:|try\s*:|except\b|with\b|print\(|@\w+)", re.M)
FORMULA_STYLE_RE = re.compile(r"(formula|equation|公式)", re.I)
CODE_STYLE_RE = re.compile(r"(code|代码|source)", re.I)
LIST_STYLE_RE = re.compile(r"(list|列表)", re.I)
# 标题里出现这些，基本可以断定是代码行 / 注释行
CODE_TITLE_RE = re.compile(
    r'(?:"""|\'\'\'|=|;|\{|\}|\[|\]|\\|^\s*(?:#|//)'
    r'|\b(?:print|def|class|import|return|raise|except|sys|os|self)\s*[.(])')
# 列表项标记：(2) / 1) / ① / • ；这类段落即使 outlineLvl 异常也不当标题
LIST_ITEM_RE = re.compile(r"^\s*(?:[（(]\s*\d+\s*[)）]|\d+\s*[、.)）]|[①-⑳]|[•·▪◦]|[-*]\s)")

CN_NUM_MAP = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100}


def cn2int(s: str) -> int:
    s = (s or "").strip()
    if not s:
        return 1
    if s.isdigit():
        return int(s)
    total, tmp, seen = 0, 0, False
    for ch in s:
        if ch == "百":
            total = (total if total else 1) * 100
            tmp, seen = 0, True
        elif ch == "十":
            total += (tmp if tmp else 1) * 10
            tmp, seen = 0, True
        else:
            v = CN_NUM_MAP.get(ch)
            if v is None:
                try:
                    return int(s)
                except ValueError:
                    return 1
            tmp, seen = v, True
    if seen:
        return total + tmp or 1
    try:
        return int(s)
    except ValueError:
        return 1


def _normalize_spaces(text: str) -> str:
    if not text:
        return ""
    if TextSegConfig["NORMALIZE_SPACES"]:
        for ch in TextSegConfig["SPACE_SUBSTITUTIONS"]:
            text = text.replace(ch, " ")
        text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def clean_title(t: str) -> str:
    """去尾部点线/页码、两端空白与冒号。

    只用「点线 / 制表符 / 连续空格 + 数字」判定页码，
    避免把 GPT-4 这类以数字结尾的标题截断。
    """
    if not t:
        return ""
    t = _normalize_spaces(t)
    if TextSegConfig["REMOVE_TRAILING_PAGE_NO"]:
        t = re.sub(r"(?:[.·・—\-_]{3,}|…{2,}|\t| {2,})\s*\d+\s*$", "", t)
    if TextSegConfig["STRIP_COLONS"]:
        t = t.strip(" ：:　\t")
    t = re.sub(r"\s+", " ", t).strip()
    # "1 . 1 智 能 体" → "1.1 智能体"：去掉汉字之间的排版空格
    t = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", t)
    # "什么是智能体 ?" → "什么是智能体?"
    t = re.sub(r"\s+([?？!！:：;；,，.。])", r"\1", t)
    return t


def remove_all_spaces(text: str) -> str:
    return re.sub(r"[\s　]+", "", text or "")


def _sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sanitize_doc_id(raw: str) -> str:
    """doc_id 会进入 evidence id（冒号分隔），因此不能含冒号/路径符。"""
    s = re.sub(r"[^0-9A-Za-z一-鿿._-]+", "-", (raw or "").strip())
    return s.strip("-") or "doc"


# ---------------------------------------------------------------------------
# 段落属性
# ---------------------------------------------------------------------------

def _style_name(par: Paragraph) -> str:
    try:
        return (par.style.name or "").strip()
    except Exception:
        return ""


def _is_toc_style(par: Paragraph) -> bool:
    return bool(TOC_STYLE_RE.match(_style_name(par)))


def _outline_level(par: Paragraph) -> Optional[int]:
    try:
        el = par._p.xpath("./w:pPr/w:outlineLvl")
        if not el:
            return None
        val = el[0].get(qn("w:val"))
        if val is None:
            val = getattr(el[0], "val", None)
        lvl = int(val)
        if 0 <= lvl <= 8:
            return lvl + 1
    except Exception:
        pass
    return None


def _num_ilvl(par: Paragraph) -> Optional[int]:
    try:
        el = par._p.xpath("./w:pPr/w:numPr/w:ilvl")
        if el:
            val = el[0].get(qn("w:val"))
            if val is None:
                val = getattr(el[0], "val", None)
            return int(val)
    except Exception:
        pass
    return None


def _style_level(par: Paragraph) -> Optional[int]:
    name = _style_name(par)
    if not name:
        return _outline_level(par)
    mapping = {str(k).strip().lower(): int(v)
               for k, v in (TextSegConfig.get("HEADING_MAP") or {}).items()}
    lvl = mapping.get(name.lower())
    if lvl:
        return lvl
    m = STYLE_HEADING_RE.match(name)
    if m:
        return int(m.group(1))
    return _outline_level(par)


def _looks_like_code(text: str) -> bool:
    if "```" in text:
        return True
    return "\n" in text and bool(CODE_LINE_RE.search(text))


def _is_heading_title(title: str) -> bool:
    if not title or len(title) > int(TextSegConfig["MAX_TITLE_LEN"]):
        return False
    if not HAS_LETTER_RE.search(title):
        return False
    if title[-1] in "。；;，,":
        return False
    if _looks_like_code(title):
        return False
    if CODE_TITLE_RE.search(title):
        return False
    if LIST_ITEM_RE.match(title):
        return False
    if not CJK_RE.search(title) and re.search(r"[()]", title):
        return False
    return not re.fullmatch(r"[\d\W_]+", title)


def _block_type(par: Paragraph, text: str) -> str:
    style = _style_name(par)
    if CODE_STYLE_RE.search(style) or _looks_like_code(text):
        return "code"
    if FORMULA_STYLE_RE.search(style) or (text.startswith("$") and text.endswith("$")):
        return "formula"
    if CAPTION_RE.match(text):
        return "table" if re.match(r"^\s*(表|table)", text, re.I) else "figure"
    if _num_ilvl(par) is not None or LIST_STYLE_RE.search(style):
        return "list"
    return "paragraph"


def _is_toc_line(par: Paragraph, text: str) -> bool:
    return _is_toc_style(par) or bool(DOT_LEADER_RE.search(text))


# ---------------------------------------------------------------------------
# 标题识别
# ---------------------------------------------------------------------------

def _numeric_parts(raw: str) -> List[str]:
    return [p for p in re.split(r"[.．]", remove_all_spaces(raw)) if p]


def _parse_heading(par: Paragraph, text: str) -> Optional[Dict[str, Any]]:
    """返回 {numbering, title, depth, source}；不是标题则 None。"""
    style_lvl = _style_level(par) if TextSegConfig["USE_STYLE_FIRST"] else None
    max_level = int(TextSegConfig["MAX_LEVEL"])
    # 正则标题只在「短行」或「样式明确是标题」时生效，
    # 避免把 "第三章介绍了……" 这类正文句子当成标题。
    regex_ok = style_lvl is not None or len(text) <= 40

    # 1) 第 X 章 / 第 X 部分 / Chapter N / 附录
    m = CHAPTER_RE.match(text)
    if m and regex_ok:
        title = clean_title(m.group(2))
        if _is_heading_title(title):
            return {"numbering": str(cn2int(m.group(1))), "title": title,
                    "depth": 1, "source": "chapter"}

    m = PART_RE.match(text)
    if m and regex_ok:
        title = clean_title(m.group(2))
        if _is_heading_title(title):
            return {"numbering": str(cn2int(m.group(1))), "title": title,
                    "depth": 1, "source": "part"}

    m = APPENDIX_RE.match(text)
    if m and regex_ok:
        title = clean_title(m.group(2))
        if _is_heading_title(title):
            return {"numbering": f"附录{remove_all_spaces(m.group(1))}",
                    "title": title, "depth": 1, "source": "appendix"}

    m = EN_CHAPTER_RE.match(text)
    if m and regex_ok:
        title = clean_title(m.group(2))
        if _is_heading_title(title):
            return {"numbering": m.group(1), "title": title,
                    "depth": 1, "source": "chapter_en"}

    # 2) 数字编号：1 / 1.2 / 1.2.3
    if TextSegConfig["ENABLE_REGEX_FALLBACK"]:
        m = NUM_HEADING_RE.match(text)
        if m:
            raw_num = m.group("num")
            parts = _numeric_parts(raw_num)
            title = clean_title(m.group("title") or m.group("title2") or "")
            has_dot = "." in remove_all_spaces(raw_num) or "．" in raw_num
            depth = len(parts)
            # 单数字标题必须带分隔符/样式；多段编号靠点号即可判定
            looks_numbered = (
                depth >= 2
                or style_lvl is not None
                or (bool(m.group("sep")) and len(text) <= 40)
            )
            # 单数字标题若没有样式背书，要求含中文——
            # 代码行常以 "1 try" / "2 return x" 这类形式出现
            if depth == 1 and style_lvl is None and not CJK_RE.search(title):
                looks_numbered = False
            if (parts and depth <= max_level and looks_numbered
                    and _is_heading_title(title)):
                return {"numbering": ".".join(str(int(p)) for p in parts),
                        "title": title, "depth": depth, "source": "number"}

    # 3) 无编号但样式是标题
    if style_lvl is not None and style_lvl <= max_level:
        title = clean_title(text)
        if _is_heading_title(title):
            return {"numbering": "", "title": title,
                    "depth": style_lvl, "source": "style"}
    return None


def _section_level(depth: int) -> str:
    if depth <= 1:
        return "chapter"
    if depth == 2:
        return "section"
    if depth == 3:
        return "subsection"
    return "subsubsection"


# ---------------------------------------------------------------------------
# 文档遍历（段落 + 表格，按正文顺序）
# ---------------------------------------------------------------------------

BodyItem = Tuple[str, Any, str]   # ("p"|"tbl", obj, text)


def _table_text(table: Table) -> str:
    rows: List[str] = []
    for row in table.rows:
        cells = [re.sub(r"\s+", " ", (c.text or "").strip()) for c in row.cells]
        line = " | ".join(c for c in cells)
        if line.strip(" |"):
            rows.append(line)
    return "\n".join(rows)


def _iter_body(doc: Document) -> Iterator[BodyItem]:
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            par = Paragraph(child, doc)
            raw = par.text or ""
            text = _normalize_spaces(raw).strip()
            if text:
                yield ("p", par, text)
        elif child.tag == qn("w:tbl"):
            table = Table(child, doc)
            text = _table_text(table)
            if text.strip():
                yield ("tbl", table, text)


def _detect_toc_indices(items: List[BodyItem]) -> set[int]:
    if not TextSegConfig["SKIP_TOC"]:
        return set()
    first_heading = None
    for i, (kind, obj, text) in enumerate(items):
        if kind != "p":
            continue
        if _style_level(obj) is not None or CHAPTER_RE.match(text):
            first_heading = i
            break
    limit = first_heading if first_heading is not None else min(len(items), 400)
    toc = {i for i in range(limit)
           if items[i][0] == "p" and _is_toc_line(items[i][1], items[i][2])}
    return toc if len(toc) >= int(TextSegConfig["MIN_TOC_LINES"]) else set()


# ---------------------------------------------------------------------------
# 主解析
# ---------------------------------------------------------------------------

def _new_section(heading: Dict[str, Any], title_path: List[str],
                 start: int, end: int) -> Dict[str, Any]:
    return {
        "level": heading["depth"],
        "numbering": heading["numbering"],
        "title": heading["title"],
        "title_path": title_path,
        "path": " > ".join(title_path),
        "section_level": _section_level(heading["depth"]),
        "heading_source": heading["source"],
        "head_char_start": start,
        "head_char_end": end,
        "blocks": [],
        "content": "",
        "children": [],
        "id": "",
        "section_id": "",
    }


def _append_block(section: Dict[str, Any], block_type: str,
                  text: str, start: int, end: int) -> None:
    section["blocks"].append({
        "block_id": f"b{len(section['blocks']) + 1}",
        "type": block_type,
        "text": text,
        "char_start": start,
        "char_end": end,
    })


def _finalize_content(node: Dict[str, Any]) -> None:
    node["content"] = "\n".join(b["text"] for b in node["blocks"])
    for child in node["children"]:
        _finalize_content(child)


def _compute_spans(node: Dict[str, Any]) -> Tuple[int, int]:
    starts: List[int] = []
    ends: List[int] = []
    if node.get("head_char_start") is not None:
        starts.append(node["head_char_start"])
    if node.get("head_char_end") is not None:
        ends.append(node["head_char_end"])
    for b in node["blocks"]:
        starts.append(b["char_start"])
        ends.append(b["char_end"])
    for child in node["children"]:
        cs, ce = _compute_spans(child)
        starts.append(cs)
        ends.append(ce)
    node["char_start"] = min(starts) if starts else 0
    node["char_end"] = max(ends) if ends else node["char_start"]
    return node["char_start"], node["char_end"]


def _walk(nodes: List[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    for n in nodes:
        yield n
        yield from _walk(n["children"])


def _subtree_has_blocks(node: Dict[str, Any]) -> bool:
    return bool(node["blocks"]) or any(_subtree_has_blocks(c) for c in node["children"])


def _prune_toc_like(nodes: List[Dict[str, Any]], warnings: List[str]) -> int:
    """同一编号出现多次时，删掉「整棵子树都没有正文」的疑似目录条目。"""
    by_num: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for n in _walk(nodes):
        if n["numbering"]:
            by_num[n["numbering"]].append(n)

    drop: set[int] = set()
    for num, items in by_num.items():
        if len(items) < 2:
            continue
        with_blocks = [n for n in items if _subtree_has_blocks(n)]
        if not with_blocks:
            continue
        for n in items:
            if not _subtree_has_blocks(n):
                drop.add(id(n))
                warnings.append(f"[疑似目录行] 编号 {num} 重复，删除无正文标题：{n['title']}")
    if not drop:
        return 0

    def _filter(seq: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for n in seq:
            if id(n) in drop:
                continue
            n["children"] = _filter(n["children"])
            out.append(n)
        return out

    nodes[:] = _filter(nodes)
    return len(drop)


def _assign_ids(nodes: List[Dict[str, Any]], warnings: List[str]) -> None:
    all_nodes = list(_walk(nodes))
    counter = Counter(n["numbering"] for n in all_nodes if n["numbering"])
    used: set[str] = set()

    for n in all_nodes:
        num = n["numbering"]
        if num and counter[num] == 1:
            sid = num
        else:
            if num and counter[num] > 1:
                warnings.append(f"[编号重复] {num} 出现 {counter[num]} 次，改用路径 ID：{n['path']}")
            sid = make_section_id("", n["path"])
        if sid in used:   # 同名路径极少见；用带序号盐的哈希，仍保持 path_ 格式
            k = 2
            while True:
                alt = make_section_id("", f"{n['path']}#{k}")
                if alt not in used:
                    sid = alt
                    break
                k += 1
            warnings.append(f"[路径重复] {n['path']} 使用路径盐 #{k} 消歧")
        used.add(sid)
        n["id"] = sid
        n["section_id"] = sid


def parse_docx(docx_path: Path, doc_id: str = "") -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[str]]:
    doc = Document(str(docx_path))
    items = list(_iter_body(doc))
    toc_indices = _detect_toc_indices(items)

    roots: List[Dict[str, Any]] = []
    stack: List[Dict[str, Any]] = []
    preamble: List[Tuple[str, str, int, int]] = []
    warnings: List[str] = []

    # 规范文本：所有非空段落/表格按顺序拼接，块偏移指向它
    all_texts: List[str] = []
    cursor = 0
    skipped_toc = 0

    for idx, (kind, obj, text) in enumerate(items):
        start = cursor
        cursor += len(text)
        end = cursor
        all_texts.append(text)
        cursor += 1   # 与 doc_text 的 "\n" 对齐

        if kind == "tbl":
            block_type = "table"
            if stack:
                _append_block(stack[-1], block_type, text, start, end)
            else:
                preamble.append((block_type, text, start, end))
            continue

        par: Paragraph = obj
        if idx in toc_indices or _is_toc_style(par):
            skipped_toc += 1
            continue

        heading = _parse_heading(par, text)
        if heading:
            depth = heading["depth"]
            while stack and stack[-1]["level"] >= depth:
                stack.pop()
            parent = stack[-1] if stack else None

            if parent is not None:
                if parent["level"] < depth - 1:
                    warnings.append(
                        f"[层级跳跃] {parent['numbering'] or parent['title']} "
                        f"(L{parent['level']}) → {heading['numbering'] or heading['title']} "
                        f"(L{depth})")
                if (parent["numbering"] and heading["numbering"]
                        and parent["level"] == depth - 1):
                    pnum = parent["numbering"].split(".")
                    cnum = heading["numbering"].split(".")
                    if cnum[:-1] != pnum:
                        warnings.append(
                            f"[编号不连续] 父级 {parent['numbering']}，"
                            f"子级 {heading['numbering']} —— {heading['title']}")

            title_path = [n["title"] for n in stack] + [heading["title"]]
            node = _new_section(heading, title_path, start, end)
            if parent is not None:
                parent["children"].append(node)
            else:
                roots.append(node)
            stack.append(node)
            continue

        block_type = _block_type(par, text)
        if stack:
            _append_block(stack[-1], block_type, text, start, end)
        elif TextSegConfig["KEEP_PREAMBLE"]:
            preamble.append((block_type, text, start, end))

    # 前置内容：没有标题之前的正文
    if preamble:
        if TextSegConfig["KEEP_PREAMBLE"]:
            pre = {
                "level": 1,
                "numbering": "",
                "title": str(TextSegConfig["PREAMBLE_TITLE"]),
                "title_path": [str(TextSegConfig["PREAMBLE_TITLE"])],
                "path": str(TextSegConfig["PREAMBLE_TITLE"]),
                "section_level": "unknown",
                "heading_source": "preamble",
                "head_char_start": None,
                "head_char_end": None,
                "blocks": [],
                "content": "",
                "children": [],
                "id": "",
                "section_id": "",
            }
            for bt, txt, s, e in preamble:
                _append_block(pre, bt, txt, s, e)
            roots.insert(0, pre)
        else:
            warnings.append(f"[前置内容] 丢弃首个标题前的 {len(preamble)} 个正文块")

    dropped = _prune_toc_like(roots, warnings)
    for n in _walk(roots):
        _finalize_content(n)
        _compute_spans(n)
    _assign_ids(roots, warnings)

    doc_text = "\n".join(all_texts)
    stats = {
        "sections": sum(1 for _ in _walk(roots)),
        "by_level": dict(Counter(n["level"] for n in _walk(roots))),
        "blocks": sum(len(n["blocks"]) for n in _walk(roots)),
        "by_block_type": dict(Counter(b["type"] for n in _walk(roots) for b in n["blocks"])),
        "skipped_toc_lines": skipped_toc,
        "dropped_toc_like": dropped,
        "warnings": len(warnings),
        "doc_chars": len(doc_text),
    }
    meta = {
        "schema_version": get_schema().version,
        "doc_id": _sanitize_doc_id(doc_id or docx_path.stem),
        "source": str(docx_path.resolve()),
        "source_sha1": _sha1_file(docx_path),
        "doc_text_sha1": hashlib.sha1(doc_text.encode("utf-8")).hexdigest(),
        "stats": stats,
    }
    return roots, {"meta": meta, "doc_text": doc_text}, warnings


# ---------------------------------------------------------------------------
# I/O & CLI
# ---------------------------------------------------------------------------

def save_toc(sections: List[Dict[str, Any]], meta: Dict[str, Any],
             out_path: Path, doc_text_path: Optional[str]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(meta)
    payload["doc_text_path"] = doc_text_path
    payload["sections"] = sections
    with out_path.open("w", encoding=TextSegConfig["ENCODING"]) as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def save_warnings(warnings: List[str], warn_path: Path) -> None:
    if not TextSegConfig["SAVE_WARNINGS_FILE"] or not warnings:
        return
    warn_path.parent.mkdir(parents=True, exist_ok=True)
    with warn_path.open("w", encoding=TextSegConfig["ENCODING"]) as f:
        for w in warnings:
            f.write(w + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="解析 Word 目录结构（v2：任意层级 + 块级正文）")
    parser.add_argument("--docx", default=str(SCRIPT_DIR / "doc" / TextSegConfig["DOCX_NAME"]))
    parser.add_argument("--out", default=str(SCRIPT_DIR / "output" / TextSegConfig["TOC_NAME"]))
    parser.add_argument("--warn", default=str(SCRIPT_DIR / "output" / TextSegConfig["WARN_NAME"]))
    parser.add_argument("--doc-text", default=None,
                        help="规范纯文本输出路径（默认 output/DOC_TEXT_NAME）")
    parser.add_argument("--doc-id", default="", help="文档 ID（默认取 docx 文件名）")
    parser.add_argument("--max-level", type=int, default=None)
    parser.add_argument("--no-toc-skip", action="store_true")
    parser.add_argument("--no-preamble", action="store_true")
    parser.add_argument("--loglevel", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    logger.setLevel(getattr(logging, args.loglevel))
    if args.max_level is not None:
        TextSegConfig["MAX_LEVEL"] = args.max_level
    if args.no_toc_skip:
        TextSegConfig["SKIP_TOC"] = False
    if args.no_preamble:
        TextSegConfig["KEEP_PREAMBLE"] = False

    in_path = Path(args.docx)
    if not in_path.exists():
        raise FileNotFoundError(f"未找到输入文件：{in_path}")

    out_path = Path(args.out)
    warn_path = Path(args.warn)

    logger.info(f"开始解析：{in_path}")
    sections, extra, warnings = parse_docx(in_path, args.doc_id)

    doc_text_path: Optional[str] = None
    if TextSegConfig["SAVE_DOC_TEXT"]:
        p = Path(args.doc_text) if args.doc_text else (
            out_path.parent / TextSegConfig["DOC_TEXT_NAME"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(extra["doc_text"], encoding=TextSegConfig["ENCODING"])
        doc_text_path = str(p)

    save_toc(sections, extra["meta"], out_path, doc_text_path)
    save_warnings(warnings, warn_path)

    stats = extra["meta"]["stats"]
    logger.info(f"✅ 完成文本分割：{out_path.resolve()}")
    logger.info(f"  章节 {stats['sections']} 个 / 正文块 {stats['blocks']} 个 / "
                f"跳过目录行 {stats['skipped_toc_lines']} 行")
    if sections:
        logger.info(f"  首节：{sections[0]['section_id']}  {sections[0]['title']}")
    if warnings:
        logger.warning(f"共有 {len(warnings)} 条格式告警（非致命）")
        for w in warnings[:int(TextSegConfig["WARNINGS_PRINT_TOP"])]:
            logger.warning("  - " + w)
        if len(warnings) > int(TextSegConfig["WARNINGS_PRINT_TOP"]):
            logger.warning("  ...（其余已省略；完整内容见文件：%s）", warn_path.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
