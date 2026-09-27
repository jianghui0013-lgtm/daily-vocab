#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 ECDICT 离线词典生成「词根词族」种子数据。

用法: python3 seed/make_roots.py
输出: seed/roots.json —— 每个词根一组成员词，词形全部经词典核验，
      英释义(en)/中文(zh)/音标(phon)取自 ECDICT，查不到的词自动剔除。
"""
import json
import os
import re
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DICT_DB = os.path.expanduser("~/.vocab/dict.db")

# 每个词根: root / meaning(中文) / origin(英文来源一句) / words(教学顺序的候选词)
# 候选词必须词源上真同根，硬凑的不要。顺序大致按常用度排。
ROOTS = [
    {
        "root": "spect / spec",
        "meaning": "看",
        "origin": "From Latin specere 'to look at' (past stem spect-).",
        "words": [
            "inspect", "respect", "expect", "suspect", "prospect", "aspect",
            "spectator", "spectacle", "speculate", "spectrum", "specify",
            "specific", "species", "specimen", "retrospect", "perspective",
            "circumspect", "conspicuous", "despise", "prospectus",
            "introspection", "irrespective", "specification", "inspector",
            "retrospective", "auspicious",
        ],
    },
    {
        "root": "dict",
        "meaning": "说",
        "origin": "From Latin dicere / dictare 'to say, to speak repeatedly'.",
        "words": [
            "predict", "predictable", "verdict", "dictate", "dictation",
            "dictator", "contradict", "contradiction", "indict",
            "indictment", "edict", "jurisdiction", "benediction",
            "malediction", "dictum", "addict", "addiction",
        ],
    },
    {
        "root": "port",
        "meaning": "搬运",
        "origin": "From Latin portare 'to carry'.",
        "words": [
            "import", "export", "transport", "report", "support",
            "important", "portable", "porter", "portfolio", "opportunity",
            "opportune", "deport", "deportation", "rapport", "deportment",
            "sport", "supportive", "importer", "exporter",
            "transportation", "purport",
        ],
    },
    {
        "root": "ject / jet",
        "meaning": "扔、掷",
        "origin": "From Latin jacere 'to throw' (past stem ject-).",
        "words": [
            "reject", "rejection", "project", "projection", "inject",
            "injection", "eject", "object", "objection", "subject",
            "subjective", "interject", "adjective", "trajectory",
            "conjecture", "abject", "dejected", "jettison", "jet",
        ],
    },
    {
        "root": "duc / duct",
        "meaning": "引导",
        "origin": "From Latin ducere 'to lead' (past stem duct-).",
        "words": [
            "conduct", "product", "produce", "producer", "reduce",
            "reduction", "introduce", "introduction", "educate", "deduce",
            "deduct", "deduction", "induce", "inductive", "seduce",
            "abduct", "duct", "aqueduct", "viaduct", "induct", "subdue",
            "duke", "conducive", "deductive", "misconduct", "conduit",
        ],
    },
]


def clean_zh(t, limit=80):
    """ECDICT 中文翻译: 去词性前缀和领域标签，合并分号/换行分隔的义项。"""
    if not t:
        return ""
    parts = []
    for seg in re.split(r"[;\n]", t):
        seg = re.sub(r"^\[[^\]]*\]\s*", "", seg.strip())
        seg = re.sub(r"^(n|vt|vi|v|a|ad|adj|adv|prep|conj|pron|num|int|aux)\.\s*", "", seg)
        seg = seg.strip(" ，,;")
        if seg and seg not in parts:
            parts.append(seg)
    return "；".join(parts)[:limit]


def clean_en(t, limit=100):
    """ECDICT 英文释义: 多义项用 · 分隔，去掉 (n)/(v) 词性标记，取前两条。"""
    if not t:
        return ""
    out = []
    for seg in t.split("\xc2\xb7" if False else "·"):
        seg = re.sub(r"^\(([^)]{1,8})\)\s*", "", seg.strip())
        seg = seg.strip()
        if seg and seg not in out:
            out.append(seg)
    return "; ".join(out[:2])[:limit]


def main():
    conn = sqlite3.connect(DICT_DB)
    conn.row_factory = sqlite3.Row
    out = []
    for r in ROOTS:
        members = []
        for w in r["words"]:
            row = conn.execute(
                "SELECT word, phonetic, en, translation, frq FROM dict WHERE word = ?",
                (w,)).fetchone()
            if not row:
                continue
            en, zh = clean_en(row["en"]), clean_zh(row["translation"])
            if not en and not zh:
                continue
            members.append({
                "word": w,
                "phon": (row["phonetic"] or "").strip(),
                "en": en,
                "zh": zh,
                "frq": row["frq"] or 0,
            })
        out.append({
            "root": r["root"], "meaning": r["meaning"],
            "origin": r["origin"], "words": members,
        })
        print("%-14s %s  %2d/%d" % (r["root"], r["meaning"], len(members), len(r["words"])))
    path = os.path.join(HERE, "roots.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    total = sum(len(g["words"]) for g in out)
    print("total words: %d -> %s" % (total, path))


if __name__ == "__main__":
    main()
