import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import yaml
from docx import Document
from docx.text.paragraph import Paragraph
from tqdm import tqdm

# ===== 配置加载（相对 config.yaml 解析 include）=====

def _load_yaml(p: Path) -> dict:
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def _load_additional_configs(include_files: List[str], base_dir: Path) -> dict:
    merged: Dict[str, Any] = {}
    for rel in include_files or []:
        rel_str = str(rel)
        inc_path = Path(rel_str)
        if not inc_path.is_absolute():
            inc_path = (base_dir / rel_str).resolve()
        # 兼容写成 "config/xxx.yaml" 的情况：退化为同目录查找
        if not inc_path.exists() and rel_str.startswith("config/"):
            inc_path = (base_dir / rel_str.split("/", 1)[1]).resolve()
        if not inc_path.exists():
            raise FileNotFoundError(f"找不到包含文件：{inc_path}")
        merged.update(_load_yaml(inc_path))
    return merged

# 脚本所在目录：src/KGBuild
script_dir = Path(__file__).resolve().parent

# 主配置：src/KGBuild/config/config.yaml
config_file = script_dir / "config" / "config.yaml"
config_dir = config_file.parent  # = src/KGBuild/config

# 读取主配置并合并 include
config = _load_yaml(config_file)
additional = _load_additional_configs(config.get("include_files", []), base_dir=config_dir)
config.update(additional)

# 提取子配置
TextSegConfig: Dict[str, Any] = config["TextSegConfig"]
# print(TextSegConfig)

# ===== 日志 =====
logger = logging.getLogger("TextSegmentation")
_handler = logging.StreamHandler(sys.stdout)  # 输出到标准输出
_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)

#  ============= 正则与工具 =============

HAS_LETTER_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z]")

# 从配置编译正则
CHAPTER_RE = re.compile(TextSegConfig["CHAPTER_PATTERN"])
SECTION_RE = re.compile(TextSegConfig["SECTION_PATTERN"])
SUBSECTION_RE = re.compile(TextSegConfig["SUBSECTION_PATTERN"])
POINT_RE = re.compile(TextSegConfig["POINT_PATTERN"])

CN_NUM_MAP = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "百": 100,
}


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
            tmp = 0
            seen = True
        elif ch == "十":
            total += (tmp if tmp else 1) * 10
            tmp = 0
            seen = True
        else:
            v = CN_NUM_MAP.get(ch)
            if v is None:
                try:
                    return int(s)
                except Exception:
                    return 1
            tmp = v
            seen = True
    if seen:
        total += tmp
        return total or 1
    try:
        return int(s)
    except Exception:
        return 1


def _normalize_spaces(text: str) -> str:
    if not text:
        return ""
    if TextSegConfig["NORMALIZE_SPACES"]:
        for ch in TextSegConfig["SPACE_SUBSTITUTIONS"]:
            text = text.replace(ch, " ")
        text = re.sub(r"\s{2,}", " ", text)
    return text


def clean_title(t: str) -> str:
    """去尾部点线+页码、两端空白/冒号；压缩奇异空格。"""
    if not t:
        return ""
    t = _normalize_spaces(t)
    if TextSegConfig["REMOVE_TRAILING_PAGE_NO"]:
        t = re.sub(r"[\.·・—\-＿\s　]*\d+\s*$", "", t)
    if TextSegConfig["STRIP_COLONS"]:
        t = t.strip(" ：:　\t")
    return t.strip()


def pick_level_by_style(par: Paragraph) -> Optional[int]:
    """从段落样式/outlineLvl/编号层级推断层级（1..4）。"""
    try:
        name = (par.style.name or "").strip().lower()
        lvl = TextSegConfig["HEADING_MAP"].get(name)
        # print(f"样式：{name} -> {lvl}")
        if lvl:
            print(f"{par[:10]} 样式：{name} -> {lvl}")
            return lvl
    except Exception:
        pass

    try:
        el = par._p.xpath("./w:pPr/w:outlineLvl")
        print(f"{par[:10]} outlineLvl：{el}")
        if el:
            # print(f"{par[:10]} outlineLvl：{el}")
            lvl = int(el[0].val)
            if 0 <= lvl <= 3:
                return lvl + 1
    except Exception:
        pass

    try:
        ilvl = par._p.xpath("./w:pPr/w:numPr/w:ilvl")
        # print(f"ilvl：{ilvl}")
        if ilvl:
            lvl = int(ilvl[0].val)
            if 0 <= lvl <= 3:
                return lvl + 1
    except Exception:
        pass

    return None


