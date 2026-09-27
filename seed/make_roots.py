#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 ECDICT 离线词典生成「词根词族」种子数据。

用法: python3 seed/make_roots.py
输出: seed/roots.json —— 每个词根一组成员词，词形全部经词典核验，
      英释义(en)/中文(zh)/音标(phon)取自 ECDICT，查不到的词自动剔除。
      why 为人工编写的构词推导：前缀(意思) + 词根(意思) → 引申 → 词义，
      让「另外那个词根/词缀的意思」也看得见。
"""
import json
import os
import re
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DICT_DB = os.path.expanduser("~/.vocab/dict.db")

# 构词推导：词根结合处，另一个词根/词缀的意思也写出来。
WHY = {
    # --- spect / spec 看 ---
    "inspect": "in-(向内) + spect(看) → 往里看 → 检查",
    "respect": "re-(反复) + spect(看) → 反复注视 → 尊重",
    "expect": "ex-(向外) + spect(看) → 向外张望等候 → 期待",
    "suspect": "sus-(从下) + spect(看) → 从下偷看 → 怀疑",
    "prospect": "pro-(向前) + spect(看) → 向前看 → 前景",
    "aspect": "ad-(朝) + spect(看) → 朝某方向看的样子 → 方面",
    "spectator": "spect(看) + -ator(人) → 看的人 → 观众",
    "spectacle": "spect(看) + -acle(物) → 供观看之物 → 景象；眼镜",
    "speculate": "spec(看) + -ulate → 边看边琢磨 → 推测；投机",
    "spectrum": "spect(看) + -rum → 看到的一整串 → 光谱；范围",
    "specify": "speci(种类) + -fy(使) → 指明是哪一类 → 指定",
    "specific": "speci(种类) + -fic → 定了类的 → 具体的",
    "species": "speci(种类) → 同一类的生物 → 物种",
    "specimen": "speci(种类) + -men → 同类里的样品 → 标本",
    "retrospect": "retro-(向后) + spect(看) → 向后看 → 回顾",
    "perspective": "per-(透过) + spect(看) → 透过去看 → 视角；透视",
    "circumspect": "circum-(环顾) + spect(看) → 四下都看了 → 谨慎的",
    "conspicuous": "con-(共同) + spic(看) → 大家都看得到 → 显眼的",
    "despise": "de-(向下) + spis(看) → 看不上眼 → 轻视",
    "prospectus": "pro-(向前) + spect(看) → 展望前路的册子 → 招股书",
    "introspection": "intro-(向内) + spect(看) → 向内看自己 → 内省",
    "irrespective": "ir-(不) + respect(看重) → 不论 → 不顾的",
    "specification": "speci(种类) + -fy + -cation → 定类细则 → 规格",
    "inspector": "in-(向内) + spect(看) + -or(人) → 检查员",
    "retrospective": "retro-(向后) + spect(看) → 回顾的",
    "auspicious": "aus(鸟) + spic(看) → 古罗马看鸟占卜得吉 → 吉兆的",
    # --- dict 说 ---
    "predict": "pre-(预先) + dict(说) → 提前说 → 预言",
    "predictable": "pre-(预先) + dict(说) + -able → 可预知的",
    "verdict": "ver(真实) + dict(说) → 说出事实 → 裁决",
    "dictate": "dict(说) + -ate → 照着说 → 口述；命令",
    "dictation": "dict(说) → 听写",
    "dictator": "dict(说) + -ator(人) → 说一不二的人 → 独裁者",
    "contradict": "contra-(相反) + dict(说) → 说反话 → 反驳",
    "contradiction": "contra-(相反) + dict(说) → 互相顶嘴 → 矛盾",
    "indict": "in-(对着) + dict(说) → 对着官方说 → 起诉",
    "indictment": "in-(对着) + dict(说) → 起诉书",
    "edict": "e-(出) + dict(说) → 说出来的 → 法令",
    "jurisdiction": "juris(法律) + dict(说) → 法律上说了算的地盘 → 管辖权",
    "benediction": "bene-(好) + dict(说) → 说好话 → 祝福",
    "malediction": "male-(坏) + dict(说) → 说坏话 → 诅咒",
    "dictum": "dict(说) → 说出来的话 → 格言",
    "addict": "ad-(向) + dict(说) → 被判给某物 → 上瘾",
    "addiction": "ad-(向) + dict(说) → 被判给 → 瘾",
    # --- port 搬运 ---
    "import": "im-(向内) + port(搬) → 运进来 → 进口",
    "export": "ex-(向外) + port(搬) → 运出去 → 出口",
    "transport": "trans-(越过) + port(搬) → 运过去 → 运输",
    "report": "re-(回) + port(带) → 把消息带回 → 报告",
    "support": "sup-(从下) + port(撑) → 从下托住 → 支持",
    "important": "im-(向内) + port(搬) → 带进来有分量的 → 重要的",
    "portable": "port(搬) + -able → 搬得动的 → 便携的",
    "porter": "port(搬) + -er(人) → 搬东西的人 → 搬运工",
    "portfolio": "port(搬) + folio(纸张) → 装纸的夹子 → 公文包",
    "opportunity": "ob-(对着) + port(港口) → 风向正对港口 → 顺风时机 → 机会",
    "opportune": "ob-(对着) + port(港口) → 顺风的 → 及时的",
    "deport": "de-(出去) + port(送) → 送出去 → 驱逐出境",
    "deportation": "de-(出去) + port(送) → 驱逐出境",
    "rapport": "re-(回) + port(带) → 能带来回应 → 融洽关系",
    "deportment": "de-(离开) + port(带走) → 把自己带走的姿态 → 举止",
    "sport": "dis-(分开) + port(搬) → 把正事搬开 → 娱乐；运动",
    "supportive": "sup-(从下) + port(撑) → 从下托着的 → 支持的",
    "importer": "im-(向内) + port(搬) + -er → 进口商",
    "exporter": "ex-(向外) + port(搬) + -er → 出口商",
    "transportation": "trans-(越过) + port(搬) → 运输",
    "purport": "pur-(向前) + port(送) → 送出去的主旨 → 意图",
    # --- ject / jet 扔 ---
    "reject": "re-(回) + ject(扔) → 扔回去 → 拒绝",
    "rejection": "re-(回) + ject(扔) → 被扔回来 → 拒绝",
    "project": "pro-(向前) + ject(扔) → 向前扔出 → 计划；投射",
    "projection": "pro-(向前) + ject(扔) → 投出去 → 投影；预测",
    "inject": "in-(向内) + ject(扔) → 扔进去 → 注射",
    "injection": "in-(向内) + ject(扔) → 注射",
    "eject": "e-(向外) + ject(扔) → 扔出去 → 弹出；驱逐",
    "object": "ob-(对着) + ject(扔) → 扔在面前的东西 → 物体；反对",
    "objection": "ob-(对着) + ject(扔) → 扔向对方的异议 → 反对",
    "subject": "sub-(在下面) + ject(扔) → 扔到麾下 → 主题；臣民",
    "subjective": "sub-(在下面) + ject(扔) → 只在自己心里的 → 主观的",
    "interject": "inter-(中间) + ject(扔) → 扔进话缝 → 插嘴",
    "adjective": "ad-(向) + ject(扔) → 扔到名词旁边的 → 形容词",
    "trajectory": "tra-(穿过) + ject(扔) → 扔出去的路径 → 轨道",
    "conjecture": "con-(共同) + ject(扔) → 大家抛出想法 → 猜测",
    "abject": "ab-(离开) + ject(扔) → 被扔掉的 → 卑贱的",
    "dejected": "de-(向下) + ject(扔) → 被摔在地上 → 沮丧的",
    "jettison": "jet(扔) → 把货扔下船 → 抛弃",
    "jet": "ject(扔) → 喷出来的 → 喷射",
    # --- duc / duct 引导 ---
    "conduct": "con-(共同) + duct(引) → 引到一起 → 指挥；管理",
    "product": "pro-(向前) + duct(引) → 引出来的东西 → 产品",
    "produce": "pro-(向前) + duc(引) → 引出来 → 生产",
    "producer": "pro-(向前) + duc(引) + -er → 生产者",
    "reduce": "re-(回) + duc(引) → 引回原处 → 减少",
    "reduction": "re-(回) + duc(引) → 减少",
    "introduce": "intro-(向内) + duc(引) → 引进来 → 介绍",
    "introduction": "intro-(向内) + duc(引) → 引进 → 介绍",
    "educate": "e-(向外) + duc(引) → 把潜能引出来 → 教育",
    "deduce": "de-(向下) + duc(引) → 往下推 → 演绎出",
    "deduct": "de-(向下) + duct(引) → 往下减 → 扣除",
    "deduction": "de-(向下) + duct(引) → 扣除；推论",
    "induce": "in-(向内) + duc(引) → 引入 → 诱导；招致",
    "inductive": "in-(向内) + duc(引) → 引出规律的 → 归纳的",
    "seduce": "se-(离开) + duc(引) → 把人引走 → 诱惑",
    "abduct": "ab-(离开) + duct(引) → 引走 → 绑架",
    "duct": "duct(引) → 引流的管子 → 导管",
    "aqueduct": "aque(水) + duct(引) → 引水的 → 水道桥",
    "viaduct": "via(路) + duct(引) → 引路跨谷 → 高架桥",
    "induct": "in-(向内) + duct(引) → 引入门 → 使就职",
    "subdue": "sub-(向下) + duce(引) → 引到脚下 → 制服；削弱",
    "duke": "duc(引导者) → 带兵的人 → 公爵",
    "conducive": "con-(共同) + duc(引) → 能引向好的 → 有助于",
    "deductive": "de-(向下) + duct(引) → 往下推的 → 演绎的",
    "misconduct": "mis-(错) + conduct(引导) → 引错了 → 行为不端",
    "conduit": "con-(共同) + duit(引) → 引水的通道 → 导管",
}

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
    for seg in t.split("·"):
        seg = re.sub(r"^\(([^)]{1,8})\)\s*", "", seg.strip())
        seg = seg.strip()
        if seg and seg not in out:
            out.append(seg)
    return "; ".join(out[:2])[:limit]


def main():
    conn = sqlite3.connect(DICT_DB)
    conn.row_factory = sqlite3.Row
    out = []
    miss_why = []
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
            why = WHY.get(w, "")
            if not why:
                miss_why.append(w)
            members.append({
                "word": w,
                "phon": (row["phonetic"] or "").strip(),
                "en": en,
                "zh": zh,
                "why": why,
                "frq": row["frq"] or 0,
            })
        out.append({
            "root": r["root"], "meaning": r["meaning"],
            "origin": r["origin"], "words": members,
        })
        print("%-14s %s  %2d/%d" % (r["root"], r["meaning"], len(members), len(r["words"])))
    if miss_why:
        print("!! 缺构词推导:", ", ".join(miss_why))
    path = os.path.join(HERE, "roots.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    total = sum(len(g["words"]) for g in out)
    print("total words: %d -> %s" % (total, path))


if __name__ == "__main__":
    main()
