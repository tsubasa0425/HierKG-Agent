import argparse
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from string import Template
import yaml
from openai import OpenAI
from typing import Dict, List, Any, Optional
from tqdm import tqdm

# -------- 基础工具 --------
def load_yaml(p: Path) -> dict:
    with p.open('r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}

def load_additional_configs(include_files: list, base_dir: Path) -> dict:
    """
    逐个把 include 的 yaml 合并到一个 dict 里。
    include 的路径相对 base_dir（即 config.yaml 所在目录）。
    """
    merged = {}
    for rel in include_files or []:
        rel = str(rel)
        inc_path = Path(rel)
        if not inc_path.is_absolute():
            inc_path = (base_dir / rel).resolve()
        # 可选：如果写成了 "config/xxx.yaml"，去掉多余的前缀
        if not inc_path.exists() and rel.startswith("config/"):
            inc_path = (base_dir / rel.split("/", 1)[1]).resolve()
        if not inc_path.exists():
            raise FileNotFoundError(f"找不到包含文件：{inc_path}")
        merged.update(load_yaml(inc_path))
    return merged

# -------- 先确定脚本 & 配置路径 --------
script_dir = Path(__file__).resolve().parent
project_root = script_dir.parent.parent  # = 项目根目录
config_file = script_dir / "config" / "config.yaml"
config_dir = config_file.parent  # = KGBuild/config
schema_file = script_dir / "config" / "schema.yaml"  # src/KGBuild/config/schema.yaml

# -------- 加载主配置 + 合并 include --------
config = load_yaml(config_file)
additional_config = load_additional_configs(config.get('include_files', []), base_dir=config_dir)
config.update(additional_config)

# -------- 加载 L0 schema 并渲染 prompt 占位符 --------
def render_entity_types(schema: Dict[str, Any]) -> str:
    """从 schema.EntityTypes 渲染成 prompt 可读的带描述列表"""
    et = schema.get("EntityTypes", {}) or {}
    lines = []
    for type_name, meta in et.items():
        desc = meta.get("description", "") or ""
        ex = meta.get("examples", []) or []
        ex_str = f"，如：{', '.join(str(x) for x in ex)}" if ex else ""
        lines.append(f"- {type_name}：{desc}{ex_str}")
    return "\n".join(lines)


def render_relation_types(schema: Dict[str, Any]) -> str:
    """从 schema.RelationTypes.entity_relations 渲染成 prompt 可读的带描述列表"""
    rt = (schema.get("RelationTypes", {}) or {}).get("entity_relations", {}) or {}
    lines = []
    for type_name, meta in rt.items():
        desc = meta.get("description", "") or ""
        ex = meta.get("example", "") or ""
        ex_str = f"。例：{ex}" if ex else ""
        lines.append(f"- {type_name}：{desc}{ex_str}")
    return "\n".join(lines)


def _render_prompts_with_schema():
    """读 schema.yaml，把实体类型和关系类型注入 prompt 模板。
    第一阶段只替换 ${entity_types_with_desc} / ${relation_types_with_desc}，
    调用时再用 Template.substitute 替换 ${section_text} / ${entity_list}。
    用 Template 而不是 str.format 是为了避免 JSON 示例里的大括号冲突。
    """
    if not schema_file.exists():
        raise FileNotFoundError(f"L0 schema 文件不存在：{schema_file}")
    schema = load_yaml(schema_file) or {}
    et_list = render_entity_types(schema)
    rt_list = render_relation_types(schema)

    ent_tpl = Template(config['ExtractionConfig']['ENTITY_PROMPT'])
    rel_tpl = Template(config['ExtractionConfig']['RELATION_PROMPT'])
    try:
        ent_prompt = ent_tpl.safe_substitute(
            entity_types_with_desc=et_list,
            relation_types_with_desc="",
            section_text="${section_text}",
            entity_list="${entity_list}",
        )
        rel_prompt = rel_tpl.safe_substitute(
            entity_types_with_desc="",
            relation_types_with_desc=rt_list,
            section_text="${section_text}",
            entity_list="${entity_list}",
        )
    except KeyError as e:
        raise RuntimeError(f"prompt 模板占位符不匹配，缺少：{e}")
    config['ExtractionConfig']['ENTITY_PROMPT'] = ent_prompt
    config['ExtractionConfig']['RELATION_PROMPT'] = rel_prompt
    return schema, et_list, rt_list


schema, entity_types_prompt, relation_types_prompt = _render_prompts_with_schema()

# ===== 类型归一化（从 schema.yaml 的 TypeMapping 加载）=====
_SCHEMA_ENTITY_TYPES = set(schema.get("EntityTypes", {}).keys())
_TYPE_MAPPING_RULES = []
for rule in (schema or {}).get("TypeMapping", []) or []:
    kw = rule.get("keywords", [])
    st = rule.get("schema_type", "")
    if kw and st:
        _TYPE_MAPPING_RULES.append((kw, st))

def _normalize_entity_type(raw_type: str) -> str:
    """将 LLM 输出的 type 归一化到 schema 定义的类型。
    1. 如果已在 schema 中 → 原样返回
    2. 按 TypeMapping 关键词匹配 → 返回映射的 schema 类型
    3. 都不匹配 → 返回 "概念"（兜底）
    """
    t = (raw_type or "").strip()
    if t in _SCHEMA_ENTITY_TYPES:
        return t
    for keywords, schema_type in _TYPE_MAPPING_RULES:
        if any(kw in t for kw in keywords):
            return schema_type
    return "概念"  # 兜底

# ===== OpenAI 配置 =====
client = OpenAI(
    api_key=config['APIConfig']['API_KEY'],
    base_url=config['APIConfig']['API_BASE']
)
MODEL_NAME = config['APIConfig']['MODEL_NAME']

# ===== 速率限制（可选）=====
_rate_lock = threading.Lock()
_last_ts = 0.0
_min_interval = (1.0 / config['APIConfig']['RATE_LIMIT_QPS']) if config['APIConfig']['RATE_LIMIT_QPS'] > 0 else 0.0

def _rate_limit_block():
    """简单全局限速：保证两次请求间隔 >= 1/QPS"""
    if _min_interval <= 0:
        return
    global _last_ts
    with _rate_lock:
        now = time.time()
        delta = now - _last_ts
        if delta < _min_interval:
            time.sleep(_min_interval - delta)
        _last_ts = time.time()

# ===== 工具函数 =====
def clean_text(text: str) -> str:
    """
    清理文本中的空格、制表符、换行符等。
    """
    if text is None:
        return ""
    text = text.strip().replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text

def _strip_code_fences(s: str) -> str:
    # 兼容模型偶尔输出 ```json ... ```
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()

def safe_json_loads(content: str) -> Dict:
    try:
        content = _strip_code_fences(content)
        return json.loads(content)
    except Exception:
        return {}

def _chat_once(prompt: str) -> str:
    _rate_limit_block()  # QPS 控制（可关）
    if config['APIConfig']['EXTRA_THROTTLE_SEC'] > 0:  # 额外固定节流（可关）
        time.sleep(config['APIConfig']['EXTRA_THROTTLE_SEC'])
    resp = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        temperature=config['ExtractionConfig']['TEMPERATURE'],
        timeout=config['APIConfig']['TIMEOUT_SECS'],
    )
    text = resp.choices[0].message.content.strip()
    # ✅ 去除 <think> ... </think> 的部分
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text.strip()

