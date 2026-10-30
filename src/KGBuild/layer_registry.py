# -*- coding: utf-8 -*-
"""分层决策登记表 —— LLM 决策复用 + 人工覆写 + 翻转审计。

设计约定（2026-10 与需求方确认）：
  * 规则决策不缓存：规则求值是纯计算，每次构建重新求值，policy 改动立即生效
  * LLM 决策只在 policy_hash / prompt_version / model_id 全部一致时复用
  * 人工覆写以 config/layer_overrides.yaml 为唯一权威，登记表只做审计记录
  * 登记表全量记录 rule / llm / cache / heuristic / human / excluded，
    用于统计兜底率、检测分层翻转

优先级：硬约束（待定 → excluded） > 人工覆写 > 规则 > LLM/登记表
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger("LayerRegistry")

REGISTRY_VERSION = "1.0"
VALID_OVERRIDE_LAYERS = ("L1", "L2")

# 登记表里保留的信号快照字段（degree 在关系装配后才算出，不属于分层信号）
SIGNAL_KEYS = (
    "cross_section_coverage",
    "occurrence_count",
    "definitional_cue",
    "has_definition",
    "heading_presence",
    "proper_instance",
)


def decision_key(name: str, node_type: str) -> str:
    """按（规范名, 类型）生成稳定键；名称改名/改类型即视为新决策。"""
    raw = f"{(name or '').strip()}|{(node_type or '').strip()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 人工覆写
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Override:
    name: str
    layer: str
    node_type: str = ""
    reason: str = ""
    owner: str = ""
    updated_at: str = ""


class LayerOverrides:
    """layer_overrides.yaml 的只读索引。

    支持两种写法：

        overrides:
          - name: 强化学习
            type: 学习方法        # 可省略，省略时匹配任意 type
            layer: L1
            reason: 教材核心概念

        overrides:
          强化学习: L1            # 简写，匹配任意 type
    """

    def __init__(self, by_full: Optional[Dict[Tuple[str, str], Override]] = None,
                 by_any_name: Optional[Dict[str, Override]] = None,
                 issues: Optional[List[str]] = None):
        self.by_full = by_full or {}
        self.by_any_name = by_any_name or {}
        self.issues = issues or []

    @classmethod
    def load(cls, path: Path) -> "LayerOverrides":
        issues: List[str] = []
        by_full: Dict[Tuple[str, str], Override] = {}
        by_any: Dict[str, Override] = {}
        if not path.exists():
            return cls(by_full, by_any, issues)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001 - 配置错误不阻断构建，降级为空覆写
            issues.append(f"无法读取 {path.name}：{e}")
            return cls(by_full, by_any, issues)

        items = raw.get("overrides")
        if items is None:
            items = raw.get("layer_overrides") or []
        if isinstance(items, dict):
            expanded: List[Dict[str, Any]] = []
            for name, value in items.items():
                if isinstance(value, str):
                    expanded.append({"name": name, "layer": value})
                elif isinstance(value, dict):
                    d = dict(value)
                    d.setdefault("name", name)
                    expanded.append(d)
                else:
                    issues.append(f"覆写 {name!r} 的值必须是 L1/L2 或对象，已忽略")
            items = expanded
        if not isinstance(items, list):
            issues.append("overrides 必须是列表或映射，已忽略")
            return cls(by_full, by_any, issues)

        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                issues.append(f"第 {idx + 1} 条覆写不是对象，已忽略")
                continue
            name = str(item.get("name") or "").strip()
            layer = str(item.get("layer") or "").strip().upper()
            node_type = str(item.get("type") or "").strip()
            if not name:
                issues.append(f"第 {idx + 1} 条覆写缺少 name，已忽略")
                continue
            if layer not in VALID_OVERRIDE_LAYERS:
                issues.append(f"覆写 {name!r} 的 layer={layer!r} 非法（只允许 L1/L2），已忽略")
                continue
            ov = Override(
                name=name,
                layer=layer,
                node_type=node_type,
                reason=str(item.get("reason") or ""),
                owner=str(item.get("owner") or ""),
                updated_at=str(item.get("updated_at") or ""),
            )
            if node_type:
                key = (name, node_type)
                if key in by_full:
                    issues.append(f"覆写 {name!r}（{node_type}）重复，后者覆盖前者")
                by_full[key] = ov
            else:
                if name in by_any:
                    issues.append(f"覆写 {name!r} 重复，后者覆盖前者")
                by_any[name] = ov
        return cls(by_full, by_any, issues)

    def resolve(self, name: str, node_type: str) -> Optional[Override]:
        """带 type 的覆写优先；无名类型覆写作为兜底。"""
        return self.by_full.get((name, node_type)) or self.by_any_name.get(name)

    def __len__(self) -> int:
        return len(self.by_full) + len(self.by_any_name)


# ---------------------------------------------------------------------------
# 登记表
# ---------------------------------------------------------------------------

class LayerRegistry:
    """layer_registry.json 的读写与查询。

    不缓存 rule 决策；只有 source=llm 且三项指纹一致的条目会被复用。
    """

    def __init__(self, path: Path, policy_hash: str, prompt_version: str,
                 model_id: str, build_id: str = "", enabled: bool = True):
        self.path = path
        self.policy_hash = policy_hash
        self.prompt_version = prompt_version
        self.model_id = model_id
        self.build_id = build_id
        self.enabled = enabled
        self.data: Dict[str, Any] = {
            "registry_version": REGISTRY_VERSION,
            "policy_hash": policy_hash,
            "prompt_version": prompt_version,
            "model_id": model_id,
            "build_id": build_id,
            "updated_at": "",
            "decisions": {},
        }
        self.previous: Dict[str, Dict[str, Any]] = {}
        self.seen: set[str] = set()
        self.flips: List[Dict[str, Any]] = []

    # -- 读取 -------------------------------------------------------------

    def load(self) -> None:
        if not self.enabled or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 - 坏登记表降级为空，不阻断构建
            logger.warning(f"登记表读取失败，按空表处理：{e}")
            return
        if not isinstance(raw, dict) or not isinstance(raw.get("decisions"), dict):
            logger.warning("登记表结构不合法，按空表处理")
            return

        previous = {str(k): v for k, v in raw["decisions"].items() if isinstance(v, dict)}
        self.previous = previous
        self.data["decisions"] = dict(previous)
        if raw.get("registry_version") != REGISTRY_VERSION:
            logger.warning(
                f"登记表版本 {raw.get('registry_version')!r} 与当前 {REGISTRY_VERSION} 不一致，"
                "仍按字段兼容读取")
        old_policy = raw.get("policy_hash")
        if old_policy and old_policy != self.policy_hash:
            logger.info("LayerPolicy/SignalDetectors 已变化：历史 LLM 决策将全部失效")

    # -- 查询 -------------------------------------------------------------

    def lookup_llm(self, name: str, node_type: str) -> Optional[Dict[str, Any]]:
        """只在三项指纹一致时复用 LLM 决策；规则决策不在复用范围。

        条目当前可能已被规则/人工决策覆盖，此时回退到条目里保留的
        last_llm 快照：规则只是本次命中，不应销毁历史 LLM 决策。
        """
        if not self.enabled:
            return None
        entry = self.previous.get(decision_key(name, node_type))
        if not entry:
            return None
        candidate = entry if entry.get("source") == "llm" else entry.get("last_llm")
        if not isinstance(candidate, dict):
            return None
        if candidate.get("layer") not in VALID_OVERRIDE_LAYERS:
            return None
        if candidate.get("policy_hash") != self.policy_hash:
            return None
        if candidate.get("prompt_version") != self.prompt_version:
            return None
        if candidate.get("model_id") != self.model_id:
            return None
        return candidate

    # -- 写入 -------------------------------------------------------------

    def record(self, name: str, node_type: str, layer: str, source: str,
               rule: str = "", confidence: float = 0.0,
               signals: Optional[Dict[str, Any]] = None,
               reason: str = "", decided_at: Optional[str] = None,
               doc_id: str = "") -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        key = decision_key(name, node_type)
        old = self.previous.get(key)
        sig = signals or {}
        doc_ids: List[str] = []
        if old and isinstance(old.get("doc_ids"), list):
            doc_ids = [str(d) for d in old["doc_ids"] if d]
        if doc_id and doc_id not in doc_ids:
            doc_ids.append(doc_id)

        # 规则/人工本次命中时，保留历史 LLM 决策快照，避免它被覆盖后永久丢失
        last_llm: Optional[Dict[str, Any]] = None
        if source != "llm" and old:
            prev_llm = old if old.get("source") == "llm" else old.get("last_llm")
            if isinstance(prev_llm, dict):
                last_llm = {
                    "layer": prev_llm.get("layer"),
                    "rule": prev_llm.get("rule"),
                    "confidence": prev_llm.get("confidence"),
                    "decided_at": prev_llm.get("decided_at"),
                    "policy_hash": prev_llm.get("policy_hash"),
                    "prompt_version": prev_llm.get("prompt_version"),
                    "model_id": prev_llm.get("model_id"),
                }

        entry: Dict[str, Any] = {
            "name": name,
            "type": node_type,
            "layer": layer,
            "source": source,
            "rule": rule,
            "confidence": round(float(confidence), 3),
            "policy_hash": self.policy_hash,
            "prompt_version": self.prompt_version,
            "model_id": self.model_id,
            "signals": {k: sig.get(k) for k in SIGNAL_KEYS if sig.get(k) is not None},
            "reason": reason,
            "decided_at": decided_at or _now(),
            "last_seen_at": _now(),
            "build_id": self.build_id,
            "doc_ids": doc_ids,
            "stale": False,
        }
        if last_llm:
            entry["last_llm"] = last_llm
        self.data["decisions"][key] = entry
        self.seen.add(key)
        if old:
            self._detect_flip(key, old, entry)
        return entry

    def _detect_flip(self, key: str, old: Dict[str, Any], new: Dict[str, Any]) -> None:
        old_layer = str(old.get("layer") or "")
        new_layer = str(new.get("layer") or "")
        if old_layer == new_layer:
            return
        old_in = old_layer in VALID_OVERRIDE_LAYERS
        new_in = new_layer in VALID_OVERRIDE_LAYERS
        if old_in and new_in:
            kind = "L1_L2"
        elif old_in != new_in:
            kind = "excluded"
        else:
            return
        self.flips.append({
            "key": key,
            "name": new.get("name") or old.get("name") or "",
            "type": new.get("type") or old.get("type") or "",
            "old_layer": old_layer,
            "new_layer": new_layer,
            "old_source": old.get("source") or "",
            "new_source": new.get("source") or "",
            "kind": kind,
        })

    # -- 收尾 -------------------------------------------------------------

    def finalize(self, stats: Optional[Dict[str, Any]] = None,
                 partial: bool = False) -> None:
        if not self.enabled:
            return
        if not partial:
            for key, entry in self.data["decisions"].items():
                entry["stale"] = key not in self.seen
        self.data["stats"] = stats or {}
        self.data["flips"] = self.flips
        self.data["updated_at"] = _now()
        self.data["build_id"] = self.build_id
        self.data["policy_hash"] = self.policy_hash
        self.data["prompt_version"] = self.prompt_version
        self.data["model_id"] = self.model_id

    def save(self) -> bool:
        if not self.enabled:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
            return True
        except OSError as e:
            logger.warning(f"登记表写入失败（不影响本次构建）：{e}")
            return False

    # -- 统计 -------------------------------------------------------------

    def decision_count(self) -> int:
        return len(self.data.get("decisions") or {})