def _parse_chapter_no(ch_id: str) -> int:
    """
    从章节 id 里安全解析章号：
    支持：'1章' / '第1章' / '第一章' / '1' / ' 1 章 '
    """
    s = (ch_id or "").strip()
    # 去掉尾部“章”
    s = s.rstrip("章").strip()
    # 去掉开头“第”
    s = s.lstrip("第").strip()
    # 去掉所有空白
    s = re.sub(r"\s+", "", s)
    # 委托中文转阿拉伯
    return cn2int(s)




def is_code_like(text: str) -> bool:
    """检测文本是否像代码段落，返回 True 表示是代码。防止和普通文本混淆。"""
    if not text:
        return False
    
    code_indicators = [
        r',',           # 逗号（CSV、参数列表）
        r'=',           # 赋值
        r'["\']',     # 引号
        r'[()]',        # 括号

        r'^\s*#',      # 行首注释（如 # comment）
        r'\s+#\s*[a-zA-Z\u4e00-\u9fa5]',  # # 后跟中英文字符（避免 #1、#2）
        
        r'(?<=^)[+\-*/](?=\s|$)|(?<=\s)[+\-*/](?=\s|$)' ,  # 只匹配前后是空格或行首/行尾的运算符
        r'f["\']',    # f-string
        r'\\n',       # 换行符
        r'\{',         # 花括号
        r'\[',         # 方括号
        r':\s*\d+',   # 冒号后跟数字（如时间 12:30）
        
        r'\bexcept\b',  # 异常处理
        r'\bfrom\b',   # 导入语句
        r'\bimport\b', # 导入语句
        r'\bclass\b',  # 类定义
        r'\bdef\b',    # 函数定义
        r'\bif\b',     # 条件语句
        r'\bfor\b',    # 循环语句
        r'\bwhile\b',  # 循环语句
        r'\btry\b',    # 异常处理
        r'\bwith\b',   # 上下文管理器
        r'\bas\b',     # 异常别名
        r'->',          # 类型注解
        r'@'            # 装饰器
    ]
    
    for pattern in code_indicators:
        if re.search(pattern, text):
            return True
    
    return False


def remove_all_spaces(text: str) -> str:
    """去除所有类型的空格（包括全角空格）。"""
    if not text:
        return ""
    return re.sub(r"[\s　]+", "", text)



# ============= 解析主逻辑 =============

def _append_content(cur_ch, cur_sec, cur_sub, text: str):
    """将正文段落追加到当前最深层级节点的 content 字段。
    优先级：subsection > section > chapter。
    """
    target = cur_sub or cur_sec or cur_ch
    if target is None:
        return
    if "content" not in target:
        target["content"] = ""
    if target["content"]:
        target["content"] += "\n" + text
    else:
        target["content"] = text