def chat_with_retry(prompt: str) -> str:
    last_err: Optional[Exception] = None
    for k in range(config['APIConfig']['RETRIES']):
        try:
            # print(f'第 {k} 次尝试，提示内容：{prompt}')
            response = _chat_once(prompt)
            # print(f'第 {k} 次尝试成功，响应内容：{response}')
            return response
            # return _chat_once(prompt)
        except Exception as e:
            # print(f'第 {k} 次尝试失败，异常信息：{e}')
            last_err = e
            backoff = (config['APIConfig']['RETRY_BACKOFF_BASE'] ** k)
            time.sleep(backoff)
    raise RuntimeError(f"LLM 请求失败（已重试 {config['APIConfig']['RETRIES']} 次）: {last_err}")

def call_openai_json(prompt: str) -> Dict:
    try:
        content = chat_with_retry(prompt)
        # print(f'LLM原始输出：{content}')
        return safe_json_loads(content)
    except Exception:
        return {}

# ===== 抽取逻辑 =====
def extract_entities(section_text: str) -> List[Dict]:
    section_text = clean_text(section_text)
    prompt = Template(config['ExtractionConfig']['ENTITY_PROMPT']).safe_substitute(
        section_text=section_text
    )
    data = call_openai_json(prompt)
    ents = data.get("entities", [])
    # 轻量清洗：去除空名；alias 正规化为空数组
    cleaned = []
    for e in ents if isinstance(ents, list) else []:
        name = (e.get("name") or "").strip()
        if not name:
            continue
        alias = e.get("alias") or []
        if isinstance(alias, str):
            alias = [alias] if alias.strip() else []
        cleaned.append({
            "name": name,
            "alias": [a for a in alias if isinstance(a, str) and a.strip()],
            "type": _normalize_entity_type((e.get("type") or "").strip()),
            "raw_content": (e.get("raw_content") or "").strip(),
        })
    return cleaned

def extract_relations(section_text: str, entities: List[Dict]) -> List[Dict]:
    section_text = clean_text(section_text)
    names = [e.get("name", "").strip() for e in entities if e.get("name")]
    entity_list = ", ".join([n for n in names if n])
    if not entity_list:
        return []
    prompt = Template(config['ExtractionConfig']['RELATION_PROMPT']).safe_substitute(
        section_text=section_text,
        entity_list=entity_list
    )
    data = call_openai_json(prompt)
    if isinstance(data, list):
        rels = data
    elif isinstance(data, dict):
        rels = data.get("relations", [])
    else:
        rels = []
    cleaned = []
    for r in rels if isinstance(rels, list) else []:
        src = (r.get("source") or "").strip()
        tgt = (r.get("target") or "").strip()
        if not src or not tgt:
            continue
        cleaned.append({
            "source": src,
            "target": tgt,
            "type": (r.get("type") or "").strip(),
            "description": (r.get("description") or "").strip(),
            "keywords": (r.get("keywords") or []),
        })
    return cleaned


def process_one_subsection(section_text: str) -> Dict:
    ents = extract_entities(section_text)
    if not ents:
        return {"entities": [], "relations": []}
    rels = extract_relations(section_text, ents)
    return {"entities": ents, "relations": rels}

def collect_subsections(toc: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """适配 2-4 层目录，自动识别最底层含正文的节点。"""
    subs: List[Dict[str, Any]] = []
    for level1 in toc:  # 第1层（如“第2章 MATLAB基础知识”）
        # 遍历第2层（level1的子节点，如“2.1 数据类型”）
        for level2 in level1.get("children", []) or []:
            # 检查第2层是否有子节点（判断是否为2层目录）
            if not level2.get("children"):  # 无子节点 → 2层目录，直接收集level2
                if level2.get("content"):
                    subs.append(level2)
            else:  # 有子节点 → 至少3层目录，继续遍历第3层
                for level3 in level2.get("children", []) or []:
                    # 检查第3层是否有子节点（判断是否为3层/4层目录）
                    if not level3.get("children"):  # 无子节点 → 3层目录，收集level3
                        if level3.get("content"):
                            subs.append(level3)
                    else:  # 有子节点 → 4层目录，遍历并收集第4层
                        for level4 in level3.get("children", []) or []:
                            if level4.get("content"):
                                subs.append(level4)
    return subs

# ===== 主流程（并发）=====
def run(max_workers: int):
    # 获取 src/KGBuild 目录
    script_dir = Path(__file__).resolve().parent

    # 获取配置文件中的文件名
    in_json_filename = config['ExtractionConfig']['IN_NAME']
    out_json_filename = config['ExtractionConfig']['OUT_NAME']

    # 构建输入和输出路径
    in_json = script_dir / "output" / in_json_filename
    out_json = script_dir / "output" / out_json_filename

    # 检查输入文件是否存在
    if not in_json.exists():
        raise FileNotFoundError(f"未找到输入：{in_json}")

    # 读取输入文件并进行处理
    with in_json.open("r", encoding="utf-8") as f:
        toc: List[Dict[str, Any]] = json.load(f)

    subsections = collect_subsections(toc)
    total = len(subsections)

    if total == 0:
        # 直接输出空结构或原结构
        with out_json.open("w", encoding="utf-8") as f:
            json.dump(toc, f, ensure_ascii=False, indent=2)
        print(f"✅ 无需抽取（没有找到含摘要的小节）。已写出：{out_json.resolve()}")
        return

    pbar = tqdm(total=total, desc="🔍 实体与关系提取", ncols=90)

    def worker(subnode: Dict[str, Any]):
        res = process_one_subsection(subnode.get("content", ""))
        subnode["entities"] = res["entities"]
        subnode["relations"] = res["relations"]
        pbar.update(1)

    # 并发执行
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(worker, sub) for sub in subsections]
        for fu in as_completed(futures):
            # 触发异常立即抛出，方便定位
            fu.result()

    pbar.close()

    out_json.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(toc, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 实体与关系提取完成：{out_json.resolve()}")

def main():
    ap = argparse.ArgumentParser(description="从章节摘要中并发抽取实体与关系")
    ap.add_argument("--workers", type=int, default=config['APIConfig']['WORKERS'],
                    help="并发线程数（默认见 APIConfig.WORKERS）")
    args = ap.parse_args()

    run(max_workers=args.workers)

if __name__ == "__main__":
    main()