def parse_docx(docx_path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    doc = Document(str(docx_path))
    toc: List[Dict[str, Any]] = []

    cur_ch = None  # level=1
    cur_sec = None  # level=2
    cur_sub = None  # level=3

    warnings: List[str] = []

    index = 0

    for para in doc.paragraphs:
        raw = (para.text or "").strip()
        if not raw:
            continue

        text_norm = _normalize_spaces(raw).strip()
        # print(f'text_norm：{text_norm}')
        matched = False

        lvl = pick_level_by_style(para) if TextSegConfig["USE_STYLE_FIRST"] else None

        # ========= 样式优先分支 =========
        if lvl == 1 and TextSegConfig["MAX_LEVEL"] >= 1:
            m = CHAPTER_RE.match(text_norm)
            if m:
                num_raw, title = m.group(1), clean_title(m.group(2))
                num_raw = remove_all_spaces(num_raw)
                title = remove_all_spaces(title)

                if HAS_LETTER_RE.search(title) and not is_code_like(title):
                    node = {
                        "level": 1,
                        # 统一为 "数字+章" 格式，示例："1章"
                        "id": f"{cn2int(num_raw)}章",
                        "title": title,
                        "children": [],
                    }
                    toc.append(node)
                    cur_ch, cur_sec, cur_sub = node, None, None
                    matched = True

        elif lvl == 2 and cur_ch and TextSegConfig["MAX_LEVEL"] >= 2:
            m = SECTION_RE.match(text_norm)
            if m:
                full, ch, sec, title = (
                    m.group(1),
                    m.group(2),
                    m.group(3),
                    clean_title(m.group(4)),
                )
                full = remove_all_spaces(full)
                ch = remove_all_spaces(ch)
                sec = remove_all_spaces(sec)
                title = remove_all_spaces(title)

                if HAS_LETTER_RE.search(title):
                    exp_ch = str(_parse_chapter_no(cur_ch["id"]))
                    if ch == exp_ch:
                        node = {"level": 2, "id": full, "title": title, "children": []}
                        cur_ch["children"].append(node)
                        cur_sec, cur_sub = node, None
                        matched = True
                    else:
                        warnings.append(
                            f"[节章号不一致] 期望{exp_ch}.x，实际{full} —— 段落：{text_norm}"
                        )

        elif lvl == 3 and cur_sec and TextSegConfig["MAX_LEVEL"] >= 3:
            m = SUBSECTION_RE.match(text_norm)
            if m:
                full, title = m.group(1), clean_title(m.group(5))
                full = remove_all_spaces(full)
                title = remove_all_spaces(title)
                if HAS_LETTER_RE.search(title) and full.startswith(cur_sec["id"] + "."):
                    node = {"level": 3, "id": full, "title": title, "children": []}
                    cur_sec["children"].append(node)
                    cur_sub = node
                    matched = True
                else:
                    warnings.append(
                        f"[小节前缀不匹配] 期望前缀{cur_sec['id']}., 实际{full}"
                    )

        elif lvl == 4 and cur_sub and TextSegConfig["MAX_LEVEL"] >= 4:
            m = POINT_RE.match(text_norm)
            if m:
                full, title = m.group(1), clean_title(m.group(6))
                full = remove_all_spaces(full)
                title = remove_all_spaces(title)
                if HAS_LETTER_RE.search(title) and full.startswith(cur_sub["id"] + "."):
                    node = {"level": 4, "id": full, "title": title}
                    cur_sub["children"].append(node)
                    matched = True
                else:
                    warnings.append(
                        f"[知识点前缀不匹配] 期望前缀{cur_sub['id']}., 实际{full}"
                    )

        if matched:
            continue

        # ========= 正则兜底分支 =========
        if not TextSegConfig["ENABLE_REGEX_FALLBACK"]:
            # 非标题段落：追加到当前节点的 content
            _append_content(cur_ch, cur_sec, cur_sub, text_norm)
            continue


        """修改下匹配顺序，防止章匹配到节"""
        """
            新的匹配顺序：
            1. 知识点（4个数字） - 最具体，优先匹配
            2. 小节（3个数字）
            3. 节（2个数字）
            4. 章（1个数字） - 最后匹配
        """

        # —— 知识点 —— #
        m = POINT_RE.match(text_norm)
        
        if m and cur_sub and TextSegConfig["MAX_LEVEL"] >= 4:
            print(f'line 341 知识点匹配: m={m}, text_norm={text_norm}')
            full, title = m.group(1), clean_title(m.group(6))
            full = remove_all_spaces(full)
            title = remove_all_spaces(title)
            if HAS_LETTER_RE.search(title) and full.startswith(cur_sub["id"] + ".") and not is_code_like(title):
                node = {"level": 4, "id": full, "title": title}
                cur_sub["children"].append(node)
            else:
                warnings.append(
                    f"[知识点前缀不匹配] 期望前缀{cur_sub['id']}., 实际{full}， 原文：{text_norm}"
                )
            continue

        
        # —— 小节 —— #
        m = SUBSECTION_RE.match(text_norm)
        
        if m and cur_sec and TextSegConfig["MAX_LEVEL"] >= 3:
            print(f'line 332 小节匹配: m={m}, text_norm={text_norm}')
            full, title = m.group(1), clean_title(m.group(5))
            full = remove_all_spaces(full)
            title = remove_all_spaces(title)
            if HAS_LETTER_RE.search(title) and full.startswith(cur_sec["id"] + ".") and not is_code_like(title):
                node = {"level": 3, "id": full, "title": title, "children": []}
                cur_sec["children"].append(node)
                cur_sub = node
            else:
                warnings.append(
                    f"[小节前缀不匹配] 期望前缀{cur_sec['id']}., 实际{full}， 原文：{text_norm}"
                )
            continue



        # —— 节 —— #
        m = SECTION_RE.match(text_norm)

        if m and cur_ch and TextSegConfig["MAX_LEVEL"] >= 2:
            print(f'line 306 节匹配: m={m}, text_norm={text_norm}')
            full, ch, sec, title = (
                m.group(1),
                m.group(2),
                m.group(3),
                clean_title(m.group(4)),
            )
            full = remove_all_spaces(full)
            ch = remove_all_spaces(ch)
            sec = remove_all_spaces(sec)
            title = remove_all_spaces(title)
            if HAS_LETTER_RE.search(title) and not is_code_like(title):
                exp_ch = str(_parse_chapter_no(cur_ch["id"]))
                if ch == exp_ch:
                    node = {"level": 2, "id": full, "title": title, "children": []}
                    cur_ch["children"].append(node)
                    cur_sec, cur_sub = node, None
                else:
                    # print(f'[节章号不一致] text_norm={text_norm}')
                    warnings.append(f"[节章号不一致] 期望{exp_ch}.x，实际{full}， 原文：{text_norm}")
            continue


        # —— 章 —— #
        m = CHAPTER_RE.match(text_norm)

        if m and TextSegConfig["MAX_LEVEL"] >= 1:
            print(f'line 284 章匹配: m={m}, text_norm={text_norm}')
            num_raw, title = m.group(1), clean_title(m.group(2))
            num_raw = remove_all_spaces(num_raw)
            title = remove_all_spaces(title)
            if HAS_LETTER_RE.search(title) and not is_code_like(title):
                # 兜底也统一为 "数字+章"
                node = {
                    "level": 1,
                    "id": f"{cn2int(num_raw)}章",
                    "title": title,
                    "children": [],
                }
                toc.append(node)
                cur_ch, cur_sec, cur_sub = node, None, None
            continue

        # 所有标题匹配都未命中：这是正文段落，追加到当前节点
        _append_content(cur_ch, cur_sec, cur_sub, text_norm)

        index += 1
        # if index > TextSegConfig["MAX_PARAGRAPH"]:
        #     warnings.append(f"[段落数超过上限] 已解析{index}段落，上限{TextSegConfig['MAX_PARAGRAPH']}")
        #     break

    return toc, warnings

#  ============= I/O & CLI =============

def save_toc(toc: List[Dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding=TextSegConfig["ENCODING"]) as f:
        json.dump(toc, f, ensure_ascii=False, indent=2)


def save_warnings(warnings: List[str], warn_path: Path) -> None:
    if not TextSegConfig["SAVE_WARNINGS_FILE"] or not warnings:
        return
    warn_path.parent.mkdir(parents=True, exist_ok=True)
    with warn_path.open("w", encoding=TextSegConfig["ENCODING"]) as f:
        for w in warnings:
            f.write(w + "\n")


def main():
    docx_path = script_dir / "doc" / TextSegConfig["DOCX_NAME"]
    toc_path = script_dir / "output" / TextSegConfig["TOC_NAME"]
    warn_path = script_dir / "output" / TextSegConfig["WARN_NAME"]
    parser = argparse.ArgumentParser(description="解析 Word 目录结构（章-节-小节-知识点）")
    parser.add_argument(
        "--docx",
        type=str,
        default=str(docx_path),
        help="输入 Word 文件路径（默认 doc 文件夹中的 DOCX_NAME）",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=str(toc_path),
        help="输出 JSON 路径（默认 output 文件夹中的 TOC_NAME）",
    )
    parser.add_argument(
        "--warn",
        type=str,
        default=str(warn_path),
        help="告警日志路径（默认 output 文件夹中的 WARN_NAME）",
    )
    parser.add_argument(
        "--loglevel",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logger.setLevel(getattr(logging, args.loglevel))

    in_path = Path(args.docx)
    if not in_path.exists():
        raise FileNotFoundError(f"未找到输入文件：{in_path}")

    out_path = Path(args.out)
    warn_path = Path(args.warn)

    logger.info(f"开始解析：{in_path}")
    toc, warnings = parse_docx(in_path)
    save_toc(toc, out_path)
    save_warnings(warnings, warn_path)

    logger.info(f"✅ 完成文本分割：{out_path.resolve()}")
    if toc:
        logger.info(f"首章：{toc[0].get('id')}  {toc[0].get('title')}")
    if warnings:
        n = len(warnings)
        logger.warning(f"共有 {n} 条格式告警（非致命）")
        for w in warnings[: TextSegConfig["WARNINGS_PRINT_TOP"]]:
            logger.warning("  - " + w)
        if n > TextSegConfig["WARNINGS_PRINT_TOP"]:
            logger.warning("  ...（其余已省略；完整内容见文件：%s）", warn_path.name)


if __name__ == "__main__":
    main()
