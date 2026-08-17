#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v — 本地生词本 (P0: 手动收词 + AI 语境化释义 + SM-2 间隔复习)

数据: ~/.vocab/vocab.db     配置: ~/.vocab/config.json
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

VOCAB_HOME = os.environ.get("VOCAB_HOME", os.path.expanduser("~/.vocab"))
DB_PATH = os.path.join(VOCAB_HOME, "vocab.db")
CFG_PATH = os.path.join(VOCAB_HOME, "config.json")

DAY_CUTOFF_HOUR = 4  # 凌晨 4 点前算前一天，跟 Anki 一致

DEFAULT_CFG = {
    "api_base": "https://api.deepseek.com",
    "api_key": "",
    "model": "deepseek-v4-flash",
    "level": "中文母语、六级左右水平的英语学习者",
    "daily_new": 20,          # 每天最多放出多少新词
    "daily_max": 200,         # 单次复习总上限
    "graduate_interval": 60,  # 间隔达到这个天数就毕业归档
    # --- 剪贴板抓词 ---
    "freq_skip_rank": 5000,   # 词频排名在这之内的词直接跳过（你肯定认识）
    "auto_accept": 0.8,       # AI 生词分 >= 此值：直接入库
    "inbox_min": 0.4,         # >= 此值进 inbox 待确认，低于则丢弃
    "watch_interval": 1.0,    # 剪贴板轮询秒数
    "max_clip_chars": 500,    # 超过这个长度不处理
    "max_candidates": 8,      # 一次最多问 AI 几个候选词
    "notify": True,           # 抓到词后弹 macOS 通知
    # --- 新闻 ---
    "lan_token": "",          # 手机访问用的固定口令，首次开 --lan 时自动生成
    "news_hour": 6,           # 每天几点抓（本地时间，24 小时制）
    "news_count": 10,         # 每天抓几条
    "pick_size": 21,          # 推荐区同时摆几个词（3 列 × 7 行）
}


# ---------------------------------------------------------------- 小工具

def _tty():
    return sys.stdout.isatty()


def c(s, code):
    return "\033[%sm%s\033[0m" % (code, s) if _tty() else s


def bold(s):
    return c(s, "1")


def dim(s):
    return c(s, "2")


def green(s):
    return c(s, "32")


def yellow(s):
    return c(s, "33")


def cyan(s):
    return c(s, "36")


def red(s):
    return c(s, "31")


def logical_now():
    return datetime.now() - timedelta(hours=DAY_CUTOFF_HOUR)


def today():
    return logical_now().strftime("%Y-%m-%d")


def day_plus(n):
    return (logical_now() + timedelta(days=n)).strftime("%Y-%m-%d")


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize(w):
    w = (w or "").strip().lower()
    w = re.sub(r"^[^a-z']+", "", w)
    w = re.sub(r"[^a-z']+$", "", w)
    return w


def getch():
    """读一个键；拿不到 tty 就退回整行输入。"""
    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if ch == "\x03":  # Ctrl-C
            raise KeyboardInterrupt
        return ch
    except Exception:
        line = sys.stdin.readline()
        return line[:1] if line else "q"


# ---------------------------------------------------------------- 配置

def load_cfg():
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f) or {})
        except Exception as e:
            print(red("配置文件读不了 (%s)，用默认值。" % e))
    for env in ("VOCAB_API_KEY", "DEEPSEEK_API_KEY"):
        if not cfg.get("api_key") and os.environ.get(env):
            cfg["api_key"] = os.environ[env]
    # 占位符/含中文/太短的一律当没配，否则会拿它去请求然后报一堆看不懂的错
    k = (cfg.get("api_key") or "").strip()
    if k and (not k.isascii() or len(k) < 12):
        cfg["api_key"] = ""
        cfg["_bad_key"] = k
    if os.environ.get("VOCAB_MODEL"):
        cfg["model"] = os.environ["VOCAB_MODEL"]
    return cfg


def save_cfg(cfg):
    os.makedirs(VOCAB_HOME, exist_ok=True)
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CFG_PATH)
    os.chmod(CFG_PATH, 0o600)


# ---------------------------------------------------------------- 数据库

SCHEMA = """
CREATE TABLE IF NOT EXISTS words (
  id              INTEGER PRIMARY KEY,
  word            TEXT NOT NULL UNIQUE,
  lemma           TEXT,
  phonetic        TEXT,
  pos             TEXT,
  definition      TEXT,
  definition_en   TEXT,
  study_count     INTEGER NOT NULL DEFAULT 0,
  encounter_count INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_words_lemma ON words(lemma);

CREATE TABLE IF NOT EXISTS contexts (
  id         INTEGER PRIMARY KEY,
  word_id    INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
  surface    TEXT,
  sentence   TEXT,
  meaning    TEXT,
  source     TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contexts_word ON contexts(word_id);

CREATE TABLE IF NOT EXISTS reviews (
  word_id        INTEGER PRIMARY KEY REFERENCES words(id) ON DELETE CASCADE,
  status         TEXT NOT NULL DEFAULT 'new',
  due_date       TEXT,
  interval       INTEGER NOT NULL DEFAULT 0,
  ease           REAL NOT NULL DEFAULT 2.5,
  reps           INTEGER NOT NULL DEFAULT 0,
  lapses         INTEGER NOT NULL DEFAULT 0,
  last_review_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_due ON reviews(status, due_date);

CREATE TABLE IF NOT EXISTS review_log (
  id            INTEGER PRIMARY KEY,
  word_id       INTEGER NOT NULL,
  grade         INTEGER NOT NULL,
  interval_from INTEGER,
  interval_to   INTEGER,
  ease_to       REAL,
  reviewed_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS word_freq (
  word TEXT PRIMARY KEY,
  rank INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox (
  id         INTEGER PRIMARY KEY,
  word       TEXT NOT NULL,
  lemma      TEXT,
  phonetic   TEXT,
  pos        TEXT,
  definition TEXT,
  meaning    TEXT,
  sentence   TEXT,
  source     TEXT,
  score      REAL,
  status     TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox(status);

CREATE TABLE IF NOT EXISTS examples (
  id         INTEGER PRIMARY KEY,
  word_id    INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
  en         TEXT NOT NULL,
  zh         TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_examples_word ON examples(word_id);

CREATE TABLE IF NOT EXISTS news (
  id         INTEGER PRIMARY KEY,
  day        TEXT NOT NULL,
  title      TEXT NOT NULL,
  summary    TEXT,
  link       TEXT NOT NULL UNIQUE,
  source     TEXT,
  published  TEXT,
  ai_en      TEXT,
  ai_zh      TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_day ON news(day);

CREATE TABLE IF NOT EXISTS pick (
  word       TEXT PRIMARY KEY,
  day        TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pick_status ON pick(status, day);

CREATE TABLE IF NOT EXISTS word_root (
  word       TEXT PRIMARY KEY,
  root       TEXT,
  variants   TEXT,
  meaning    TEXT,
  breakdown  TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_word_root ON word_root(root);

CREATE TABLE IF NOT EXISTS root_family (
  root       TEXT PRIMARY KEY,
  words      TEXT,
  created_at TEXT NOT NULL
);
"""


def db():
    os.makedirs(VOCAB_HOME, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")     # 网页和剪贴板守护会同时写
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(words)")]
    if "definition_en" not in cols:
        conn.execute("ALTER TABLE words ADD COLUMN definition_en TEXT")
        conn.commit()
    if "study_count" not in cols:
        conn.execute("ALTER TABLE words ADD COLUMN study_count INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    ncols = [r[1] for r in conn.execute("PRAGMA table_info(news)")]
    for c in ("ai_en", "ai_zh"):
        if ncols and c not in ncols:
            conn.execute("ALTER TABLE news ADD COLUMN %s TEXT" % c)
            conn.commit()
    return conn


def find_word(conn, key):
    """按 word 或 lemma 查，两边都命中算同一个词。"""
    return conn.execute(
        "SELECT * FROM words WHERE word = ? OR lemma = ? LIMIT 1", (key, key)
    ).fetchone()


# ---------------------------------------------------------------- AI

AI_SYSTEM = (
    "你是一个英语词汇助手，服务对象是%s。"
    "只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释性文字。"
)

AI_USER = """请解释单词：{word}
{ctx}
按这个结构返回 JSON：
{{
  "lemma": "原形（如 rescinded -> rescind）",
  "phonetic": "美式音标，带斜杠",
  "pos": "词性缩写，如 v. / n. / adj.",
  "definition": "常用中文释义，1-2 条，用；分隔。要精准，别啰嗦",
  "meaning_in_context": "如果给了句子，说明它在这个句子里的具体意思（中文，一句话）；没给句子就填空字符串",
  "example": "如果没给句子，造一个地道的英文例句；给了句子就填空字符串"
}}"""


def ai_lookup(word, sentence, cfg, quiet=False):
    key = cfg.get("api_key")
    if not key:
        return None
    ctx = ("它出现在这个句子里：%s" % sentence) if sentence else "（没有提供上下文）"
    payload = {
        "model": cfg.get("model"),
        "messages": [
            {"role": "system", "content": AI_SYSTEM % cfg.get("level")},
            {"role": "user", "content": AI_USER.format(word=word, ctx=ctx)},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    url = cfg.get("api_base", "").rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = body["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:200]
        except Exception:
            pass
        if not quiet:
            _clear_line()
            print(dim("  AI 查询失败 HTTP %s %s" % (e.code, detail)))
        return None
    except Exception as e:
        if not quiet:
            _clear_line()
            print(dim("  AI 查询失败: %s" % e))
        return None

    data = _parse_json(text)
    if not isinstance(data, dict):
        if not quiet:
            _clear_line()
            print(dim("  AI 返回的不是 JSON，已跳过释义"))
        return None
    return data


def _clear_line():
    if _tty():
        sys.stdout.write("\r" + " " * 30 + "\r")
        sys.stdout.flush()


def _parse_json(text):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


# ---------------------------------------------------------------- add

def insert_word(conn, lemma, ai, surface, sentence, source):
    """新建一条词 + 复习记录 + 例句，返回 word_id。"""
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO words (word, lemma, phonetic, pos, definition, definition_en,"
        " study_count, encounter_count, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,1,1,?,?)",
        (lemma, lemma, ai.get("phonetic"), ai.get("pos"), ai.get("definition"),
         ai.get("definition_en") or (dict_lookup(lemma) or {}).get("definition_en"), ts, ts),
    )
    wid = cur.lastrowid
    conn.execute(
        "INSERT OR REPLACE INTO reviews (word_id, status, due_date, interval, ease, reps,"
        " lapses) VALUES (?, 'new', ?, 0, 2.5, 0, 0)", (wid, today()),
    )
    sent = sentence or ai.get("example") or ""
    if sent:
        conn.execute(
            "INSERT INTO contexts (word_id, surface, sentence, meaning, source, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (wid, surface, sent, ai.get("meaning_in_context"),
             source or (None if sentence else "AI 例句"), ts),
        )
    return wid


def cmd_add(args, cfg):
    raw = " ".join(args.word).strip()
    key = normalize(raw)
    if not key or not re.match(r"^[a-z][a-z'\- ]*$", key):
        print(red("这看起来不像一个英文单词：%r" % raw))
        return 1

    conn = db()
    sentence = (args.context or "").strip()
    source = (args.source or "").strip() or None

    row = find_word(conn, key)
    if row:
        _bump(conn, row, key, sentence, source, None)
        conn.commit()
        print("%s %s  %s" % (yellow("↑"), bold(row["word"]),
                             dim("已在库中，遇见次数 %d" % (row["encounter_count"] + 1))))
        if sentence:
            print(dim("  + 例句已追加"))
        _print_word(conn, find_word(conn, key), brief=True)
        return 0

    ai = None
    if not args.no_ai:
        ai = lookup(key, sentence, cfg)

    lemma = normalize((ai or {}).get("lemma") or "") or key
    # AI 还原出原形后再查一次，避免 rescinded / rescind 建两条
    if lemma != key:
        row = find_word(conn, lemma)
        if row:
            _bump(conn, row, key, sentence, source, (ai or {}).get("meaning_in_context"))
            conn.commit()
            print("%s %s %s" % (yellow("↑"), bold(row["word"]),
                                dim("(%s 的原形) 已在库中，遇见次数 %d"
                                    % (key, row["encounter_count"] + 1))))
            return 0

    insert_word(conn, lemma, ai or {}, key, sentence, source)
    conn.commit()

    print("%s %s" % (green("+"), bold(lemma)))
    _print_word(conn, find_word(conn, lemma), brief=True)
    if not (ai or {}).get("definition"):
        print(dim("  （词典里没查到这个词，释义先空着）"))
    return 0


def _bump(conn, row, surface, sentence, source, meaning):
    conn.execute(
        "UPDATE words SET encounter_count = encounter_count + 1, updated_at = ? WHERE id = ?",
        (now_iso(), row["id"]),
    )
    if sentence:
        dup = conn.execute(
            "SELECT 1 FROM contexts WHERE word_id = ? AND sentence = ?",
            (row["id"], sentence),
        ).fetchone()
        if not dup:
            conn.execute(
                "INSERT INTO contexts (word_id, surface, sentence, meaning, source, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (row["id"], surface, sentence, meaning, source, now_iso()),
            )


# ---------------------------------------------------------------- review

def _queue(conn, cfg):
    due = conn.execute(
        "SELECT w.* FROM words w JOIN reviews r ON r.word_id = w.id"
        " WHERE r.status IN ('learning','review') AND r.due_date <= ?"
        " ORDER BY r.due_date ASC, w.encounter_count DESC", (today(),),
    ).fetchall()
    new = conn.execute(
        "SELECT w.* FROM words w JOIN reviews r ON r.word_id = w.id"
        " WHERE r.status = 'new'"
        " ORDER BY w.encounter_count DESC, w.created_at ASC LIMIT ?",
        (cfg["daily_new"],),
    ).fetchall()
    return (list(due) + list(new))[: cfg["daily_max"]]


def schedule(r, grade, cfg):
    """SM-2 简化版。grade: 1=忘了 2=模糊 3=会了"""
    ease = r["ease"]
    interval = r["interval"] or 0
    reps = r["reps"]
    lapses = r["lapses"]

    if grade == 1:
        ease = max(1.3, ease - 0.2)
        interval = 1
        reps = 0
        lapses += 1
    elif grade == 2:
        interval = max(1, int(round(max(interval, 1) * 1.2)))
        reps += 1
    else:
        if reps == 0:
            interval = 1
        elif reps == 1:
            interval = 3
        else:
            interval = int(round(max(interval, 1) * ease))
        ease = min(3.0, ease + 0.1)
        reps += 1

    if grade == 3 and interval >= cfg["graduate_interval"]:
        status = "known"
    elif interval >= 7:
        status = "review"
    else:
        status = "learning"
    return status, interval, ease, reps, lapses


def cmd_review(args, cfg):
    conn = db()
    queue = _queue(conn, cfg)
    if not queue:
        n = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        print(green("今天没有要复习的词。") if n else "还没有收词，先 %s" % bold("v add <word>"))
        return 0

    print(dim("共 %d 个词  ·  空格翻面  ·  1 忘了  2 模糊  3 会了  ·  q 退出\n" % len(queue)))
    done = 0
    for i, w in enumerate(queue, 1):
        r = conn.execute("SELECT * FROM reviews WHERE word_id = ?", (w["id"],)).fetchone()
        ctxs = conn.execute(
            "SELECT * FROM contexts WHERE word_id = ? ORDER BY id DESC LIMIT 3", (w["id"],)
        ).fetchall()

        print(dim("── %d/%d ─────────────" % (i, len(queue))))
        print("  %s" % bold(cyan(w["word"])))
        if ctxs:
            print("  %s" % dim(_mask(ctxs[0]["sentence"], w["word"])))
        print(dim("  [空格翻面]"), end="", flush=True)
        k = getch()
        print("\r" + " " * 24)
        if k in ("q", "Q"):
            break

        if w["phonetic"]:
            print("  %s" % dim(w["phonetic"]))
        print("  %s" % (w["definition"] or dim("(暂无释义)")))
        for ct in ctxs:
            if ct["sentence"]:
                print("  %s %s" % (dim("·"), ct["sentence"]))
            if ct["meaning"]:
                print("    %s" % dim("→ " + ct["meaning"]))

        grade = None
        while grade is None:
            print("  %s " % dim("1 忘了 / 2 模糊 / 3 会了"), end="", flush=True)
            k = getch()
            print("\r" + " " * 40, end="\r")
            if k in ("q", "Q"):
                grade = 0
            elif k in ("1", "2", "3"):
                grade = int(k)
        if grade == 0:
            break

        status, interval, ease, reps, lapses = schedule(r, grade, cfg)
        conn.execute(
            "UPDATE reviews SET status=?, due_date=?, interval=?, ease=?, reps=?,"
            " lapses=?, last_review_at=? WHERE word_id=?",
            (status, day_plus(interval), interval, ease, reps, lapses, now_iso(), w["id"]),
        )
        conn.execute(
            "INSERT INTO review_log (word_id, grade, interval_from, interval_to, ease_to,"
            " reviewed_at) VALUES (?,?,?,?,?,?)",
            (w["id"], grade, r["interval"], interval, ease, now_iso()),
        )
        conn.execute("UPDATE words SET study_count = study_count + 1 WHERE id = ?", (w["id"],))
        conn.commit()
        done += 1
        tip = green("毕业归档 ✓") if status == "known" else dim("%d 天后再见" % interval)
        print("  %s\n" % tip)

    print(dim("\n本次复习 %d 个词。" % done))
    return 0


def _mask(sentence, word):
    if not sentence:
        return ""
    stem = word[:4] if len(word) > 4 else word
    return re.sub(r"\b%s\w*\b" % re.escape(stem), "____", sentence, flags=re.I)


# ---------------------------------------------------------------- list / show / stats

def cmd_list(args, cfg):
    conn = db()
    where, params = [], []
    if args.due:
        where.append("r.status IN ('learning','review') AND r.due_date <= ?")
        params.append(today())
    elif args.new:
        where.append("r.status = 'new'")
    elif args.known:
        where.append("r.status = 'known'")
    if args.query:
        where.append("(w.word LIKE ? OR w.definition LIKE ?)")
        params += ["%%%s%%" % args.query, "%%%s%%" % args.query]
    sql = ("SELECT w.*, r.status, r.due_date, r.interval FROM words w"
           " JOIN reviews r ON r.word_id = w.id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY w.updated_at DESC LIMIT ?"
    params.append(args.limit)

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print(dim("没有匹配的词。"))
        return 0
    for w in rows:
        mark = {"new": yellow("新"), "learning": cyan("学"),
                "review": green("复"), "known": dim("会")}.get(w["status"], " ")
        cnt = dim("×%d" % w["encounter_count"]) if w["encounter_count"] > 1 else "   "
        d = (w["definition"] or "")[:46]
        print("%s %-18s %s %s" % (mark, bold(w["word"]), cnt, d))
    print(dim("\n%d 个词" % len(rows)))
    return 0


def cmd_show(args, cfg):
    conn = db()
    w = find_word(conn, normalize(args.word))
    if not w:
        print(red("库里没有 %s" % args.word))
        return 1
    _print_word(conn, w, brief=False)
    return 0


def _print_word(conn, w, brief=True):
    if not w:
        return
    r = conn.execute("SELECT * FROM reviews WHERE word_id = ?", (w["id"],)).fetchone()
    head = "  %s" % bold(cyan(w["word"]))
    if w["phonetic"]:
        head += " %s" % dim(w["phonetic"])
    if w["pos"]:
        head += " %s" % dim(w["pos"])
    print(head)
    if w["definition"]:
        print("  %s" % w["definition"])
    ctxs = conn.execute(
        "SELECT * FROM contexts WHERE word_id = ? ORDER BY id DESC LIMIT ?",
        (w["id"], 1 if brief else 20),
    ).fetchall()
    for ct in ctxs:
        if ct["sentence"]:
            print("  %s %s" % (dim("·"), ct["sentence"]))
        if ct["meaning"]:
            print("    %s" % dim("→ " + ct["meaning"]))
        if ct["source"] and not brief:
            print("    %s" % dim("来源: " + ct["source"]))
    if not brief and r:
        print(dim("  状态 %s · 遇见 %d 次 · 间隔 %d 天 · 下次 %s · 忘记 %d 次"
                  % (r["status"], w["encounter_count"], r["interval"],
                     r["due_date"] or "-", r["lapses"])))


def cmd_stats(args, cfg):
    conn = db()
    total = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
    if not total:
        print("还没有收词，先 %s" % bold("v add <word>"))
        return 0
    by = dict(conn.execute("SELECT status, COUNT(*) FROM reviews GROUP BY status").fetchall())
    due = conn.execute(
        "SELECT COUNT(*) FROM reviews WHERE status IN ('learning','review') AND due_date <= ?",
        (today(),)).fetchone()[0]
    week = conn.execute(
        "SELECT COUNT(*) FROM words WHERE created_at >= ?",
        ((datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),)).fetchone()[0]
    reviewed = conn.execute("SELECT COUNT(*) FROM review_log").fetchone()[0]
    top = conn.execute(
        "SELECT word, encounter_count FROM words WHERE encounter_count > 1"
        " ORDER BY encounter_count DESC LIMIT 5").fetchall()

    print("  总词数    %s" % bold(str(total)))
    print("  新词      %d      学习中  %d      复习中  %d      已掌握  %s"
          % (by.get("new", 0), by.get("learning", 0), by.get("review", 0),
             green(str(by.get("known", 0)))))
    print("  今天待复习 %s" % (yellow(str(due)) if due else "0"))
    print("  近 7 天新增 %d   ·   累计复习 %d 次" % (week, reviewed))
    if top:
        print(dim("  反复遇见: " + "  ".join("%s×%d" % (t[0], t[1]) for t in top)))
    return 0


def forget_word(conn, w):
    """删掉一个词，并记一笔「这个我认识」，以后剪贴板不再抓它。"""
    conn.execute(
        "INSERT INTO inbox (word, lemma, status, created_at) VALUES (?,?,'rejected',?)",
        (w["word"], w["lemma"] or w["word"], now_iso()))
    conn.execute("DELETE FROM words WHERE id = ?", (w["id"],))


def cmd_rm(args, cfg):
    conn = db()
    w = find_word(conn, normalize(args.word))
    if not w:
        print(red("库里没有 %s" % args.word))
        return 1
    forget_word(conn, w)
    conn.commit()
    print("%s 已删除 %s" % (red("-"), bold(w["word"])))
    return 0


def cmd_ai_fill(args, cfg):
    if not cfg.get("api_key") and not dict_db():
        print(red("既没有离线词典也没配 API key，补不了释义。"))
        print(dim("  装词典：v dict import <ecdict.csv>"))
        return 1
    conn = db()
    rows = conn.execute(
        "SELECT * FROM words WHERE definition IS NULL OR definition = '' LIMIT ?",
        (args.limit,)).fetchall()
    if not rows:
        print(green("所有词都有释义了。"))
        return 0
    print(dim("补全 %d 个词的释义…" % len(rows)))
    ok = 0
    for w in rows:
        ct = conn.execute(
            "SELECT sentence FROM contexts WHERE word_id = ? ORDER BY id DESC LIMIT 1",
            (w["id"],)).fetchone()
        ai = lookup(w["word"], ct["sentence"] if ct else "", cfg)
        if not ai:
            print("  %s %s" % (red("×"), w["word"]))
            continue
        conn.execute(
            "UPDATE words SET phonetic=?, pos=?, definition=?, lemma=?, updated_at=? WHERE id=?",
            (ai.get("phonetic"), ai.get("pos"), ai.get("definition"),
             normalize(ai.get("lemma") or "") or w["lemma"], now_iso(), w["id"]))
        if not ct and ai.get("example"):
            conn.execute(
                "INSERT INTO contexts (word_id, surface, sentence, meaning, source, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (w["id"], w["word"], ai["example"], None, "AI 例句", now_iso()))
        conn.commit()
        ok += 1
        print("  %s %-16s %s" % (green("✓"), w["word"], (ai.get("definition") or "")[:40]))
    print(dim("\n补全 %d/%d" % (ok, len(rows))))
    return 0


def cmd_key(args, cfg):
    """交互式设置 API key：输入不回显，也不会进 shell 历史。"""
    import getpass
    print("  在 platform.deepseek.com 生成一个 key，粘贴到下面回车（输入不会显示出来）")
    try:
        k = getpass.getpass("  key: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    if not k:
        print(red("  没输入，取消了。"))
        return 1
    if not k.isascii() or len(k) < 12:
        print(red("  这不像一个密钥（%s）。" % k[:20]))
        print(dim("  真正的 key 长这样：sk- 开头的一长串英文数字。没有就别配，"))
        print(dim("  离线词典已经够用了。"))
        return 1

    raw = {}
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
        except Exception:
            raw = {}
    raw["api_key"] = k
    save_cfg(raw)
    print(green("  已保存到 %s（权限 600）" % CFG_PATH))

    cfg = load_cfg()
    print(dim("  拿一个词试一下 %s …" % cfg.get("model")))
    ai = ai_lookup("rescind", "The board voted to rescind the offer.", cfg)
    if not ai:
        print(red("  没跑通。"))
        print(dim("  常见原因：key 写错了，或者模型名不对。"))
        print(dim("  换个模型再试：v config set model deepseek-v4-pro，然后 v key 重来"))
        return 1
    print(green("  通了 ✓"))
    print("    %s %s %s" % (bold("rescind"), dim(ai.get("phonetic") or ""),
                           dim(ai.get("pos") or "")))
    print("    %s" % (ai.get("definition") or ""))
    if ai.get("meaning_in_context"):
        print("    %s" % dim("→ " + ai["meaning_in_context"]))
    n = db().execute(
        "SELECT COUNT(*) FROM words WHERE definition IS NULL OR definition = ''").fetchone()[0]
    if n:
        print(dim("\n  库里还有 %d 个词没释义，跑 v ai-fill 补上。" % n))
    return 0


def cmd_config(args, cfg):
    raw = {}
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
    if args.action == "show":
        shown = dict(cfg)
        k = shown.get("api_key") or ""
        if cfg.get("_bad_key"):
            shown["api_key"] = "(无效，已忽略：%s)" % cfg["_bad_key"][:12]
        else:
            shown["api_key"] = (k[:6] + "…" + k[-4:]) if len(k) > 12 else (
                "(未设置，走离线词典)" if not k else "(已设置)")
        shown.pop("_bad_key", None)
        for key in sorted(shown):
            print("  %-18s %s" % (key, shown[key]))
        print(dim("\n配置文件: %s" % CFG_PATH))
        return 0
    if not args.key:
        print(red("用法: v config set <key> <value>"))
        return 1
    if args.key not in DEFAULT_CFG:
        print(red("未知配置项 %s。可用: %s" % (args.key, ", ".join(sorted(DEFAULT_CFG)))))
        return 1
    val = args.value
    if isinstance(DEFAULT_CFG[args.key], int):
        val = int(val)
    raw[args.key] = val
    save_cfg(raw)
    print(green("已设置 %s" % args.key))
    return 0


# ---------------------------------------------------------------- 词频粗筛

# 内置兜底高频词（按大致词频排序）。想更准就 v freq import <排名文件>。
FALLBACK_COMMON = """
the be to of and a in that have i it for not on with he as you do at this
but his by from they we say her she or an will my one all would there their
what so up out if about who get which go me when make can like time no just
him know take people into year your good some could them see other than then
now look only come its over think also back after use two how our work first
well way even new want because any give day most us is are was were been am
being has had did does said says made making going got gone went come came
take took taken see saw seen know knew known think thought tell told find
found give given go goes went keep kept let put set say seem show shown try
turn ask need feel become leave call move live believe hold bring happen write
provide sit stand lose pay meet include continue learn change lead understand
watch follow stop create speak read allow add spend grow open walk win offer
remember love consider appear buy wait serve die send expect build stay fall
cut reach kill remain suggest raise pass sell require report decide pull
person people man woman child world life hand part place case week company
system program question work government number night point home water room
mother area money story fact month lot right study book eye job word business
issue side kind head house service friend father power hour game line end
member law car city community name president team minute idea kid body
information back parent face others level office door health art war history
party result change morning reason research girl guy moment air teacher force
education foot boy age policy process music market sense nation plan college
interest death course someone experience behavior car ago able bad best better
big black certain clear common different difficult early easy economic few
free full good great green hard high human important international large late
little local long low major military national natural new nice old only other
personal political poor possible present private public real recent right
small social special strong sure true whole young simple serious short single
similar strange sudden usual various white wrong actually almost already
always away back better close course early enough especially even ever far
finally hard here however instead later least less likely maybe much never
next often once only perhaps pretty probably quite rather really recently
right since still sometimes soon still today together too usually very well
yet yesterday tomorrow again against among around before behind below beneath
between beyond during except inside near outside over since through throughout
under until upon within without across along above according both each either
neither every many more most much nothing something anything everything
someone anyone everyone nobody another such same own very just only quite
also thus therefore while whether although though unless whereas indeed
"""


def freq_seed(conn):
    n = conn.execute("SELECT COUNT(*) FROM word_freq").fetchone()[0]
    if n:
        return n
    seen, rows = set(), []
    for w in FALLBACK_COMMON.split():
        if w not in seen:
            seen.add(w)
            rows.append((w, len(rows) + 1))
    conn.executemany("INSERT OR IGNORE INTO word_freq (word, rank) VALUES (?,?)", rows)
    conn.commit()
    return len(rows)


def stems(w):
    """粗糙还原，只用来查词频表，真正的原形交给 AI。"""
    out = [w]
    for suf, repl in (("iest", "y"), ("ies", "y"), ("ied", "y"), ("ily", "y"),
                      ("ier", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", ""),
                      ("ly", ""), ("er", ""), ("est", "")):
        if w.endswith(suf) and len(w) - len(suf) >= 2:
            base = w[: len(w) - len(suf)] + repl
            out.append(base)
            if suf in ("ing", "ed"):
                out.append(base + "e")                      # voted -> vote
                if len(base) > 2 and base[-1] == base[-2]:
                    out.append(base[:-1])                   # running -> run
    return out


def freq_rank(conn, w):
    """取所有词干里最靠前的排名——voted 本身排 5792，但 vote 排得很前，按 vote 算。"""
    d = dict_lookup(w)
    if d and d.get("_frq"):
        return d["_frq"]
    best = None
    for st in stems(w):
        r = conn.execute("SELECT rank FROM word_freq WHERE word = ?", (st,)).fetchone()
        if r and (best is None or r[0] < best):
            best = r[0]
    return best


def find_by_stem(conn, w):
    for st in stems(w):
        row = find_word(conn, st)
        if row:
            return row
    return None


def cmd_freq(args, cfg):
    conn = db()
    if args.action == "status":
        n = conn.execute("SELECT COUNT(*) FROM word_freq").fetchone()[0]
        if not n:
            n = freq_seed(conn)
        src = conn.execute("SELECT word FROM word_freq ORDER BY rank LIMIT 5").fetchall()
        print("  词频表 %s 条  ·  跳过阈值 rank <= %d" % (bold(str(n)), cfg["freq_skip_rank"]))
        print(dim("  前几个: " + " ".join(r[0] for r in src)))
        if n <= 600:
            print(dim("  当前是内置兜底表。导入更大的表能省更多 AI 调用："))
            print(dim("    v freq import <每行一个词、按词频排序的文件>"))
        return 0
    if not args.path or not os.path.exists(args.path):
        print(red("文件不存在：%s" % args.path))
        return 1
    rows, seen = [], set()
    with open(args.path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            w = normalize(line.split(",")[0].split("\t")[0])
            if w and w.isalpha() and w not in seen:
                seen.add(w)
                rows.append((w, len(rows) + 1))
    if not rows:
        print(red("没解析出词"))
        return 1
    conn.execute("DELETE FROM word_freq")
    conn.executemany("INSERT INTO word_freq (word, rank) VALUES (?,?)", rows)
    conn.commit()
    print(green("已导入 %d 个词的词频排名" % len(rows)))
    return 0


# ---------------------------------------------------------------- 离线词典

DICT_PATH = os.path.join(VOCAB_HOME, "dict.db")
SELFCOPY_PATH = os.path.join(VOCAB_HOME, ".selfcopy")


def mark_selfcopy(word):
    """网页上双击会把词复制到剪贴板，留个记号免得守护进程再当成一次「遇见」。"""
    try:
        with open(SELFCOPY_PATH, "w", encoding="utf-8") as f:
            f.write("%s\t%.1f" % (word, time.time()))
    except Exception:
        pass


def is_selfcopy(text, within=25):
    try:
        with open(SELFCOPY_PATH, encoding="utf-8") as f:
            w, ts = f.read().split("\t")
        return w == text.strip().lower() and (time.time() - float(ts)) < within
    except Exception:
        return False


def dict_db():
    """打开离线词典，没有就返回 None。"""
    if not os.path.exists(DICT_PATH):
        return None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DICT_PATH, uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def dict_import(csv_path, quiet=False):
    import csv as _csv
    _csv.field_size_limit(10 ** 7)
    tmp = DICT_PATH + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    out = sqlite3.connect(tmp)
    out.executescript("""
      PRAGMA journal_mode = OFF;
      PRAGMA synchronous = OFF;
      CREATE TABLE dict (
        word TEXT PRIMARY KEY, lemma TEXT, phonetic TEXT, translation TEXT,
        en TEXT, pos TEXT, tag TEXT, collins INTEGER, frq INTEGER, bnc INTEGER);
      CREATE INDEX IF NOT EXISTS idx_dict_tag ON dict(tag);
    """)
    n, batch = 0, []
    with open(csv_path, encoding="utf-8", errors="ignore") as f:
        for row in _csv.DictReader(f):
            w = (row.get("word") or "").strip().lower()
            tr = (row.get("translation") or "").strip()
            if not w or not tr or " " in w or not w.replace("-", "").replace("'", "").isalpha():
                continue
            lemma = ""
            for part in (row.get("exchange") or "").split("/"):
                if part.startswith("0:"):
                    lemma = part[2:].strip().lower()
                    break
            def _i(k):
                try:
                    return int(row.get(k) or 0)
                except ValueError:
                    return 0
            en_raw = (row.get("definition") or "").strip()
            senses = [x.strip() for x in en_raw.split("\\n") if x.strip()]
            mt = re.match(r"^([a-z]+)\.", tr)
            if mt and senses:
                want = {mt.group(1)}
                if "a" in want:
                    want.add("s")      # WordNet 用 s. 表示形容词的从属义项
                hit = [x for x in senses if x.split(".")[0].strip().lower() in want]
                senses = hit or senses
            senses = [POS_PREFIX_RE.sub("", x, count=1) for x in senses[:2]]
            en = " · ".join(senses)
            batch.append((w, lemma or w, (row.get("phonetic") or "").strip(),
                          tr.replace("\\n", "; "), en, (row.get("pos") or "").strip(),
                          (row.get("tag") or "").strip(),
                          _i("collins"), _i("frq"), _i("bnc")))
            if len(batch) >= 5000:
                out.executemany("INSERT OR REPLACE INTO dict VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
                n += len(batch); batch = []
                if not quiet and n % 100000 == 0:
                    print(dim("  已导入 %d 条…" % n), end="\r", flush=True)
    if batch:
        out.executemany("INSERT OR REPLACE INTO dict VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
        n += len(batch)
    out.execute("CREATE INDEX idx_dict_lemma ON dict(lemma)")
    out.commit()
    out.close()
    os.replace(tmp, DICT_PATH)
    if not quiet:
        print(" " * 30, end="\r")
    return n


def dict_lookup(word):
    """离线查词。返回 ai_lookup 那套同样的字段，好让上层无感切换。"""
    d = dict_db()
    if not d:
        return None
    try:
        row = d.execute("SELECT * FROM dict WHERE word = ?", (word,)).fetchone()
        if not row:
            for st in stems(word):
                row = d.execute("SELECT * FROM dict WHERE word = ?", (st,)).fetchone()
                if row:
                    break
        if not row:
            return None
        # 原形是一级级指的：bearings -> bearing -> bear，要跟到底
        base = row
        lemma = base["lemma"] or base["word"]
        seen = {base["word"]}
        for _ in range(4):
            if lemma == base["word"] or lemma in seen:
                break
            b = d.execute("SELECT * FROM dict WHERE word = ?", (lemma,)).fetchone()
            if not b:
                break
            seen.add(b["word"])
            base = b
            lemma = base["lemma"] or base["word"]
        ph = (base["phonetic"] or row["phonetic"] or "").strip()
        return {
            "lemma": lemma,
            "phonetic": ("/%s/" % ph) if ph else "",
            "pos": (base["pos"] or "").strip(),
            "definition": base["translation"] or row["translation"],
            "definition_en": base["en"] or row["en"] or "",
            "meaning_in_context": "",
            "example": "",
            "_frq": min([x for x in (base["frq"], base["bnc"]) if x] or [0]),
            "_collins": base["collins"] or 0,
        }
    finally:
        d.close()


def lookup(word, sentence, cfg, quiet=False):
    """统一查词入口：离线词典优先（免费、毫秒级），查不到且配了 key 才问 AI。"""
    d = dict_lookup(word)
    if d:
        return d
    if cfg.get("api_key"):
        return ai_lookup(word, sentence, cfg, quiet=quiet)
    return None


def cmd_dict(args, cfg):
    d = dict_db()
    if args.action == "status":
        if not d:
            print(red("还没装离线词典。"))
            print(dim("  v dict import <ecdict.csv 的路径>"))
            return 1
        n = d.execute("SELECT COUNT(*) FROM dict").fetchone()[0]
        size = os.path.getsize(DICT_PATH) / 1048576.0
        print("  离线词典 %s 条  ·  %.0f MB  ·  %s" % (bold(str(n)), size, DICT_PATH))
        for w in ("rescind", "ubiquitous", "mitigate"):
            r = dict_lookup(w)
            if r:
                print(dim("  %s %s %s" % (w, r["phonetic"], (r["definition"] or "")[:40])))
        d.close()
        return 0
    if not args.path or not os.path.exists(args.path):
        print(red("文件不存在：%s" % args.path))
        return 1
    print(dim("  导入中，大约要一分钟…"))
    n = dict_import(args.path)
    print(green("  离线词典就绪：%d 条" % n))
    return 0


# ---------------------------------------------------------------- 剪贴板抓词

POS_PREFIX_RE = re.compile(
    r"^(?:n|v|a|s|r|adj|adv|vt|vi|prep|conj|pron|art|num|int)\.?\s+", re.I)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")
SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]|ghp_|gho_|xox[baprs]-|AKIA[0-9A-Z]|BEGIN [A-Z ]*PRIVATE KEY"
    r"|api[_\- ]?key|passwo?rd|secret|bearer |token)", re.I)
URLISH_RE = re.compile(r"(https?://|www\.|/Users/|/etc/|~/|[A-Za-z]:\\)")
CODEISH_RE = re.compile(r"[{}();=<>|&@#$%^*\[\]\\/_]")
SENT_END_RE = re.compile(r"[.!?][\"')\]]?\s+$")


def clip_reject(text, cfg):
    """能处理返回 None，否则返回不处理的原因。"""
    t = (text or "").strip()
    if not t:
        return "空"
    if len(t) > cfg["max_clip_chars"]:
        return "太长(%d 字符)" % len(t)
    letters = sum(1 for ch in t if ch.isascii() and ch.isalpha())
    if letters < 3:
        return "没有英文"
    if letters / float(len(t)) < 0.5:
        return "英文占比低"
    if SECRET_RE.search(t):
        return "疑似密钥/密码，已忽略"
    if URLISH_RE.search(t):
        return "像链接或路径"
    if len(CODEISH_RE.findall(t)) / float(len(t)) > 0.06:
        return "像代码"
    if len(t.split()) == 1 and len(t) >= 20:
        return "像随机串"
    return None


def extract_candidates(conn, text, cfg):
    """返回 (fresh, bumped)。fresh 需要问 AI；bumped 是库里已有、已就地 +1 的。"""
    freq_seed(conn)
    single = len(text.split()) == 1
    fresh, bumped, seen = [], [], set()
    for i, m in enumerate(WORD_RE.finditer(text)):
        raw = m.group(0)
        w = normalize(raw)
        if len(w) < 3 or w in seen:
            continue
        seen.add(w)
        # 句中的大写词按专有名词跳过（句首除外）
        if raw[0].isupper() and i > 0 and not SENT_END_RE.search(text[:m.start()]):
            continue
        row = find_word(conn, w) or find_by_stem(conn, w)
        if row:
            st = conn.execute(
                "SELECT status FROM reviews WHERE word_id = ?", (row["id"],)).fetchone()
            if st and st["status"] == "known":
                continue
            _bump(conn, row, w, "" if single else text, "剪贴板", None)
            bumped.append(row["word"])
            continue
        if conn.execute(
                "SELECT 1 FROM inbox WHERE word = ? AND status IN ('pending','rejected')",
                (w,)).fetchone():
            continue
        rank = freq_rank(conn, w)
        if rank is not None and rank <= cfg["freq_skip_rank"] and not single:
            continue
        fresh.append(w)
    return fresh[: cfg["max_candidates"]], bumped


AI_JUDGE_SYSTEM = (
    "你是一个英语词汇助手，服务对象是%s。"
    "只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释性文字。"
)

AI_JUDGE_USER = """句子：{sentence}

候选词：{words}

对每个候选词判断：对这位学习者来说，它（在这个句子里的这个用法）是不是值得收藏学习的生词？
注意：常见词的生僻用法也算生词（例如 "the play had a long run" 里的 run）。
返回：
{{"words": [
  {{"word": "候选词原样",
    "lemma": "原形",
    "score": 0.0,
    "phonetic": "美式音标，带斜杠",
    "pos": "词性缩写",
    "definition": "常用中文释义，1-2 条，用；分隔",
    "meaning_in_context": "它在这个句子里的具体意思（中文，一句话）"}}
]}}
score 表示这位学习者不认识或不熟悉该用法的可能性，0 到 1。
认识的常见词给低分，不用填释义。"""


def ai_judge(sentence, words, cfg, quiet=False):
    if not cfg.get("api_key") or not words:
        return []
    payload = {
        "model": cfg.get("model"),
        "messages": [
            {"role": "system", "content": AI_JUDGE_SYSTEM % cfg.get("level")},
            {"role": "user", "content": AI_JUDGE_USER.format(
                sentence=sentence, words="、".join(words))},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    url = cfg.get("api_base", "").rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg["api_key"]}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        data = _parse_json(body["choices"][0]["message"]["content"])
    except Exception as e:
        if not quiet:
            _clear_line()
            print(dim("  AI 判定失败: %s" % e))
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("words")
    return items if isinstance(items, list) else []


def process_text(conn, text, cfg, source="剪贴板"):
    """一段文本 -> (新入库, 进inbox, 已有词+1, 说明)"""
    reason = clip_reject(text, cfg)
    if reason:
        return 0, 0, [], reason
    fresh, bumped = extract_candidates(conn, text, cfg)
    conn.commit()
    if not fresh:
        return 0, 0, bumped, None if bumped else "没有候选生词"

    single = len(text.split()) == 1
    sentence = "" if single else text.strip()
    added = pending = 0

    if not cfg.get("api_key"):
        # 查离线词典填好释义，直接入库
        for w in fresh:
            d = dict_lookup(w) or {}
            lemma = d.get("lemma") or w
            row = find_word(conn, lemma)
            if row:
                _bump(conn, row, w, sentence, source, None)
                bumped.append(row["word"])
                continue
            insert_word(conn, lemma, d, w, sentence, source)
            added += 1
        conn.commit()
        return added, 0, bumped, None

    for it in ai_judge(sentence or fresh[0], fresh, cfg):
        w = normalize(str(it.get("word") or ""))
        if not w:
            continue
        try:
            score = float(it.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        # 你专门复制了单独一个词 = 明确的「我要这个」，最差也进 inbox，不静默丢弃
        if score < cfg["inbox_min"] and not single:
            continue
        lemma = normalize(str(it.get("lemma") or "")) or w
        row = find_word(conn, lemma)
        if row:
            _bump(conn, row, w, sentence, source, it.get("meaning_in_context"))
            bumped.append(row["word"])
            continue
        if not it.get("definition"):
            it = dict(it, **{k: v for k, v in (dict_lookup(lemma) or {}).items()
                             if k in ("phonetic", "pos", "definition") and v})
        insert_word(conn, lemma, it, w, sentence, source)
        added += 1
    conn.commit()
    return added, pending, bumped, None


def notify(title, msg, cfg):
    if not cfg.get("notify") or not shutil.which("osascript"):
        return
    clean = lambda t: re.sub(r'["\\\\]', "", str(t))[:120]
    try:
        subprocess.run(
            ["osascript", "-e",
             'display notification "%s" with title "%s"' % (clean(msg), clean(title))],
            timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def read_clipboard():
    try:
        out = subprocess.run(["pbpaste"], timeout=5, stdout=subprocess.PIPE)
        return out.stdout.decode("utf-8", "ignore")
    except Exception:
        return ""


def post_capture(base, text, quiet=True):
    """把剪贴板内容发给远端服务器，由它跑抓词流水线。"""
    from urllib.parse import urlsplit, urlunsplit
    u = urlsplit(base)
    url = urlunsplit((u.scheme or "http", u.netloc, "/api/capture", u.query, ""))
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        if not quiet:
            print(dim("  发送失败: %s" % str(e)[:70]))
        return None


def cmd_watch(args, cfg):
    has_clip = bool(shutil.which("pbpaste"))   # 服务器（Linux）没有剪贴板
    remote = (args.server or os.environ.get("VOCAB_SERVER") or "").strip()
    conn = db()

    def handle(text, show_skip):
        if remote:                              # 词交给服务器，本机不存
            r = post_capture(remote, text, quiet=not show_skip)
            if r and (r.get("added") or r.get("bumped")):
                print("  %s  %s" % (
                    green("上传 %d" % (r.get("added") or 0)),
                    dim((text.strip().replace("\n", " "))[:46])))
            elif show_skip:
                print(dim("  跳过  %s" % (text.strip()[:46])))
            return
        if is_selfcopy(text):
            if show_skip:
                print(dim("  跳过（网页刚复制的，不重复计数）  %s" % text.strip()[:40]))
            return
        added, pending, bumped, reason = process_text(conn, text, cfg)
        head = (text.strip().replace("\n", " "))[:46]
        if reason:
            if show_skip:
                print(dim("  跳过（%s）  %s" % (reason, head)))
            return
        parts = []
        if added:
            parts.append(green("入库 %d" % added))
        if pending:
            parts.append(yellow("待确认 %d" % pending))
        if bumped:
            parts.append(dim("已有词 +1: " + " ".join(sorted(set(bumped))[:5])))
        if parts:
            print("  %s  %s" % ("  ".join(parts), dim(head)))
            if added or pending:
                notify("生词本", "入库 %d，待确认 %d" % (added, pending), cfg)

    if args.once:
        if not has_clip:
            print(red("这台机器没有剪贴板"))
            return 1
        handle(read_clipboard(), True)
        return 0

    gc_orphans(conn)
    dict_backfill(conn)
    if has_clip:
        print(dim("盯着剪贴板…  复制一句英文就自动抽生词  ·  Ctrl-C 退出"))
    else:
        print(dim("没有剪贴板（非 macOS），只跑新闻抓取和例句生成  ·  Ctrl-C 退出"))
    last = hashlib.md5(read_clipboard().encode("utf-8", "ignore")).hexdigest() if has_clip else ""
    tick = 0
    while True:
        time.sleep(cfg["watch_interval"])
        tick += 1
        # 连着服务器时，本机只管抓词上传；下面这些后台活儿由服务器那边跑，
        # 否则两边都做，白烧一份 token
        if remote:
            if not has_clip:
                continue
            try:
                cur = read_clipboard()
            except Exception:
                continue
            h = hashlib.md5(cur.encode("utf-8", "ignore")).hexdigest()
            if h == last:
                continue
            last = h
            try:
                handle(cur, args.verbose)
            except Exception as e:
                print(dim("  处理出错: %s: %s" % (type(e).__name__, e)))
            continue
        # 每天到点抓新闻（电脑睡着错过了，醒来会补上）
        if tick % 60 == 0:
            try:
                need = news_due(conn, cfg)
                if need:
                    got = news_fetch(conn, need)
                    if got:
                        print("  %s 今天 %d 条" % (green("新闻"), got))
                        notify("生词本", "今天的 %d 条科技商业新闻到了" % got, cfg)
            except Exception as e:
                print(dim("  新闻抓取出错: %s" % e))
        # 词根分析 + 同根词族，一次一批
        if cfg.get("api_key") and tick % 30 == 0:
            try:
                if not roots_analyze(conn, cfg, 20):
                    todo = [r[0] for r in conn.execute(
                        "SELECT DISTINCT wr.root FROM word_root wr"
                        " LEFT JOIN root_family f ON f.root = wr.root"
                        " WHERE wr.root != '' AND f.root IS NULL LIMIT 12")]
                    if todo and root_family_fill(conn, cfg, todo):
                        print("  %s %d 组" % (green("词族"), len(todo)))
            except Exception as e:
                print(dim("  词根分析出错: %s" % e))
        # 新闻摘要，一次一条
        if cfg.get("api_key") and tick % 20 == 0:
            try:
                todo = news_without_summary(conn, 1)
                if todo:
                    if news_summarize(conn, todo[0], cfg):
                        print("  %s %s" % (green("摘要"), todo[0]["title"][:44]))
                    else:
                        conn.execute("UPDATE news SET ai_en = '-' WHERE id = ?",
                                     (todo[0]["id"],))
                        conn.commit()
            except Exception as e:
                print(dim("  摘要出错: %s" % e))
        # 空闲时顺手把缺例句的词补上，一次一个，别把接口打爆
        if tick % 10 == 0:
            cfg = load_cfg()   # 你在网页 ⚙ 里改了密钥/模型，这里要能立刻读到
        if cfg.get("api_key") and tick % 10 == 0:
            try:
                todo = words_without_examples(conn, 1)
                if todo:
                    n = gen_examples(conn, todo[0], cfg)
                    if n:
                        print("  %s %s %s" % (green("例句"), todo[0]["word"],
                                              dim("%d 句" % n)))
                    else:
                        conn.execute(
                            "INSERT INTO examples (word_id, en, zh, created_at)"
                            " VALUES (?,'','',?)", (todo[0]["id"], now_iso()))
                        conn.commit()
            except Exception as e:
                print(dim("  例句生成出错: %s" % e))
        if not has_clip:
            continue                       # 服务器上只跑上面那些后台任务
        try:
            cur = read_clipboard()
        except Exception:
            continue
        h = hashlib.md5(cur.encode("utf-8", "ignore")).hexdigest()
        if h == last:
            continue
        last = h
        try:
            handle(cur, args.verbose)
        except Exception as e:
            print(dim("  处理出错: %s: %s" % (type(e).__name__, e)))


# ---------------------------------------------------------------- inbox

def _accept_inbox(conn, r):
    row = find_word(conn, r["lemma"] or r["word"])
    if row:
        _bump(conn, row, r["word"], r["sentence"] or "", r["source"], r["meaning"])
    else:
        insert_word(conn, r["lemma"] or r["word"],
                    {"phonetic": r["phonetic"], "pos": r["pos"],
                     "definition": r["definition"], "meaning_in_context": r["meaning"]},
                    r["word"], r["sentence"] or "", r["source"])
    conn.execute("UPDATE inbox SET status = 'accepted' WHERE id = ?", (r["id"],))


def cmd_inbox(args, cfg):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM inbox WHERE status = 'pending' ORDER BY score DESC, id ASC").fetchall()
    if not rows:
        print(green("待确认列表是空的。"))
        return 0

    if args.clear:
        conn.execute("UPDATE inbox SET status = 'rejected' WHERE status = 'pending'")
        conn.commit()
        print("已丢弃 %d 个待确认的词" % len(rows))
        return 0
    if args.accept_all:
        for r in rows:
            _accept_inbox(conn, r)
        conn.commit()
        print(green("已全部收下 %d 个词" % len(rows)))
        return 0

    print(dim("%d 个待确认  ·  y 收下  n 丢弃  a 全部收下  q 退出\n" % len(rows)))
    kept = dropped = 0
    for i, r in enumerate(rows, 1):
        sc = ("%.0f%%" % (r["score"] * 100)) if r["score"] is not None else "—"
        print("%s %s %s" % (dim("%d/%d" % (i, len(rows))), bold(cyan(r["word"])),
                            dim("生词分 " + sc)))
        if r["definition"]:
            print("   %s" % r["definition"])
        if r["sentence"]:
            print("   %s" % dim(r["sentence"][:110]))
        if r["meaning"]:
            print("   %s" % dim("→ " + r["meaning"]))
        k = ""
        while k not in ("y", "n", "a", "q"):
            print("   %s " % dim("y/n/a/q"), end="", flush=True)
            k = (getch() or "q").lower()
            print("\r" + " " * 24, end="\r")
        if k == "q":
            break
        if k == "a":
            for rest in rows[i - 1:]:
                _accept_inbox(conn, rest)
                kept += 1
            conn.commit()
            break
        if k == "y":
            _accept_inbox(conn, r)
            kept += 1
        else:
            conn.execute("UPDATE inbox SET status = 'rejected' WHERE id = ?", (r["id"],))
            dropped += 1
        conn.commit()
        print()
    print(dim("\n收下 %d 个，丢弃 %d 个。" % (kept, dropped)))
    return 0


# ---------------------------------------------------------------- 例句

AI_EX_SYSTEM = (
    "你是一个英语例句老师，服务对象是%s。"
    "只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释性文字。"
)

AI_EX_USER = """单词：{word}
释义：{definition}

给这个词造 3 个短例句，帮助理解它怎么用。要求：
- 每句不超过 12 个单词，用日常场景，别用生僻词
- 3 句尽量覆盖这个词的不同常见用法或搭配
- 每句配一句自然的中文翻译

返回：
{{"examples": [{{"en": "英文句子", "zh": "中文翻译"}}]}}"""


def gen_examples(conn, w, cfg, quiet=True):
    """给一个词生成 3 个例句，返回生成条数。"""
    if not cfg.get("api_key"):
        return 0
    payload = {
        "model": cfg.get("model"),
        "messages": [
            {"role": "system", "content": AI_EX_SYSTEM % cfg.get("level")},
            {"role": "user", "content": AI_EX_USER.format(
                word=w["word"], definition=w["definition"] or "（无）")},
        ],
        "temperature": 0.6,
        "response_format": {"type": "json_object"},
    }
    url = cfg.get("api_base", "").rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg["api_key"]}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        data = _parse_json(body["choices"][0]["message"]["content"])
    except Exception as e:
        if not quiet:
            _clear_line()
            print(dim("  %s 例句生成失败: %s" % (w["word"], e)))
        return 0
    items = (data or {}).get("examples")
    if not isinstance(items, list):
        return 0
    n = 0
    for it in items[:3]:
        en = (it.get("en") or "").strip()
        if not en:
            continue
        conn.execute(
            "INSERT INTO examples (word_id, en, zh, created_at) VALUES (?,?,?,?)",
            (w["id"], en, (it.get("zh") or "").strip(), now_iso()))
        n += 1
    if n:
        conn.commit()
    return n


def words_without_examples(conn, limit=500):
    return conn.execute(
        "SELECT w.* FROM words w LEFT JOIN examples e ON e.word_id = w.id"
        " WHERE e.id IS NULL GROUP BY w.id ORDER BY w.updated_at DESC LIMIT ?",
        (limit,)).fetchall()


def examples_of(conn, word_id, limit=3):
    return [dict(r) for r in conn.execute(
        "SELECT en, zh FROM examples WHERE word_id = ? AND en != '' ORDER BY id LIMIT ?",
        (word_id, limit))]


def cmd_examples(args, cfg):
    conn = db()
    if not cfg.get("api_key"):
        print(red("生成例句需要 AI。"))
        print(dim("  打开生词本网页 → 设置 → 填上密钥，或者跑 v key"))
        return 1
    rows = words_without_examples(conn, args.limit)
    if not rows:
        print(green("每个词都有例句了。"))
        return 0
    print(dim("给 %d 个词生成例句…" % len(rows)))
    ok = 0
    for w in rows:
        n = gen_examples(conn, w, cfg, quiet=False)
        if n:
            ok += 1
            ex = examples_of(conn, w["id"], 1)
            print("  %s %-14s %s" % (green("✓"), w["word"], dim(ex[0]["en"] if ex else "")))
        else:
            print("  %s %s" % (red("×"), w["word"]))
    print(dim("\n完成 %d/%d" % (ok, len(rows))))
    return 0


# ---------------------------------------------------------------- 词根

ROOT_SYSTEM = ("你是一位词源学老师，服务对象是%s。"
               "只输出一个 JSON 对象，不要 markdown 代码块，不要解释性文字。")

ROOT_USER = """分析这些英语单词的构词：{words}

对每个词判断它有没有清晰的拉丁/希腊词根。古英语来源的常用词（get、run、bear 之类）
没有可拆的词根，root 填空字符串。

返回：
{{"items": [
  {{"word": "原词",
    "root": "核心词根，如 tract；没有就填空",
    "variants": "该词根的常见拼写变体，逗号分隔，如 tract,treat",
    "meaning": "词根的中文意思，2-6 个字，如 拉、拖",
    "breakdown": "拆解，如 con-(共同) + tract(拉) + -or(人) → 一起拉合约的人"}}
]}}"""


def roots_analyze(conn, cfg, limit=20, quiet=True):
    """一次问一批词的构词，省调用次数。"""
    if not cfg.get("api_key"):
        return 0
    rows = conn.execute(
        "SELECT w.word FROM words w LEFT JOIN word_root r ON r.word = w.word"
        " WHERE r.word IS NULL ORDER BY w.id LIMIT ?", (limit,)).fetchall()
    if not rows:
        return 0
    words = [r[0] for r in rows]
    payload = {
        "model": cfg.get("model"),
        "messages": [
            {"role": "system", "content": ROOT_SYSTEM % cfg.get("level")},
            {"role": "user", "content": ROOT_USER.format(words="、".join(words))},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    try:
        req = urllib.request.Request(
            cfg.get("api_base", "").rstrip("/") + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + cfg["api_key"]}, method="POST")
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = _parse_json(json.loads(resp.read().decode("utf-8"))
                               ["choices"][0]["message"]["content"])
    except Exception as e:
        if not quiet:
            print(dim("  词根分析失败: %s" % str(e)[:60]))
        return 0
    items = (data or {}).get("items")
    if not isinstance(items, list):
        return 0
    got = {}
    for it in items:
        w = normalize(str(it.get("word") or ""))
        if w:
            got[w] = it
    n = 0
    for w in words:                       # 没返回的也占个位，免得反复问
        it = got.get(w, {})
        conn.execute(
            "INSERT OR REPLACE INTO word_root (word, root, variants, meaning,"
            " breakdown, created_at) VALUES (?,?,?,?,?,?)",
            (w, normalize(str(it.get("root") or "")), str(it.get("variants") or ""),
             str(it.get("meaning") or ""), str(it.get("breakdown") or ""), now_iso()))
        n += 1
    conn.commit()
    return n


FAMILY_USER = """这些是英语词根：{roots}

对每个词根，列出 10 个确实由它派生的常用英语单词。
只要词源上真的同根的（比如 vor「吃」是 carnivore、devour、voracious，
不是 favor、ivory 这种只是碰巧含有这几个字母的）。

返回：
{{"items": [{{"root": "词根", "words": "逗号分隔的 10 个单词"}}]}}"""


def root_family_fill(conn, cfg, roots, quiet=True):
    """让 AI 给出真正同根的词族——字符串匹配会把 favor 算成 vor 的同根词。"""
    todo = [r for r in roots if not conn.execute(
        "SELECT 1 FROM root_family WHERE root = ?", (r,)).fetchone()]
    if not todo or not cfg.get("api_key"):
        return 0
    todo = todo[:12]
    payload = {
        "model": cfg.get("model"),
        "messages": [
            {"role": "system", "content": ROOT_SYSTEM % cfg.get("level")},
            {"role": "user", "content": FAMILY_USER.format(roots="、".join(todo))},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    try:
        req = urllib.request.Request(
            cfg.get("api_base", "").rstrip("/") + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + cfg["api_key"]}, method="POST")
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = _parse_json(json.loads(resp.read().decode("utf-8"))
                               ["choices"][0]["message"]["content"])
    except Exception as e:
        if not quiet:
            print(dim("  词族生成失败: %s" % str(e)[:60]))
        return 0
    got = {normalize(str(i.get("root") or "")): str(i.get("words") or "")
           for i in ((data or {}).get("items") or [])}
    n = 0
    for r in todo:
        conn.execute(
            "INSERT OR REPLACE INTO root_family (root, words, created_at) VALUES (?,?,?)",
            (r, got.get(r, ""), now_iso()))
        n += 1
    conn.commit()
    return n


def root_related(conn, root, exclude, limit=10):
    """同根词族里，你还没收的那些。词形以离线词典为准，AI 编的假词会被剔掉。"""
    row = conn.execute("SELECT words FROM root_family WHERE root = ?", (root,)).fetchone()
    if not row or not row[0]:
        return []
    out = []
    for w in [normalize(x) for x in row[0].split(",")]:
        if not w or w in exclude or any(o["word"] == w for o in out):
            continue
        e = dict_lookup(w)
        if not e or not e.get("definition"):
            continue                       # 词典里查不到就是 AI 编的，丢掉
        out.append({"word": w, "zh": (e.get("definition") or "")[:40]})
        if len(out) >= limit:
            break
    return out


def roots_view(conn, cfg, min_words=1):
    """按词根分组：每组挂你库里的同根词 + 词典里的关联词。"""
    groups = {}
    for r in conn.execute(
            "SELECT wr.word, wr.root, wr.variants, wr.meaning, wr.breakdown"
            " FROM word_root wr JOIN words w ON w.word = wr.word"
            " WHERE wr.root != '' ORDER BY wr.root, wr.word"):
        g = groups.setdefault(r["root"], {"root": r["root"], "meaning": r["meaning"],
                                          "variants": r["variants"], "mine": []})
        g["mine"].append({"word": r["word"], "breakdown": r["breakdown"]})
    known = {x[0] for x in conn.execute("SELECT word FROM words")}
    known |= {x[0] for x in conn.execute("SELECT word FROM inbox")}
    known |= {x[0] for x in conn.execute("SELECT word FROM pick WHERE status='skipped'")}
    keep = [g for g in groups.values() if len(g["mine"]) >= min_words]
    out = []
    for g in keep:
        g["related"] = root_related(conn, g["root"], known)
        out.append(g)
    out.sort(key=lambda x: -len(x["mine"]))
    return out


def cmd_roots(args, cfg):
    conn = db()
    if args.analyze:
        total = 0
        while True:
            n = roots_analyze(conn, cfg, 20, quiet=False)
            if not n:
                break
            total += n
            print(dim("  已分析 %d 个…" % total))
        print(green("  分析完成 %d 个词" % total))
    for g in roots_view(conn, cfg, args.min):
        print("\n%s %s  %s" % (bold(g["root"]), dim(g["meaning"]),
                                dim("(%d 个)" % len(g["mine"]))))
        print("  你的: " + ", ".join(m["word"] for m in g["mine"]))
        if g["related"]:
            print("  " + dim("关联: " + ", ".join(r["word"] for r in g["related"])))
    return 0


# ---------------------------------------------------------------- 推荐词

PICK_FRQ_LO = 5500        # 比这更常见的词，你多半已经会了
PICK_FRQ_HI = 22000       # 比这更冷僻的，学了用不上


def pick_batch(conn, cfg, size=None):
    """凑够今天要推荐的一批词。已收藏、已跳过、已推过的都不再出现。"""
    size = size or int(cfg.get("pick_size", 50))
    day = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT word, status FROM pick WHERE status IN ('pending','want')"
        " ORDER BY rowid").fetchall()
    need = size - len(rows)
    if need > 0:
        d = dict_db()
        if d:
            try:
                used = {r[0] for r in conn.execute("SELECT word FROM pick")}
                used |= {r[0] for r in conn.execute("SELECT word FROM words")}
                used |= {r[0] for r in conn.execute("SELECT word FROM inbox")}
                has_tag = [r[1] for r in d.execute("PRAGMA table_info(dict)")]
                if "tag" in has_tag:
                    # 优先出考试大纲里的词（六级/考研/托福/雅思/GRE），比按词频瞎抽准得多
                    cand = d.execute(
                        "SELECT word FROM dict"
                        " WHERE lemma = word AND translation != '' AND en != ''"
                        "   AND (tag LIKE '%cet6%' OR tag LIKE '%ky%'"
                        "        OR tag LIKE '%toefl%' OR tag LIKE '%ielts%'"
                        "        OR tag LIKE '%gre%')"
                        "   AND frq BETWEEN ? AND ?"
                        " ORDER BY RANDOM() LIMIT ?",
                        (PICK_FRQ_LO, PICK_FRQ_HI, need * 4)).fetchall()
                else:
                    cand = d.execute(
                        "SELECT word FROM dict"
                        " WHERE lemma = word AND translation != '' AND en != ''"
                        "   AND frq BETWEEN ? AND ?"
                        " ORDER BY RANDOM() LIMIT ?",
                        (PICK_FRQ_LO, PICK_FRQ_HI, need * 4)).fetchall()
            finally:
                d.close()
            fresh = []
            for r in cand:
                if r[0] in used or r[0] in fresh:
                    continue
                fresh.append(r[0])
                if len(fresh) >= need:
                    break
            for w in fresh:
                conn.execute(
                    "INSERT OR IGNORE INTO pick (word, day, status, created_at)"
                    " VALUES (?,?,'pending',?)", (w, day, now_iso()))
            conn.commit()
        rows = conn.execute(
            "SELECT word, status FROM pick WHERE status IN ('pending','want')"
            " ORDER BY rowid").fetchall()

    out = []
    for r in rows[:size]:
        e = dict_lookup(r[0]) or {}
        out.append({"word": r[0], "phonetic": e.get("phonetic") or "",
                    "en": e.get("definition_en") or "",
                    "zh": e.get("definition") or "",
                    "selected": r[1] == "want"})
    return out


def cmd_pick(args, cfg):
    conn = db()
    if args.reset:
        n = conn.execute("DELETE FROM pick WHERE status = 'pending'").rowcount
        conn.commit()
        print(dim("  清掉 %d 个待选的，重新出题" % n))
    items = pick_batch(conn, cfg, args.size)
    print(dim("  今天推荐 %d 个词：" % len(items)))
    for i, it in enumerate(items, 1):
        print("  %2d. %-16s %-14s %s" % (i, it["word"], it["phonetic"],
                                         (it["en"] or it["zh"])[:46]))
    return 0


# ---------------------------------------------------------------- 商业新闻

# 科技类商业新闻
NEWS_FEEDS = [
    ("TechCrunch", "https://techcrunch.com/feed/"),
    ("CNBC Tech",  "https://search.cnbc.com/rs/search/combinedcms/view.xml"
                   "?partnerId=wrss01&id=19854910"),
    ("The Verge",  "https://www.theverge.com/rss/index.xml"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/business"),
    ("Wired",      "https://www.wired.com/feed/category/business/latest/rss"),
]

ATOM = "{http://www.w3.org/2005/Atom}"

TAG_RE = re.compile(r"<[^>]+>")


def _clean(txt):
    import html as _html
    t = TAG_RE.sub(" ", txt or "")
    t = _html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _pubdate(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    try:                                    # Atom 用 ISO 8601
        return datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S").strftime("%Y-%m-%d %H:%M")
    except Exception:
        return raw[:16]


def news_fetch(conn, limit=10, quiet=True, day=None):
    """从各家 RSS 抓当天的商业新闻。只存标题和媒体自己发布的摘要，正文去原站看。"""
    import xml.etree.ElementTree as ET
    got = []
    for name, url in NEWS_FEEDS:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (vocab local reader)"})
            raw = urllib.request.urlopen(req, timeout=15).read()
            root = ET.fromstring(raw)
        except Exception as e:
            if not quiet:
                print(dim("  %s 抓取失败: %s" % (name, str(e)[:60])))
            continue
        items = root.findall(".//item")
        atom = False
        if not items:
            items = root.findall(".//" + ATOM + "entry")
            atom = True
        for it in items[:15]:
            if atom:
                title = _clean(it.findtext(ATOM + "title"))
                ln = it.find(ATOM + "link")
                link = (ln.get("href") if ln is not None else "") or ""
                summary = _clean(it.findtext(ATOM + "summary")
                                 or it.findtext(ATOM + "content") or "")
                pub = _pubdate(it.findtext(ATOM + "published")
                               or it.findtext(ATOM + "updated"))
            else:
                title = _clean(it.findtext("title"))
                link = (it.findtext("link") or "").strip()
                summary = _clean(it.findtext("description"))
                pub = _pubdate(it.findtext("pubDate"))
            if not title or not link:
                continue
            got.append({"source": name, "title": title, "summary": summary[:900],
                        "link": link.strip(), "published": pub})

    # 各家轮流取，别让某一家刷屏
    by_src = {}
    seen = set()
    for g in got:
        if g["link"] in seen:
            continue
        seen.add(g["link"])
        by_src.setdefault(g["source"], []).append(g)
    for v in by_src.values():
        v.sort(key=lambda x: x["published"], reverse=True)
    uniq, i = [], 0
    while any(len(v) > i for v in by_src.values()):
        for name, _ in NEWS_FEEDS:
            v = by_src.get(name) or []
            if len(v) > i:
                uniq.append(v[i])
        i += 1

    day = day or datetime.now().strftime("%Y-%m-%d")
    n = 0
    for g in uniq:
        if n >= limit:
            break
        if conn.execute("SELECT 1 FROM news WHERE link = ?", (g["link"],)).fetchone():
            continue
        conn.execute(
            "INSERT INTO news (day, title, summary, link, source, published, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (day, g["title"], g["summary"], g["link"], g["source"],
             g["published"], now_iso()))
        n += 1
    conn.commit()
    return n


def article_text(url):
    """把正文段落抓下来，只用于喂给 AI 做摘要，不入库。"""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
                          "Accept-Language": "en-US,en;q=0.9"})
        raw = urllib.request.urlopen(req, timeout=20).read()
    except Exception:
        return ""
    html = raw.decode("utf-8", "ignore")
    html = re.sub(r"(?is)<(script|style|noscript|nav|header|footer|aside|form)[^>]*>.*?</\1>",
                  " ", html)
    ps = [_clean(x) for x in re.findall(r"(?is)<p[^>]*>(.*?)</p>", html)]
    ps = [x for x in ps if len(x) > 60]
    return " ".join(ps)[:7000]


NEWS_SUM_SYSTEM = (
    "You are a news editor writing study material for %s. "
    "Output one JSON object only, no markdown fences, no extra text."
)

NEWS_SUM_USER = """Title: {title}

Article:
{body}

Write a self-contained summary in YOUR OWN WORDS so the reader never needs the original.
Return:
{{"en": "about 130 words of clear English: what happened, the key numbers, who is affected, why it matters. Plain vocabulary, short sentences.",
  "zh": "一句话中文要点，20 字以内"}}"""


def news_summarize(conn, row, cfg, quiet=True):
    if not cfg.get("api_key"):
        return False
    body = article_text(row["link"])
    if len(body) < 400:                       # 付费墙或纯 JS 页面，退回用 RSS 摘要
        body = row["summary"] or ""
    if len(body) < 80:
        return False
    payload = {
        "model": cfg.get("model"),
        "messages": [
            {"role": "system", "content": NEWS_SUM_SYSTEM % cfg.get("level")},
            {"role": "user", "content": NEWS_SUM_USER.format(
                title=row["title"], body=body[:7000])},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    try:
        req = urllib.request.Request(
            cfg.get("api_base", "").rstrip("/") + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + cfg["api_key"]}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _parse_json(json.loads(resp.read().decode("utf-8"))
                               ["choices"][0]["message"]["content"])
    except Exception as e:
        if not quiet:
            print(dim("  摘要失败 %s: %s" % (row["title"][:30], str(e)[:50])))
        return False
    en = ((data or {}).get("en") or "").strip()
    if not en:
        return False
    conn.execute("UPDATE news SET ai_en = ?, ai_zh = ? WHERE id = ?",
                 (en, ((data or {}).get("zh") or "").strip(), row["id"]))
    conn.commit()
    return True


def news_without_summary(conn, limit=20):
    return conn.execute(
        "SELECT * FROM news WHERE ai_en IS NULL OR ai_en = ''"
        " ORDER BY day DESC, published DESC LIMIT ?", (limit,)).fetchall()


def news_due(conn, cfg, now=None):
    """到了设定的钟点、且今天还没抓够，就返回还缺几条；否则 0。"""
    now = now or datetime.now()
    if now.hour < int(cfg.get("news_hour", 6)):
        return 0
    day = now.strftime("%Y-%m-%d")
    have = conn.execute("SELECT COUNT(*) FROM news WHERE day = ?", (day,)).fetchone()[0]
    return max(0, int(cfg.get("news_count", 10)) - have)


def cmd_preview(args, cfg):
    """在本机起一个隔离的预览实例：改完先看，满意了再部署到服务器。

    用独立的数据目录 ~/.vocab/preview，词典做软链省空间，
    词库每次从正式库拷一份 —— 在里面随便点、随便删都不会影响真实数据。
    """
    import subprocess as sp
    home = os.path.join(VOCAB_HOME, "preview")
    os.makedirs(home, exist_ok=True)

    link = os.path.join(home, "dict.db")
    if not os.path.exists(link):
        if not os.path.exists(DICT_PATH):
            print(red("本机没有离线词典，预览里查不到释义"))
        else:
            os.symlink(DICT_PATH, link)

    pv_db = os.path.join(home, "vocab.db")
    if args.fresh or not os.path.exists(pv_db):
        for suffix in ("", "-wal", "-shm"):
            src = DB_PATH + suffix
            if os.path.exists(src):
                shutil.copy(src, pv_db + suffix)
        print(dim("  已从正式库拷一份数据到预览环境"))

    print(green("  预览环境（改动只影响这里，不动真实数据）"))
    print(dim("  数据目录 %s" % home))
    env = dict(os.environ, VOCAB_HOME=home)
    env.pop("VOCAB_TOKEN", None)
    cmd = [sys.executable, os.path.abspath(__file__), "serve", "-p", str(args.port)]
    if not args.no_open:
        cmd.append("--open")
    try:
        sp.run(cmd, env=env)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_export(args, cfg):
    """把整个词库导成 JSON —— 备份、换机器、部署到服务器都用它。"""
    conn = db()
    out = []
    for w in conn.execute("SELECT * FROM words ORDER BY id"):
        r = conn.execute("SELECT * FROM reviews WHERE word_id=?", (w["id"],)).fetchone()
        out.append({
            "word": w["word"], "lemma": w["lemma"], "phonetic": w["phonetic"],
            "pos": w["pos"], "definition": w["definition"],
            "definition_en": w["definition_en"],
            "study_count": w["study_count"], "encounter_count": w["encounter_count"],
            "created_at": w["created_at"],
            "examples": [dict(e) for e in conn.execute(
                "SELECT en, zh FROM examples WHERE word_id=? AND en!='' ORDER BY id",
                (w["id"],))],
            "contexts": [dict(c) for c in conn.execute(
                "SELECT sentence, meaning, source FROM contexts WHERE word_id=? ORDER BY id",
                (w["id"],))],
            "review": {k: r[k] for k in
                       ("status", "due_date", "interval", "ease", "reps", "lapses")} if r else None,
        })
    data = {"version": 1, "exported_at": now_iso(), "count": len(out), "words": out}
    path = args.path or os.path.join(VOCAB_HOME, "words.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(green("已导出 %d 个词 -> %s" % (len(out), path)))
    return 0


def cmd_import(args, cfg):
    """从 JSON 导回词库。已存在的词跳过，不覆盖。"""
    if not args.path or not os.path.exists(args.path):
        print(red("文件不存在：%s" % args.path))
        return 1
    with open(args.path, encoding="utf-8") as f:
        data = json.load(f)
    conn = db()
    added = skipped = 0
    for it in data.get("words", []):
        w = normalize(it.get("word") or "")
        if not w:
            continue
        if find_word(conn, w):
            skipped += 1
            continue
        ts = it.get("created_at") or now_iso()
        cur = conn.execute(
            "INSERT INTO words (word, lemma, phonetic, pos, definition, definition_en,"
            " study_count, encounter_count, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (w, it.get("lemma") or w, it.get("phonetic"), it.get("pos"),
             it.get("definition"), it.get("definition_en"),
             it.get("study_count") or 1, it.get("encounter_count") or 1, ts, ts))
        wid = cur.lastrowid
        rv = it.get("review") or {}
        conn.execute(
            "INSERT OR REPLACE INTO reviews (word_id, status, due_date, interval, ease,"
            " reps, lapses) VALUES (?,?,?,?,?,?,?)",
            (wid, rv.get("status") or "new", rv.get("due_date") or today(),
             rv.get("interval") or 0, rv.get("ease") or 2.5,
             rv.get("reps") or 0, rv.get("lapses") or 0))
        for e in it.get("examples") or []:
            conn.execute("INSERT INTO examples (word_id, en, zh, created_at) VALUES (?,?,?,?)",
                         (wid, e.get("en") or "", e.get("zh") or "", ts))
        for c in it.get("contexts") or []:
            conn.execute(
                "INSERT INTO contexts (word_id, surface, sentence, meaning, source, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (wid, w, c.get("sentence"), c.get("meaning"), c.get("source"), ts))
        added += 1
    conn.commit()
    print(green("导入 %d 个词，跳过已有 %d 个" % (added, skipped)))
    return 0


def cmd_news(args, cfg):
    conn = db()
    if args.fetch:
        print(dim("  抓取中…"))
        n = news_fetch(conn, args.limit, quiet=False)
        print(green("  今天新增 %d 条" % n))
    if args.summarize:
        todo = news_without_summary(conn)
        if not cfg.get("api_key"):
            print(red("  写摘要需要 AI 密钥"))
        else:
            print(dim("  给 %d 条写摘要…" % len(todo)))
            for r in todo:
                ok = news_summarize(conn, r, cfg, quiet=False)
                print("  %s %s" % (green("✓") if ok else red("×"), r["title"][:56]))
    rows = conn.execute(
        "SELECT * FROM news ORDER BY day DESC, published DESC LIMIT ?",
        (args.limit if not args.all else 200,)).fetchall()
    if not rows:
        print("还没有新闻，跑 %s" % bold("v news --fetch"))
        return 0
    cur = None
    for r in rows:
        if r["day"] != cur:
            cur = r["day"]
            print("\n%s" % bold(cur))
        print("  %s %s" % (dim("%-11s" % r["source"]), r["title"]))
    return 0


# ---------------------------------------------------------------- 本地网页

SERVE_HTML = r"""<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Vocab</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Vocab">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="theme-color" content="#f7f6f3" media="(prefers-color-scheme:light)">
<meta name="theme-color" content="#16171a" media="(prefers-color-scheme:dark)">
<style>
:root{
  --bg:#f7f6f3; --card:#fff; --fg:#1a1a1a; --dim:#8a8a86; --line:#e6e4df;
  --accent:#2f6f4f; --warn:#b8862a; --bad:#b04a3f; --shadow:0 1px 3px rgba(0,0,0,.06);
}
@media (prefers-color-scheme:dark){
  :root{ --bg:#16171a; --card:#1e2024; --fg:#e8e6e3; --dim:#8b8d92; --line:#2d3036;
         --accent:#6bbf8e; --warn:#d6a94e; --bad:#e07a6c; --shadow:none; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:16px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif;
  -webkit-font-smoothing:antialiased;padding-bottom:env(safe-area-inset-bottom)}
.wrap{max-width:940px;margin:0 auto;padding:16px}
header{display:flex;align-items:center;gap:8px;margin-bottom:18px;flex-wrap:wrap}
h1{font-size:17px;margin:0;font-weight:600;letter-spacing:.02em}
.tabs{display:flex;gap:4px;margin-left:auto}
.tab{padding:6px 11px;border-radius:8px;border:none;background:transparent;color:var(--dim);
  font-size:14px;cursor:pointer;font-family:inherit;white-space:nowrap}
.tabs{gap:2px}
#cnt{font-weight:600}
#cnt.lit{color:var(--accent)}
.tab.on{background:var(--card);color:var(--fg);box-shadow:var(--shadow)}
.badge{display:inline-block;min-width:17px;padding:0 5px;margin-left:5px;border-radius:9px;
  background:var(--accent);color:var(--card);font-size:11px;line-height:17px;text-align:center;
  font-weight:600}
.card{background:var(--card);border-radius:14px;padding:26px 22px;box-shadow:var(--shadow);
  border:1px solid var(--line)}
.word{font-size:34px;font-weight:600;letter-spacing:-.01em;word-break:break-word}
.phon{color:var(--dim);font-size:14px;margin-top:4px;font-variant:none}
.sent{color:var(--dim);margin-top:16px;font-size:15px;line-height:1.7}
.sent b{color:var(--fg);font-weight:600}
.def{margin-top:18px;padding-top:18px;border-top:1px solid var(--line);font-size:16px}
.mean{color:var(--dim);font-size:14px;margin-top:6px}
.hint{color:var(--dim);font-size:13px;text-align:center;margin-top:18px}
.btns{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:16px}
button.g{padding:14px 8px;border-radius:11px;border:1px solid var(--line);background:var(--card);
  color:var(--fg);font-size:15px;cursor:pointer;font-family:inherit;
  -webkit-tap-highlight-color:transparent;user-select:none;-webkit-user-select:none}
button.g:active{transform:scale(.97)}
button.g small{display:block;color:var(--dim);font-size:11px;margin-top:2px}
button.g1{border-color:var(--bad);color:var(--bad)}
button.g2{border-color:var(--warn);color:var(--warn)}
button.g3{border-color:var(--accent);color:var(--accent)}
button.g1 small,button.g2 small,button.g3 small{color:var(--dim)}
.big{width:100%;padding:16px;border-radius:12px;border:1px solid var(--line);background:var(--card);
  color:var(--fg);font-size:16px;cursor:pointer;font-family:inherit;margin-top:16px;
  user-select:none;-webkit-user-select:none;-webkit-tap-highlight-color:transparent}
.prog{height:3px;background:var(--line);border-radius:2px;overflow:hidden;margin-bottom:16px}
.prog i{display:block;height:100%;background:var(--accent);transition:width .25s}
.row{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px 16px;
  margin-bottom:8px;box-shadow:var(--shadow);position:relative;
  display:flex;gap:22px;align-items:flex-start}
.cl{flex:0 0 33%;min-width:0}
.cr{flex:1;min-width:0;padding-left:22px;border-left:1px solid var(--line);
  align-self:stretch;position:relative}
.row .w{font-size:18px;font-weight:600;display:flex;align-items:baseline;gap:0}
.del{position:absolute;right:-6px;top:-6px;border:none;background:var(--card);
  color:var(--dim);font-size:17px;line-height:1;cursor:pointer;padding:3px 7px;
  font-family:inherit;border-radius:7px;opacity:0;transition:opacity .12s ease;
  -webkit-tap-highlight-color:transparent}
.row:hover .del{opacity:.75}
.del:hover{opacity:1;color:var(--bad)}
.del.arm{opacity:1;color:var(--bad);font-size:12px;font-weight:600;
  border:1px solid var(--bad);padding:3px 8px}
.row.armed .del{opacity:.85}            /* 手机长按后亮出来 */
@media (hover:none){ .del{opacity:0} }  /* 手机上平时完全不显示 */
.row:hover .del{color:var(--dim)}
.del:hover{color:var(--bad)}
.row .d{font-size:14px;color:var(--dim);margin-top:2px}
.row .s{font-size:13px;color:var(--dim);margin-top:8px;line-height:1.6}
.meta{font-size:12px;color:var(--dim);margin-left:8px;font-weight:400}
.act{display:flex;gap:8px;margin-top:12px}
.act button{flex:1;padding:9px;border-radius:9px;border:1px solid var(--line);background:transparent;
  color:var(--fg);font-size:14px;cursor:pointer;font-family:inherit}
.act .yes{border-color:var(--accent);color:var(--accent)}
.act .no{color:var(--dim)}
input[type=text]{width:100%;padding:12px 14px;border-radius:11px;border:1px solid var(--line);
  background:var(--card);color:var(--fg);font-size:15px;font-family:inherit;margin-bottom:8px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:12px 8px;text-align:center}
.stat b{display:block;font-size:20px;font-weight:600}
.stat span{font-size:11px;color:var(--dim)}
.empty{text-align:center;color:var(--dim);padding:60px 20px;font-size:15px}
.count{font-size:12px;color:var(--dim);margin:10px 2px 8px}
#all{position:relative}
#sug{position:absolute;left:0;right:0;z-index:40;background:var(--card);
  border:1px solid var(--line);border-radius:11px;margin-top:-4px;overflow:hidden;
  box-shadow:0 8px 26px rgba(0,0,0,.16);display:none}
#sug.show{display:block}
.sgi{padding:9px 14px;cursor:pointer;display:flex;align-items:baseline;gap:9px;
  border-top:1px solid var(--line);font-size:14px}
.sgi:first-child{border-top:none}
.sgi.on,.sgi:hover{background:var(--bg)}
.sgi b{font-weight:600;flex:none}
.sgi i{font-style:normal;color:var(--dim);font-size:12.5px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.sgs{font-size:10.5px;color:var(--accent);border:1px solid var(--accent);
  border-radius:5px;padding:0 4px;flex:none}
.rt{background:var(--card);border:1px solid var(--line);border-radius:11px;
  margin-bottom:7px;box-shadow:var(--shadow)}   /* 不能 overflow:hidden，会裁掉提示框 */
.rthead{display:flex;align-items:baseline;gap:11px;padding:12px 15px;cursor:pointer;
  user-select:none;-webkit-user-select:none}
.rthead b{font-size:17px;font-weight:600;font-family:var(--mono,inherit)}
.rtm{font-size:13.5px;color:var(--dim)}
.rtn{margin-left:auto;font-size:12px;color:var(--dim);border:1px solid var(--line);
  border-radius:9px;padding:1px 8px}
.rt.open .rtn{border-color:var(--accent);color:var(--accent)}
.rtbody{display:none;padding:0 15px 14px;border-top:1px solid var(--line)}
.rt.open .rtbody{display:block}
.rtlab{font-size:11px;color:var(--dim);letter-spacing:.08em;text-transform:uppercase;
  margin:11px 0 7px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{font-size:14px;padding:4px 10px;border-radius:8px;border:1px solid var(--line);
  position:relative;cursor:default}
.chip.tip::after{top:auto;bottom:100%;margin:0 0 6px;left:50%;transform:translateX(-50%)}
.chip.mine{border-color:var(--accent);color:var(--accent)}
.chip.add{color:var(--dim);cursor:pointer;border-style:dashed}
.chip.add:hover{color:var(--fg);border-color:var(--dim)}
.pkgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}
@media (max-width:880px){ .pkgrid{grid-template-columns:repeat(2,1fr)} }
@media (max-width:560px){ .pkgrid{grid-template-columns:1fr} }
.pk{background:var(--card);border:1px solid var(--line);border-radius:11px;
  padding:11px 13px;cursor:pointer;box-shadow:var(--shadow);position:relative;
  user-select:none;-webkit-user-select:none;-webkit-tap-highlight-color:transparent;
  touch-action:manipulation}
.pk:hover{border-color:var(--dim)}
.pk.sel{border-color:var(--accent);opacity:.35}
.pk.gone{opacity:.25}
.pkx{position:absolute;right:6px;top:5px;border:none;background:transparent;
  color:var(--line);font-size:17px;line-height:1;cursor:pointer;padding:3px 7px;
  border-radius:6px;font-family:inherit;opacity:0;transition:opacity .12s;
  -webkit-tap-highlight-color:transparent}
.pk:hover .pkx{opacity:1}
.pkx:hover{color:var(--bad);background:var(--bg)}
.pk.zhon{border-color:var(--dim)}
.pk.zhon .pke{color:var(--fg)}
@media (hover:none){ .pkx{opacity:.5} }
@keyframes pknew{0%{background:color-mix(in srgb,var(--accent) 16%,var(--card))}
                 100%{background:var(--card)}}
.pk.pknew{animation:pknew .7s ease-out}
.pkw{font-size:16px;font-weight:600;display:flex;align-items:baseline;gap:7px}
.pke{font-size:12.5px;color:var(--dim);margin-top:3px;line-height:1.45;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
  height:calc(2 * 1.45 * 12.5px)}
.pager{display:flex;gap:5px;justify-content:center;margin:18px 0 4px;flex-wrap:wrap}
.pg{min-width:32px;padding:6px 9px;border-radius:8px;border:1px solid var(--line);
  background:var(--card);color:var(--fg);font-size:13px;cursor:pointer;font-family:inherit}
.pg.now{border-color:var(--accent);color:var(--accent);font-weight:600}
.pg.off{opacity:.3;cursor:default}
.ex{margin:0}
.cr{min-height:calc(3 * 1.55 * 14px + 10px)}
.exi{margin-bottom:5px}
.exi:last-child{margin-bottom:0}
.exi{position:relative}
.exi .en{font-size:14px;line-height:1.55;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
u.kw{text-decoration:underline;text-decoration-color:var(--accent);
  text-decoration-thickness:1.5px;text-underline-offset:3px}
.exi .zh{font-size:13px;color:var(--dim);margin-top:1px}
.row .w.tip::after,.card .word.tip::after{font-weight:400}
.den{font-size:13.5px;color:var(--dim);margin-top:4px;line-height:1.5;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;
  height:calc(3 * 1.5 * 13.5px)}          /* 固定三行，卡片高度才稳定 */
.wait{font-size:13px;color:var(--dim);line-height:1.55}
.wait::before{content:"◌ "}
.wt{cursor:pointer}
.lk{color:var(--accent);cursor:pointer;text-decoration:underline;text-underline-offset:2px}
.st{margin-left:auto;padding:2px 2px;font-size:14px;letter-spacing:1px;color:var(--dim);opacity:.6;
  cursor:pointer;font-weight:400;line-height:1;white-space:nowrap;
  -webkit-tap-highlight-color:transparent;user-select:none;-webkit-user-select:none}
.st.on{color:var(--warn);opacity:1}
.st:hover{color:var(--warn);opacity:1}
#toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(8px);
  background:var(--fg);color:var(--bg);padding:8px 15px;border-radius:20px;font-size:13px;
  opacity:0;pointer-events:none;transition:opacity .15s,transform .15s;z-index:99;
  box-shadow:0 6px 20px rgba(0,0,0,.25)}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.card .den{font-size:15px;margin-top:14px;color:var(--fg);opacity:.85}
.ph{font-size:12px;color:var(--dim);margin-left:8px;font-weight:400}
.tip{position:relative;cursor:help}
.tip::after{content:attr(data-zh);position:absolute;left:0;top:100%;margin-top:5px;
  background:var(--fg);color:var(--bg);padding:7px 11px;border-radius:9px;
  font-size:13px;line-height:1.55;white-space:normal;width:max-content;
  max-width:min(360px,78vw);display:none;
  z-index:20;pointer-events:none;box-shadow:0 6px 20px rgba(0,0,0,.22)}
.tip:hover::after,.tip.on::after{display:block}
.pk.zhon.tip:hover::after{display:none}
.zh{display:none}
body.show-cn .zh{display:block}
body.show-cn .tip::after{display:none}
body.show-cn .tip{cursor:auto}
input[type=password]{width:100%;padding:12px 14px;border-radius:11px;border:1px solid var(--line);
  background:var(--bg);color:var(--fg);font-size:15px;font-family:inherit;margin-top:10px}
.tab[data-t="set"]{font-size:16px;padding:6px 10px}
.hidden{display:none}
#wtip{position:fixed;z-index:60;max-width:min(330px,80vw);padding:8px 12px;border-radius:9px;
  background:var(--fg);color:var(--bg);font-size:13.5px;line-height:1.55;
  box-shadow:0 6px 22px rgba(0,0,0,.28);pointer-events:none;display:none}
#wtip.show{display:block}
#wtip b{font-weight:600}
#wtip i{font-style:normal;opacity:.65;font-size:12px}
body.wordtip .tip:hover::after{display:none}
.nday{font-size:12px;color:var(--dim);margin:16px 2px 8px;letter-spacing:.04em}
.nitem{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:12px 15px;margin-bottom:7px;box-shadow:var(--shadow)}
.nhead{display:flex;gap:11px;align-items:baseline;cursor:pointer}
.nbody{cursor:auto}
.nsrc{flex:none;font-size:11px;color:var(--dim);width:74px;letter-spacing:.03em}
.ntitle{font-size:15px;line-height:1.5}
.nbody{display:none;margin-top:11px;padding-top:11px;border-top:1px solid var(--line)}
.nitem.open .nbody{display:block}
.nbody p{margin:0 0 12px;font-size:15px;line-height:1.75;max-width:70ch}
.npub{font-size:12px;color:var(--dim);margin-left:12px}
.nbody .nraw{font-size:13.5px;color:var(--dim)}
.ntitle.tip{position:relative}
.ntitle.tip::after{top:auto;bottom:100%;margin:0 0 6px}   /* 浮在标题上方，不压正文 */
@keyframes flash{ 0%{background:var(--accent)} 100%{background:var(--card)} }
.row.flash{animation:flash 1.1s ease-out}
@media (max-width:700px){
  .row{display:block}
  .den{height:auto;-webkit-line-clamp:4}
  .cr{min-height:0}
  .cr{padding-left:0;border-left:none;margin-top:10px;padding-top:10px;
    border-top:1px solid var(--line)}
  .del{right:-4px;top:4px}
  .exi .en{white-space:normal}
}
</style></head><body>
<div class="wrap">
  <header>
    <h1>Vocab</h1>
    <div class="tabs">
      <button class="tab on" data-t="all">Words</button>
      <button class="tab" data-t="pick">Pick</button>
      <button class="tab" data-t="roots">Roots</button>
      <button class="tab" data-t="news">News</button>
      <button class="tab" data-t="review">Review</button>
      <button class="tab" id="cnt" title="Show / hide Chinese">ZH</button>
      <button class="tab" data-t="set" title="Settings">⚙</button>
    </div>
  </header>
  <div id="all"></div>
  <div id="pick" class="hidden"></div>
  <div id="roots" class="hidden"></div>
  <div id="news" class="hidden"></div>
  <div id="review" class="hidden"></div>
  <div id="set" class="hidden"></div>
</div>
<script>
const K="__TOKEN__";
const $=s=>document.querySelector(s);
const api=async(p,o)=>{
  const u=p+(p.includes("?")?"&":"?")+"k="+encodeURIComponent(K);
  const r=await fetch(u,o||{});
  if(!r.ok) throw new Error(await r.text());
  return r.json();
};
const post=(p,b)=>api(p,{method:"POST",headers:{"Content-Type":"application/json"},
                        body:JSON.stringify(b)});
const esc=s=>(s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

let tab="all", Q=[], qi=0, flipped=false;
const TABS=["all","pick","roots","news","review","set"];

function activate(t){
  if(!TABS.includes(t)) t="all";
  if(window.hideWtip) hideWtip();          // 切页时别把提示框留在屏幕上
  tab=t;
  localStorage.setItem("tab",t);          // 刷新后还停在这一页
  document.querySelectorAll(".tab[data-t]").forEach(
    x=>x.classList.toggle("on", x.dataset.t===t));
  TABS.forEach(x=>$("#"+x).classList.toggle("hidden", x!==t));
  if(t==="all") loadAll("");
  if(t==="pick") loadPick();
  if(t==="roots") loadRoots();
  if(t==="news") loadNews();
  if(t==="review") loadQueue();
  if(t==="set") loadSet();
}
document.querySelectorAll(".tab[data-t]").forEach(
  b=>b.onclick=()=>activate(b.dataset.t));
const refreshBadge=()=>api("/api/stats");

// ---------- 复习：同样的卡片版面，每次进来随机翻出一批老词
async function loadQueue(){
  const el = $("#review");
  const st = await api("/api/stats");
  const data = await api("/api/words?random=1&n=20");
  const rows = data.rows || [];
  if(!rows.length){ el.innerHTML = `<div class="empty">No words yet.</div>`; return; }
  el.innerHTML =
    `<div class="count">${rows.length} random words out of ${data.total}
       <a class="lk" style="margin-left:10px" onclick="loadQueue()">shuffle</a></div>
     ${rows.map(w => rowHTML(w, st)).join("")}`;
}

// ---------- 全部
function pagerHTML(){
  if(totalPages <= 1) return "";
  const q = () => "loadAll($('#q')?$('#q').value.trim():''";
  const b=(p,label,on)=>`<button class="pg${on?" now":""}"${on?"":
    ` onclick="${q()},${p})"`}>${label}</button>`;
  let h=`<div class="pager">`;
  h+= curPage>1 ? b(curPage-1,"\u2039") : `<button class="pg off">\u2039</button>`;
  for(let p=1;p<=totalPages;p++) h+=b(p,p,p===curPage);
  h+= curPage<totalPages ? b(curPage+1,"\u203a") : `<button class="pg off">\u203a</button>`;
  return h+`</div>`;
}

// ---------- 输入联想
let sugItems = [], sugIdx = -1, pendingRisky = "";

function sugEl(){
  let el = $("#sug");
  if(!el){
    el = document.createElement("div"); el.id = "sug";
    $("#q").parentNode.insertBefore(el, $("#q").nextSibling);
  }
  return el;
}
function hideSug(){ const el = $("#sug"); if(el) el.classList.remove("show"); sugIdx = -1; }

async function suggest(q){
  if(!q || q.length < 2) return hideSug();
  const r = await api("/api/suggest?q=" + encodeURIComponent(q));
  sugItems = (r && r.items) || [];
  if(!sugItems.length) return hideSug();
  const el = sugEl();
  el.innerHTML = sugItems.map((it,i)=>
    `<div class="sgi${i===sugIdx?" on":""}" data-i="${i}" onmousedown="pickSug(${i})">
       <b>${esc(it.word)}</b>${it.saved?`<span class="sgs">saved</span>`:""}
       <i>${esc(it.zh)}</i></div>`).join("");
  el.classList.add("show");
}
function moveSug(d){
  if(!sugItems.length) return;
  sugIdx = (sugIdx + d + sugItems.length + 1) % (sugItems.length + 1) - (d<0&&sugIdx<0?0:0);
  if(sugIdx > sugItems.length - 1) sugIdx = -1;
  if(sugIdx < -1) sugIdx = sugItems.length - 1;
  const el = $("#sug");
  if(el) el.querySelectorAll(".sgi").forEach((x,i)=>x.classList.toggle("on", i===sugIdx));
  if(sugIdx >= 0) $("#q").value = sugItems[sugIdx].word;
}
window.pickSug = async i => { $("#q").value = sugItems[i].word; hideSug(); await addFromBox(true); };

async function onBoxKey(e, box){
  if(e.key === "ArrowDown"){ e.preventDefault(); return moveSug(1); }
  if(e.key === "ArrowUp"){ e.preventDefault(); return moveSug(-1); }
  if(e.key === "Escape"){ return hideSug(); }
  if(e.key !== "Enter") { pendingRisky = ""; return; }
  e.preventDefault();
  hideSug();
  await addFromBox(false);
}

async function addFromBox(fromPick){
  const box = $("#q");
  const v = box.value.trim().toLowerCase();
  if(!v) return;
  // 打错拦截：词典里查不到就先不收，除非再按一次回车
  if(!fromPick && v !== pendingRisky){
    const d = await api("/api/define?w=" + encodeURIComponent(v));
    if(!d || !d.found){
      pendingRisky = v;
      const s = await api("/api/suggest?q=" + encodeURIComponent(v.slice(0,3)));
      const near = (s.items||[]).slice(0,3).map(x=>x.word).join("、");
      toast(`"${v}" not in the dictionary${near?` · did you mean ${near}?`:""} · Enter again to add anyway`);
      return;
    }
  }
  pendingRisky = "";
  box.value = ""; box.placeholder = "adding…"; box.disabled = true;
  const r = await post("/api/add", {word: v});
  box.disabled = false; box.placeholder = "Type a word — Enter to add"; box.focus();
  if(r) toast(r.merged ? `${r.word} · already saved · ${r.count}×` : `${r.word} · saved`);
      loadKnown();
  await loadAll(""); flashRow(r && r.id);
}

function flashRow(id){
  if(!id) return;
  const el=document.querySelector(`.row[data-id="${id}"]`);
  if(!el) return;
  el.classList.remove("flash"); void el.offsetWidth;    // 重置动画
  el.classList.add("flash");
}

function rowHTML(w, st){
  return `<div class="row" data-id="${w.id}">
    <div class="cl">
      <div class="w${w.definition?" tip":""}"${w.definition?` data-zh="${esc(w.definition)}"`:""}><span class="wt">${esc(w.word)}</span>${w.phonetic?`<span class="ph">${esc(w.phonetic)}</span>`:""}${w.encounter_count>1?`<span class="meta">×${w.encounter_count}</span>`:""}${starHTML(w.id,w.study_count)}</div>
      ${w.definition_en?`<div class="den">${esc(w.definition_en)}</div>`:""}
      ${w.definition?`<div class="d zh">${esc(w.definition)}</div>`:""}
    </div>
    <div class="cr">
      ${exHTML(w.examples)}
      ${(!w.examples||!w.examples.length)?(st.has_key
          ? `<div class="wait">writing examples\u2026</div>`
          : (w.sentence?`<div class="s">${esc(w.sentence)}</div>`:"")):""}
      <button class="del" title="Remove — and stop capturing this word" onclick="forget(${w.id},event)">×</button>
    </div>
  </div>`;
}

let _seq=0, curPage=1, totalPages=1;
async function loadAll(q, page){
  const my=++_seq;                 // 打字快时多个请求并发，只认最后发出的那次
  const el=$("#all");
  const st=await refreshBadge();
  if(my!==_seq) return;
  if(!$("#q")){          // 输入框只建一次，否则打字到一半会失去焦点
    el.innerHTML=`
      <input type="text" id="q" placeholder="Type a word — Enter to add"
        autocomplete="off" autocapitalize="off" spellcheck="false">
      <div class="count" id="count"></div>
      <div id="rows"></div>`;
    const box=$("#q");
    box.oninput=()=>{
      clearTimeout(window._t);
      window._t=setTimeout(()=>{ loadAll(box.value.trim()); suggest(box.value.trim()); }, 220);
    };
    box.onkeydown=e=>onBoxKey(e, box);
    box.onblur=()=>setTimeout(hideSug, 180);
  }
  const data=await api("/api/words?q="+encodeURIComponent(q||"")+"&page="+(page||1));
  if(my!==_seq) return;
  const rows=data.rows||[];
  curPage=data.page; totalPages=data.pages;
  $("#count").innerHTML = (data.total ? `${data.total} word${data.total>1?"s":""}` : "")
    + ((!st.has_key && st.pending_examples)
       ? ` · <a class="lk" onclick="goSet()">${st.pending_examples} without examples — add an AI key in ⚙</a>`
       : "");
  let lastDay="";
  $("#rows").innerHTML=rows.length?rows.map(w=>{
    const d=(w.updated_at||"").slice(0,10);
    const head = d && d!==lastDay ? (lastDay=d, `<div class="nday">${esc(d)}</div>`) : "";
    return head + rowHTML(w, st);
  }).join("") + pagerHTML()
    :`<div class="empty">No matches</div>`;
  // 还有词在等例句就自动刷新，不用你手动刷
  clearTimeout(window._poll);
  if(st.has_key && st.pending_examples)
    window._poll=setTimeout(()=>{
      if(tab==="all") loadAll($("#q")?$("#q").value.trim():"");
    }, 8000);
}
window.starLabel=n=>n===0 ? "☆" : (n<=10 ? "★".repeat(n) : `★×${n}`);
window.starTitle=n=>`Studied ${n} time${n===1?"":"s"} — left-click +1, right-click −1`;
window.starHTML=(id,n)=>{
  n = n || 0;
  return `<span class="st${n?" on":""}" title="${starTitle(n)}"
    onclick="star(${id},event,1)" oncontextmenu="star(${id},event,-1)">${starLabel(n)}</span>`;
};
window.star=async(id,ev,delta)=>{
  ev.preventDefault(); ev.stopPropagation();
  const el=ev.currentTarget||ev.target;          // await 之后 currentTarget 会变 null，先抓住
  const r=await post("/api/star",{id,delta});
  if(!r||!r.ok) return;
  el.textContent=starLabel(r.count);
  el.title=starTitle(r.count);
  el.classList.toggle("on", r.count>0);
};

// 例句里出现你已经收过的词，画一条下划线
let knownSet = null;
async function loadKnown(){
  try{ const r = await api("/api/known"); knownSet = new Set(r.words || []); }
  catch(e){ knownSet = new Set(); }
}
function kwStems(w){
  const o = [w];
  const rules = [["ies","y"],["ied","y"],["ing",""],["ed",""],["es",""],["s",""],
                 ["ly",""],["er",""],["est",""]];
  for(const [suf, rp] of rules){
    if(w.endsWith(suf) && w.length - suf.length >= 2){
      const b = w.slice(0, w.length - suf.length) + rp;
      o.push(b, b + "e");
      if(b.length > 2 && b[b.length-1] === b[b.length-2]) o.push(b.slice(0,-1));
    }
  }
  return o;
}
function markKnown(text){
  if(!knownSet || !knownSet.size) return esc(text);
  return esc(text).replace(/[A-Za-z][A-Za-z'-]*/g, m => {
    const low = m.toLowerCase();
    return kwStems(low).some(x => knownSet.has(x)) ? `<u class="kw">${m}</u>` : m;
  });
}

window.exHTML=list=>!list||!list.length?"":`<div class="ex">`+list.map(e=>
  `<div class="exi${e.zh?" tip":""}"${e.zh?` data-zh="${esc(e.zh)}"`:""}><div class="en">${markKnown(e.en)}</div>${e.zh?`<div class="zh">${esc(e.zh)}</div>`:""}</div>`
).join("")+`</div>`;

// ---------- 设置
async function loadSet(){
  const st=await api("/api/settings"); const el=$("#set");
  el.innerHTML=`
    <div class="row">
      <div class="w">Example sentences</div>
      <div class="d">Three short examples per word, generated by AI. ${st.has_key?"":"Add a key below first."}</div>
      ${st.has_key?`<div class="act"><button class="yes" id="gen">${st.missing?`Generate for ${st.missing} word${st.missing>1?"s":""}`:"All words covered"}</button></div>
      <div class="s" id="genlog"></div>`:""}
    </div>
    <div class="row">
      <div class="w">AI key</div>
      <div class="d">${st.has_key?`set — ${esc(st.key_hint)}`:"Not set. Everything works without it, except example sentences."}</div>
      <div class="s">Create one at platform.deepseek.com and paste it below. It is stored only on this Mac.</div>
      <input type="password" id="k" placeholder="sk-..." autocomplete="off">
      <div class="act"><button class="yes" id="save">Save</button>
        ${st.has_key?`<button class="no" id="clr">Remove key</button>`:""}</div>
      <div class="s" id="kmsg"></div>
    </div>
    <div class="row">
      <div class="w">Offline dictionary</div>
      <div class="d">${st.dict_count?`${st.dict_count.toLocaleString()} entries — definitions and phonetics come from here, no network needed`:"Not installed"}</div>
      <div class="s">model ${esc(st.model||"")}</div>
    </div>`;
  const save=$("#save");
  if(save) save.onclick=async()=>{
    const v=$("#k").value.trim();
    if(!v){ $("#kmsg").textContent="Paste a key first"; return; }
    save.textContent="checking…"; save.disabled=true;
    const r=await post("/api/settings",{api_key:v});
    save.disabled=false; save.textContent="Save";
    $("#k").value="";
    $("#kmsg").textContent = r.ok ? "Saved ✓ — you can generate examples now" : ("Failed: " + r.error);
    if(r.ok) loadSet();
  };
  const clr=$("#clr");
  if(clr) clr.onclick=async()=>{ await post("/api/settings",{clear_key:true}); loadSet(); };
  const gen=$("#gen");
  if(gen && st.missing) gen.onclick=async()=>{
    gen.disabled=true;
    for(;;){
      const r=await post("/api/examples",{});
      if(!r.ok){ $("#genlog").textContent="Error: " + r.error; break; }
      if(r.done || r.left===0){ $("#genlog").textContent="All done ✓"; break; }
      $("#genlog").textContent=`${r.word} done — ${r.left} left…`;
      gen.textContent=`${r.left} left`;
    }
    gen.disabled=false; loadSet();
  };
}

// 中文默认藏起来：悬停看一眼，点一下钉住，右上角「中」永久显示
(function(){
  const btn=$("#cnt");
  const set=on=>{
    document.body.classList.toggle("show-cn",on);
    btn.classList.toggle("lit",on);
    btn.textContent="ZH";
    btn.title=on?"Click to hide Chinese":"Click to keep Chinese visible";
  };
  set(localStorage.getItem("showcn")==="1");
  btn.onclick=()=>{
    const on=!document.body.classList.contains("show-cn");
    localStorage.setItem("showcn",on?"1":"0");
    set(on);
  };
  document.addEventListener("click",e=>{
    const t=e.target.closest(".tip");
    if(document.body.classList.contains("show-cn")) return;
    if(t && (t.closest(".nhead") || t.classList.contains("pk"))) return;
    document.querySelectorAll(".tip.on").forEach(x=>{ if(x!==t) x.classList.remove("on"); });
    if(t) t.classList.toggle("on");
  });
})();

// 双击任何单词 = 复制它
function toast(msg){
  let t=$("#toast");
  if(!t){ t=document.createElement("div"); t.id="toast"; document.body.appendChild(t); }
  t.textContent=msg; t.classList.add("show");
  clearTimeout(window._toast);
  window._toast=setTimeout(()=>t.classList.remove("show"),1100);
}
async function copyText(txt,quiet){
  try{
    await navigator.clipboard.writeText(txt);
  }catch(e){
    const ta=document.createElement("textarea");
    ta.value=txt; ta.style.position="fixed"; ta.style.opacity="0";
    document.body.appendChild(ta); ta.select();
    try{ document.execCommand("copy"); }catch(_){}
    ta.remove();
  }
  if(!quiet) toast(txt + " copied");
}
// 双击页面上任何一个英文单词：复制 + 入库
document.addEventListener("dblclick",async e=>{
  if(!e.target.closest("#rows, #review, #all, #news")) return;
  const sel=(window.getSelection().toString()||"").trim();
  const w=/^[A-Za-z][A-Za-z'-]{2,}$/.test(sel)
    ? sel
    : (e.target.closest(".wt") ? e.target.closest(".wt").textContent.trim() : "");
  if(!w) return;
  copyText(w, true);
  toast(w + " …");
  const r=await post("/api/add",{word:w,copied:true});
  toast(!r ? w : (r.merged ? `${r.word} · already saved · ${r.count}×`
                           : `${r.word} · saved`));
  // 不重排列表：你正在读的位置比「新词立刻跳到顶部」重要
  if(tab==="all" && r && r.id) flashRow(r.id);
});

// 手机上没有 hover：长按卡片 0.55 秒才把删除按钮亮出来
(function(){
  let t=null;
  const clear=()=>{ clearTimeout(t); t=null; };
  document.addEventListener("touchstart", e=>{
    const row=e.target.closest(".row");
    document.querySelectorAll(".row.armed").forEach(x=>{ if(x!==row) x.classList.remove("armed"); });
    if(!row || e.target.closest(".del")) return;
    clear();
    t=setTimeout(()=>row.classList.add("armed"), 550);
  }, {passive:true});
  ["touchend","touchmove","touchcancel","scroll"].forEach(
    ev=>document.addEventListener(ev, clear, {passive:true, capture:true}));
})();

window.forget=async(id,ev)=>{
  ev.stopPropagation();
  const b=ev.target;
  if(b.dataset.armed!=="1"){          // 第一下只是「准备删」，2.5 秒内没再点就自己取消
    document.querySelectorAll(".del.arm").forEach(x=>{
      x.dataset.armed="0"; x.classList.remove("arm"); x.textContent="×"; });
    b.dataset.armed="1"; b.classList.add("arm"); b.textContent="delete?";
    clearTimeout(window._arm);
    window._arm=setTimeout(()=>{
      b.dataset.armed="0"; b.classList.remove("arm"); b.textContent="×"; },2500);
    return;
  }
  clearTimeout(window._arm);
  await post("/api/forget",{id});
  const row = document.querySelector(`.row[data-id="${id}"]`);
  if(row) row.remove();                       // 当场消失，两个页面都适用
  if(tab === "all"){                          // 单词页还要更新总数和分页
    loadAll($("#q") ? $("#q").value.trim() : "");
  } else if(tab === "review"){
    const n = document.querySelectorAll("#review .row").length;
    const c = document.querySelector("#review .count");
    if(c) c.firstChild.textContent = ` ${n} random words `;
  }
};

// ---------- 词根：只列词根，点开才展开
const openRoots = new Set();

async function loadRoots(){
  const el = $("#roots");
  const r = await api("/api/roots");
  const gs = r.groups || [];
  if(!gs.length){
    el.innerHTML = `<div class="empty">${r.pending && r.has_key
      ? `Working out the roots of your words…`
      : "No shared roots yet — collect a few more words."}</div>`;
    if(r.pending && r.has_key) setTimeout(()=>{ if(tab==="roots") loadRoots(); }, 12000);
    return;
  }
  el.innerHTML =
    `<div class="count">${gs.length} roots across your words${
      r.pending ? ` · ${r.pending} still filling in` : ""}</div>
     ${gs.map(g => `
       <div class="rt${openRoots.has(g.root) ? " open" : ""}" data-r="${esc(g.root)}">
         <div class="rthead" onclick="toggleRoot(this.parentNode)">
           <b>${esc(g.root)}</b><span class="rtm">${esc(g.meaning)}</span>
           <span class="rtn">${g.mine.length}</span>
         </div>
         <div class="rtbody">
           <div class="rtlab">In your list</div>
           <div class="chips">${g.mine.map(m =>
             `<span class="chip mine tip" data-zh="${esc(m.breakdown)}">${esc(m.word)}</span>`
           ).join("")}</div>
           ${g.related.length ? `<div class="rtlab">Same root — double-click to add</div>
           <div class="chips">${g.related.map(x =>
             `<span class="chip add tip" data-zh="${esc(x.zh)}" data-w="${esc(x.word)}"
                ondblclick="addChip(this)">${esc(x.word)}</span>`).join("")}</div>` : ""}
         </div>
       </div>`).join("")}`;
  if(r.pending && r.has_key) setTimeout(()=>{ if(tab==="roots") loadRoots(); }, 20000);
}

window.toggleRoot = el => {
  const on = el.classList.toggle("open");
  if(on) openRoots.add(el.dataset.r); else openRoots.delete(el.dataset.r);
};

window.addChip = async el => {
  if(el.dataset.busy) return;
  el.dataset.busy = "1";
  getSelection().removeAllRanges();
  const w = el.dataset.w;
  const r = await post("/api/add", {word: w});
  toast(r && r.merged ? `${w} · already saved` : `${w} · added`);
  loadKnown();
  el.classList.remove("add"); el.classList.add("mine");
};

// ---------- 推荐词：点一个就收进词库，原位补一个新的
function pkCardHTML(it){
  return `<div class="pk${it.zh ? " tip" : ""}"${it.zh ? ` data-zh="${esc(it.zh)}"` : ""}
       data-w="${esc(it.word)}" data-en="${esc(it.en)}"
       onclick="pkTap(this)" ondblclick="pkTake(this)">
    <div class="pkw"><span class="wt">${esc(it.word)}</span>${
      it.phonetic ? `<span class="ph">${esc(it.phonetic)}</span>` : ""}</div>
    <div class="pke">${esc(it.en)}</div>
    <button class="pkx" title="Not interested — show another"
      onclick="event.stopPropagation();pickAct(this.parentNode,'skip')">×</button>
  </div>`;
}

// 单击看中文，双击才收进词库
let pkTimer = null;
window.pkTap = card => {
  if(pkTimer){ clearTimeout(pkTimer); pkTimer = null; return; }  // 第二下交给 dblclick
  pkTimer = setTimeout(() => {
    pkTimer = null;
    if(!card.dataset.zh) return;
    // 同时只留一张显示中文：点下一个，上一个自动变回英文
    document.querySelectorAll("#pick .pk.zhon").forEach(x => {
      if(x === card) return;
      x.classList.remove("zhon");
      x.querySelector(".pke").textContent = x.dataset.en;
    });
    const body = card.querySelector(".pke");
    const on = card.classList.toggle("zhon");
    body.textContent = on ? card.dataset.zh : card.dataset.en;
  }, 260);
};
window.pkTake = card => {
  clearTimeout(pkTimer); pkTimer = null;
  getSelection().removeAllRanges();          // 双击会选中文字，清掉
  pickAct(card, "add");
};

async function loadPick(){
  const el = $("#pick");
  const r = await api("/api/pick");
  const items = r.items || [];
  if(!items.length){
    el.innerHTML = `<div class="empty">Nothing left to recommend.</div>`;
    return;
  }
  el.innerHTML =
    `<div class="count">Tap for the meaning · double-tap to add it · tap × to skip</div>
     <div class="pkgrid">${items.map(pkCardHTML).join("")}</div>`;
}

window.pickAct = async (el, action) => {
  if(el.dataset.busy) return;
  el.dataset.busy = "1";
  const w = el.dataset.w;
  el.classList.add(action === "add" ? "sel" : "gone");
  const shown = [...document.querySelectorAll("#pick .pk")].map(x => x.dataset.w);
  const r = await post("/api/pick/take", {word: w, action, shown});
  toast(action === "add" ? `${w} · added` : `${w} · skipped`);
  if(r && r.next){                       // 原位换成新词，网格不跳动
    el.outerHTML = pkCardHTML(r.next);
    const fresh = document.querySelector(`#pick .pk[data-w="${r.next.word}"]`);
    if(fresh){ fresh.classList.add("pknew"); setTimeout(()=>fresh.classList.remove("pknew"), 700); }
  } else {
    el.remove();
  }
};

// ---------- 新闻
const openNews = new Set();          // 展开的是哪几条，重绘后要还原
window.toggleNews=(el,id)=>{
  const on = el.classList.toggle("open");
  if(on) openNews.add(id); else openNews.delete(id);
};
async function loadNews(){
  const el=$("#news");
  const rows=await api("/api/news");
  if(!rows.length){
    el.innerHTML=`<div class="empty">No stories yet.<br><br>
      <button class="big" style="max-width:220px;margin:0 auto" onclick="fetchNews(this)">Fetch today\u2019s news</button></div>`;
    return;
  }
  const st=await api("/api/stats");
  const pending=rows.filter(r=>!r.ai_en).length;
  const todayStr=new Date().toISOString().slice(0,10);
  const todayN=rows.filter(r=>r.day===todayStr).length;
  let h=`<div class="count">${todayN} today · ${rows.length} stories · double-click any word to save it
    <a class="lk" style="margin-left:10px" onclick="fetchNews(this)">refresh</a>${
    pending&&st.has_key?` · <span class="wait" style="display:inline">${pending} summaries being written</span>`:""}</div>`;
  let day="";
  rows.forEach(r=>{
    if(r.day!==day){ day=r.day; h+=`<div class="nday">${esc(day)}</div>`; }
    const full = r.ai_en && r.ai_en!=="-";
    const body = full ? r.ai_en
               : (st.has_key ? "" : (r.summary||"(no summary)"));
    h+=`<div class="nitem${openNews.has(r.id)?" open":""}" data-nid="${r.id}">
      <div class="nhead" onclick="toggleNews(this.parentNode,${r.id})"><span class="nsrc">${esc(r.source)}</span>
        <span class="ntitle${r.ai_zh?" tip":""}"${r.ai_zh?` data-zh="${esc(r.ai_zh)}"`:""}>${esc(r.title)}</span></div>
      <div class="nbody">
        ${body?`<p>${esc(body)}</p>`:`<div class="wait">writing summary\u2026</div>`}
        ${!full&&r.summary?`<p class="nraw">${esc(r.summary)}</p>`:""}
        <a class="lk" href="${esc(r.link)}" target="_blank" rel="noopener"
           onclick="event.stopPropagation()">Original \u2197</a>
        <span class="npub">${esc(r.published||"")}</span>
      </div></div>`;
  });
  el.innerHTML=h;
  clearTimeout(window._npoll);
  if(pending&&st.has_key)
    window._npoll=setTimeout(()=>{ if(tab==="news") loadNews(); }, 10000);
}
window.fetchNews=async(btn)=>{
  const old=btn.textContent; btn.textContent="fetching…";
  const r=await post("/api/news/fetch",{});
  btn.textContent=old;
  toast(!r ? "failed"
        : r.added ? `${r.added} new`
        : `already have today\u2019s ${r.quota}`);
  loadNews();
};

// ---------- 鼠标停在任意英文单词上 -> 就地显示中文
const READ_AREAS = "#news .nbody, .den, .card .sent, .card .def";
const defCache = new Map();
let hoverWord = "", hoverTimer = null;

function wordAtPoint(x, y){
  let node, off;
  if(document.caretRangeFromPoint){
    const r = document.caretRangeFromPoint(x, y);
    if(!r) return null;
    node = r.startContainer; off = r.startOffset;
  }else if(document.caretPositionFromPoint){
    const p = document.caretPositionFromPoint(x, y);
    if(!p) return null;
    node = p.offsetNode; off = p.offset;
  }else return null;
  if(!node || node.nodeType !== 3) return null;
  if(!node.parentElement || !node.parentElement.closest(READ_AREAS)) return null;
  const t = node.textContent, isw = c => c && /[A-Za-z'\-]/.test(c);
  if(!isw(t[off]) && !isw(t[off-1])) return null;
  let a = off, b = off;
  while(a > 0 && isw(t[a-1])) a--;
  while(b < t.length && isw(t[b])) b++;
  const w = t.slice(a, b).replace(/^[''\-]+|[''\-]+$/g, "");
  if(w.length < 2) return null;
  // caretRangeFromPoint 会把光标吸附到最近的文字，
  // 所以必须确认鼠标真的落在这个词的字形上，否则停在空白处也会弹
  const rr = document.createRange();
  rr.setStart(node, a); rr.setEnd(node, b);
  const rects = rr.getClientRects();
  for(let i = 0; i < rects.length; i++){
    const c = rects[i];
    if(x >= c.left - 1 && x <= c.right + 1 && y >= c.top - 1 && y <= c.bottom + 1)
      return w;
  }
  return null;
}

function wtipEl(){
  let el = $("#wtip");
  if(!el){ el = document.createElement("div"); el.id = "wtip"; document.body.appendChild(el); }
  return el;
}
window.hideWtip=function hideWtip(){
  const el = $("#wtip"); if(el) el.classList.remove("show");
  document.body.classList.remove("wordtip");
  hoverWord = "";
}
async function showWtip(w, x, y){
  let d = defCache.get(w);
  if(d === undefined){
    d = await api("/api/define?w=" + encodeURIComponent(w));
    defCache.set(w, d);
  }
  if(hoverWord !== w) return;                 // 鼠标已经移开了
  if(!d || !d.found || !d.zh) return hideWtip();
  const el = wtipEl();
  el.innerHTML = `<b>${esc(d.word)}</b>${d.phonetic?` <i>${esc(d.phonetic)}</i>`:""}<br>${esc(d.zh)}`;
  el.classList.add("show");
  document.body.classList.add("wordtip");
  const r = el.getBoundingClientRect();
  el.style.left = Math.max(8, Math.min(x - r.width/2, innerWidth - r.width - 8)) + "px";
  el.style.top  = (y - r.height - 14 < 8 ? y + 20 : y - r.height - 14) + "px";
}
document.addEventListener("mousemove", e => {
  clearTimeout(hoverTimer);
  hoverTimer = setTimeout(() => {
    const w = wordAtPoint(e.clientX, e.clientY);
    if(!w) return hideWtip();
    if(w.toLowerCase() === hoverWord) return;
    hoverWord = w.toLowerCase();
    showWtip(hoverWord, e.clientX, e.clientY);
  }, 90);
});
document.addEventListener("scroll", hideWtip, true);
document.addEventListener("mouseleave", hideWtip);
window.addEventListener("blur", hideWtip);

window.goReview=()=>document.querySelector('[data-t="review"]').click();
window.goSet=()=>document.querySelector('[data-t="set"]').click();
loadKnown().then(() => activate(localStorage.getItem("tab") || "all"));
</script></body></html>"""


def _serve_row(conn, w):
    ctxs = conn.execute(
        "SELECT sentence, meaning FROM contexts WHERE word_id = ? ORDER BY id DESC LIMIT 3",
        (w["id"],)).fetchall()
    return {
        "id": w["id"], "word": w["word"], "phonetic": w["phonetic"], "pos": w["pos"],
        "definition": w["definition"], "encounter_count": w["encounter_count"],
        "examples": examples_of(conn, w["id"]),
        "study_count": w["study_count"],
        "contexts": [{"sentence": c["sentence"] or "",
                      "meaning": c["meaning"] or "",
                      "masked": _mask_html(c["sentence"] or "", w["word"])} for c in ctxs],
    }


def _mask_html(sentence, word):
    import html as _html
    if not sentence:
        return ""
    stem = word[:4] if len(word) > 4 else word
    parts, last = [], 0
    for m in re.finditer(r"\b%s\w*\b" % re.escape(stem), sentence, re.I):
        parts.append(_html.escape(sentence[last:m.start()]))
        parts.append("<b>____</b>")
        last = m.end()
    parts.append(_html.escape(sentence[last:]))
    return "".join(parts)


def make_handler(cfg, token):
    from http.server import BaseHTTPRequestHandler
    import html as _html

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authed(self, qs):
            if not token:
                return True
            # 本机访问不用口令：能坐在这台电脑前的人，本来就能直接打开数据库文件
            if self.client_address and self.client_address[0] in ("127.0.0.1", "::1"):
                return True
            return qs.get("k", [""])[0] == token

        def _guard(self, fn):
            try:
                return fn()
            except Exception as e:
                try:
                    self._send(500, json.dumps({"error": "%s: %s" % (
                        type(e).__name__, e)}, ensure_ascii=False))
                except Exception:
                    pass

        def do_GET(self):
            return self._guard(self._get)

        def do_POST(self):
            return self._guard(self._post)

        def _get(self):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if u.path == "/":
                return self._send(200, SERVE_HTML.replace("__TOKEN__", token or ""),
                                  "text/html; charset=utf-8")
            if u.path == "/favicon.ico":
                return self._send(204, b"")
            if not self._authed(qs):
                return self._send(403, json.dumps({"error": "bad token"}))
            conn = db()
            try:
                if u.path == "/api/queue":
                    return self._send(200, json.dumps(
                        [_serve_row(conn, w) for w in _queue(conn, cfg)], ensure_ascii=False))
                if u.path == "/api/stats":
                    return self._send(200, json.dumps(self._stats(conn), ensure_ascii=False))
                if u.path == "/api/inbox":
                    rows = conn.execute(
                        "SELECT * FROM inbox WHERE status='pending' ORDER BY score DESC, id"
                    ).fetchall()
                    return self._send(200, json.dumps([dict(r) for r in rows], ensure_ascii=False))
                if u.path == "/api/words":
                    q = (qs.get("q", [""])[0] or "").strip()
                    size = 100
                    try:
                        page = max(1, int(qs.get("page", ["1"])[0]))
                    except ValueError:
                        page = 1
                    where, args_ = "", []
                    if q:
                        where = " WHERE w.word LIKE ? OR w.definition LIKE ?"
                        args_ = ["%" + q + "%", "%" + q + "%"]
                    if qs.get("random", [""])[0] == "1":
                        try:
                            n_ = min(60, max(1, int(qs.get("n", ["20"])[0])))
                        except ValueError:
                            n_ = 20
                        out = []
                        for r in conn.execute(
                                "SELECT w.*, r.status, (SELECT sentence FROM contexts c"
                                " WHERE c.word_id=w.id ORDER BY c.id DESC LIMIT 1) AS sentence"
                                " FROM words w JOIN reviews r ON r.word_id=w.id"
                                " ORDER BY RANDOM() LIMIT ?", (n_,)):
                            d = dict(r)
                            d["examples"] = examples_of(conn, r["id"])
                            out.append(d)
                        return self._send(200, json.dumps(
                            {"rows": out, "page": 1, "pages": 1,
                             "total": conn.execute(
                                 "SELECT COUNT(*) FROM words").fetchone()[0]},
                            ensure_ascii=False))
                    total = conn.execute(
                        "SELECT COUNT(*) FROM words w" + where, args_).fetchone()[0]
                    pages = max(1, (total + size - 1) // size)
                    page = min(page, pages)
                    sql = ("SELECT w.*, r.status, (SELECT sentence FROM contexts c"
                           " WHERE c.word_id=w.id ORDER BY c.id DESC LIMIT 1) AS sentence"
                           " FROM words w JOIN reviews r ON r.word_id=w.id" + where +
                           " ORDER BY w.updated_at DESC LIMIT ? OFFSET ?")
                    out = []
                    for r in conn.execute(sql, args_ + [size, (page - 1) * size]):
                        d = dict(r)
                        d["examples"] = examples_of(conn, r["id"])
                        out.append(d)
                    return self._send(200, json.dumps(
                        {"rows": out, "total": total, "page": page, "pages": pages},
                        ensure_ascii=False))
                if u.path == "/api/suggest":
                    q = normalize(qs.get("q", [""])[0])
                    if not q or len(q) < 1:
                        return self._send(200, json.dumps({"items": []}))
                    d = dict_db()
                    items = []
                    if d:
                        try:
                            hi = q[:-1] + chr(ord(q[-1]) + 1)
                            rows = d.execute(
                                "SELECT word, translation, frq, bnc FROM dict"
                                " WHERE word >= ? AND word < ?"
                                " ORDER BY (word = ?) DESC,"
                                "          (CASE WHEN frq > 0 THEN frq ELSE 999999 END) ASC"
                                " LIMIT 8", (q, hi, q)).fetchall()
                            for r in rows:
                                items.append({"word": r["word"],
                                              "zh": (r["translation"] or "")[:60]})
                        finally:
                            d.close()
                    have = {r[0] for r in conn.execute(
                        "SELECT word FROM words WHERE word >= ? AND word < ?",
                        (q, q + "\uffff"))}
                    for it in items:
                        it["saved"] = it["word"] in have
                    return self._send(200, json.dumps(
                        {"items": items, "exact": bool(items and items[0]["word"] == q)},
                        ensure_ascii=False))
                if u.path == "/api/define":
                    w = normalize(qs.get("w", [""])[0])
                    d = dict_lookup(w) if w else None
                    if not d:
                        return self._send(200, json.dumps({"found": False}))
                    return self._send(200, json.dumps({
                        "found": True, "word": d.get("lemma") or w,
                        "phonetic": d.get("phonetic") or "",
                        "zh": d.get("definition") or "",
                        "en": d.get("definition_en") or "",
                    }, ensure_ascii=False))
                if u.path == "/api/known":
                    ws = [r[0] for r in conn.execute("SELECT word FROM words")]
                    ws += [r[0] for r in conn.execute(
                        "SELECT DISTINCT lemma FROM words WHERE lemma != ''")]
                    return self._send(200, json.dumps({"words": sorted(set(ws))}))
                if u.path == "/api/roots":
                    gs = roots_view(conn, cfg)
                    pend = conn.execute(
                        "SELECT COUNT(*) FROM words w LEFT JOIN word_root r"
                        " ON r.word = w.word WHERE r.word IS NULL").fetchone()[0]
                    pend += sum(1 for g in gs if not g["related"])
                    return self._send(200, json.dumps(
                        {"groups": gs, "pending": pend,
                         "has_key": bool(cfg.get("api_key"))}, ensure_ascii=False))
                if u.path == "/api/pick":
                    return self._send(200, json.dumps(
                        {"items": pick_batch(conn, cfg)}, ensure_ascii=False))
                if u.path == "/api/news":
                    rows = conn.execute(
                        "SELECT * FROM news ORDER BY day DESC, published DESC LIMIT 60"
                    ).fetchall()
                    return self._send(200, json.dumps(
                        [dict(r) for r in rows], ensure_ascii=False))
                if u.path == "/api/settings":
                    k = cfg.get("api_key") or ""
                    d = dict_db()
                    dn = d.execute("SELECT COUNT(*) FROM dict").fetchone()[0] if d else 0
                    if d:
                        d.close()
                    return self._send(200, json.dumps({
                        "has_key": bool(k),
                        "key_hint": (k[:6] + "…" + k[-4:]) if len(k) > 12 else "",
                        "model": cfg.get("model"), "level": cfg.get("level"),
                        "dict_count": dn,
                        "missing": len(words_without_examples(conn)),
                    }, ensure_ascii=False))
                return self._send(404, json.dumps({"error": "not found"}))
            finally:
                conn.close()

        def _post(self):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            if not self._authed(parse_qs(u.query)):
                return self._send(403, json.dumps({"error": "bad token"}))
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except Exception:
                return self._send(400, json.dumps({"error": "bad json"}))
            conn = db()
            try:
                if u.path == "/api/grade":
                    r = conn.execute("SELECT * FROM reviews WHERE word_id=?",
                                     (body.get("id"),)).fetchone()
                    if not r:
                        return self._send(404, json.dumps({"error": "no such word"}))
                    g = int(body.get("grade") or 2)
                    status, interval, ease, reps, lapses = schedule(r, g, cfg)
                    conn.execute(
                        "UPDATE reviews SET status=?,due_date=?,interval=?,ease=?,reps=?,"
                        "lapses=?,last_review_at=? WHERE word_id=?",
                        (status, day_plus(interval), interval, ease, reps, lapses,
                         now_iso(), r["word_id"]))
                    conn.execute(
                        "INSERT INTO review_log (word_id,grade,interval_from,interval_to,"
                        "ease_to,reviewed_at) VALUES (?,?,?,?,?,?)",
                        (r["word_id"], g, r["interval"], interval, ease, now_iso()))
                    conn.execute(
                        "UPDATE words SET study_count = study_count + 1 WHERE id = ?",
                        (r["word_id"],))
                    conn.commit()
                    return self._send(200, json.dumps({"ok": True, "interval": interval,
                                                       "status": status}))
                if u.path == "/api/inbox":
                    row = conn.execute("SELECT * FROM inbox WHERE id=?",
                                       (body.get("id"),)).fetchone()
                    if not row:
                        return self._send(404, json.dumps({"error": "no such item"}))
                    if body.get("action") == "accept":
                        _accept_inbox(conn, row)
                    else:
                        conn.execute("UPDATE inbox SET status='rejected' WHERE id=?", (row["id"],))
                    conn.commit()
                    return self._send(200, json.dumps({"ok": True}))
                if u.path == "/api/add":
                    w = normalize(str(body.get("word") or ""))
                    if not w:
                        return self._send(400, json.dumps({"error": "empty"}))
                    sent = (body.get("context") or "").strip()
                    if body.get("copied"):
                        mark_selfcopy(w)
                    row = find_word(conn, w)
                    if row:
                        _bump(conn, row, w, sent, "网页", None)
                        conn.commit()
                        return self._send(200, json.dumps(
                            {"ok": True, "merged": True, "word": row["word"],
                             "id": row["id"], "count": row["encounter_count"] + 1},
                            ensure_ascii=False))
                    # 已有词绝不走 AI：先用本地词典还原原形，能并就并
                    ai = lookup(w, sent, cfg, quiet=True) or {}
                    lemma = normalize(ai.get("lemma") or "") or w
                    row = find_word(conn, lemma)
                    if row:
                        _bump(conn, row, w, sent, "网页", ai.get("meaning_in_context"))
                        conn.commit()
                        return self._send(200, json.dumps(
                            {"ok": True, "merged": True, "word": row["word"],
                             "id": row["id"], "count": row["encounter_count"] + 1},
                            ensure_ascii=False))
                    wid = insert_word(conn, lemma, ai, w, sent, "网页")
                    conn.commit()
                    return self._send(200, json.dumps(
                        {"ok": True, "merged": False, "word": lemma, "id": wid},
                        ensure_ascii=False))
                if u.path == "/api/capture":
                    text = str(body.get("text") or "")
                    # 网页双击会把词复制到剪贴板，Mac 守护进程随后又传上来，
                    # 不拦住就会一个词记两次
                    if is_selfcopy(text):
                        return self._send(200, json.dumps(
                            {"ok": True, "added": 0, "bumped": [],
                             "skipped": "selfcopy"}, ensure_ascii=False))
                    added, pending, bumped, reason = process_text(
                        conn, text, cfg, source=str(body.get("source") or "剪贴板"))
                    return self._send(200, json.dumps(
                        {"ok": True, "added": added, "bumped": sorted(set(bumped)),
                         "skipped": reason}, ensure_ascii=False))
                if u.path == "/api/pick/take":
                    w = normalize(str(body.get("word") or ""))
                    if not w:
                        return self._send(400, json.dumps({"error": "empty"}))
                    take = str(body.get("action") or "add") == "add"
                    conn.execute("UPDATE pick SET status=? WHERE word=?",
                                 ("added" if take else "skipped", w))
                    if take and not find_word(conn, w):
                        e = dict_lookup(w) or {}
                        insert_word(conn, e.get("lemma") or w, e, w, "", "推荐")
                    conn.commit()
                    # 补位：多取一个，第一个没显示在屏幕上的就是它
                    shown = {normalize(str(x)) for x in (body.get("shown") or [])}
                    shown.add(w)
                    size = int(cfg.get("pick_size", 21))
                    items = pick_batch(conn, cfg, size + len(shown))
                    fresh = next((i for i in items if i["word"] not in shown), None)
                    return self._send(200, json.dumps(
                        {"ok": True, "word": w, "added": take, "next": fresh},
                        ensure_ascii=False))
                if u.path == "/api/pick":
                    add = [normalize(str(x)) for x in (body.get("add") or [])]
                    skip = [normalize(str(x)) for x in (body.get("skip") or [])]
                    if body.get("use_saved"):
                        add = [r[0] for r in conn.execute(
                            "SELECT word FROM pick WHERE status='want'")]
                    added = 0
                    for w in add:
                        if not w:
                            continue
                        conn.execute(
                            "UPDATE pick SET status='added' WHERE word=?", (w,))
                        if find_word(conn, w):
                            continue
                        e = dict_lookup(w) or {}
                        insert_word(conn, e.get("lemma") or w, e, w, "", "推荐")
                        added += 1
                    for w in skip:
                        if w:
                            conn.execute(
                                "UPDATE pick SET status='skipped' WHERE word=?", (w,))
                    conn.commit()
                    return self._send(200, json.dumps(
                        {"ok": True, "added": added,
                         "items": pick_batch(conn, cfg)}, ensure_ascii=False))
                if u.path == "/api/news/fetch":
                    day = datetime.now().strftime("%Y-%m-%d")
                    have = conn.execute("SELECT COUNT(*) FROM news WHERE day = ?",
                                        (day,)).fetchone()[0]
                    quota = int(cfg.get("news_count", 10))
                    need = quota - have          # 只补足额度，满了就不再抓
                    got = news_fetch(conn, need) if need > 0 else 0
                    return self._send(200, json.dumps(
                        {"ok": True, "added": got, "today": have + got, "quota": quota}))
                if u.path == "/api/star":
                    d = 1 if int(body.get("delta") or 1) >= 0 else -1
                    conn.execute(
                        "UPDATE words SET study_count = MAX(0, study_count + ?) WHERE id = ?",
                        (d, body.get("id")))
                    conn.commit()
                    r = conn.execute("SELECT study_count FROM words WHERE id = ?",
                                     (body.get("id"),)).fetchone()
                    return self._send(200, json.dumps(
                        {"ok": True, "count": r["study_count"] if r else 0}))
                if u.path == "/api/forget":
                    w = conn.execute("SELECT * FROM words WHERE id = ?",
                                     (body.get("id"),)).fetchone()
                    if not w:
                        return self._send(404, json.dumps({"error": "no such word"}))
                    forget_word(conn, w)
                    conn.commit()
                    return self._send(200, json.dumps({"ok": True}))
                if u.path == "/api/settings":
                    raw = {}
                    if os.path.exists(CFG_PATH):
                        try:
                            with open(CFG_PATH, "r", encoding="utf-8") as f:
                                raw = json.load(f) or {}
                        except Exception:
                            raw = {}
                    k = (body.get("api_key") or "").strip()
                    if k:
                        if not k.isascii() or len(k) < 12:
                            return self._send(200, json.dumps(
                                {"ok": False, "error": "That does not look like a key — it should start with sk- followed by a long string"},
                                ensure_ascii=False))
                        raw["api_key"] = k
                    elif body.get("clear_key"):
                        raw.pop("api_key", None)
                    if body.get("model"):
                        raw["model"] = body["model"]
                    if body.get("level"):
                        raw["level"] = body["level"]
                    save_cfg(raw)
                    cfg.update(load_cfg())
                    if k:
                        test = ai_lookup("rescind", "", cfg, quiet=True)
                        if not test:
                            return self._send(200, json.dumps(
                                {"ok": False, "error": "Key saved, but the test call failed. The key may be wrong, or the model name needs changing."},
                                ensure_ascii=False))
                    return self._send(200, json.dumps({"ok": True}))
                if u.path == "/api/examples":
                    if not cfg.get("api_key"):
                        return self._send(200, json.dumps(
                            {"ok": False, "error": "No API key set"}, ensure_ascii=False))
                    rows = words_without_examples(conn, 1)
                    if not rows:
                        return self._send(200, json.dumps({"ok": True, "done": True, "left": 0}))
                    w = rows[0]
                    n = gen_examples(conn, w, cfg)
                    left = len(words_without_examples(conn))
                    if not n:
                        conn.execute(
                            "INSERT INTO examples (word_id, en, zh, created_at) VALUES (?,?,?,?)",
                            (w["id"], "", "", now_iso()))
                        conn.commit()
                        left = len(words_without_examples(conn))
                    return self._send(200, json.dumps(
                        {"ok": True, "word": w["word"], "made": n, "left": left},
                        ensure_ascii=False))
                return self._send(404, json.dumps({"error": "not found"}))
            finally:
                conn.close()

        def _stats(self, conn):
            by = dict(conn.execute(
                "SELECT status, COUNT(*) FROM reviews GROUP BY status").fetchall())
            nxt = conn.execute(
                "SELECT MIN(due_date) FROM reviews WHERE status IN ('learning','review')"
                " AND due_date > ?", (today(),)).fetchone()[0]
            return {
                "total": conn.execute("SELECT COUNT(*) FROM words").fetchone()[0],
                "new": by.get("new", 0), "learning": by.get("learning", 0),
                "review": by.get("review", 0), "known": by.get("known", 0),
                "inbox_pending": conn.execute(
                    "SELECT COUNT(*) FROM inbox WHERE status='pending'").fetchone()[0],
                "next_due": nxt,
                "queue_size": len(_queue(conn, cfg)),
                "has_key": bool(cfg.get("api_key")),
                "pending_examples": len(words_without_examples(conn)),
            }
    return H


def _lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def gc_orphans(conn):
    """清掉没有对应 words 行的孤儿记录（外键没生效时留下的）。"""
    n = 0
    for t in ("reviews", "contexts", "examples", "review_log"):
        cur = conn.execute(
            "DELETE FROM %s WHERE word_id NOT IN (SELECT id FROM words)" % t)
        n += cur.rowcount or 0
    conn.commit()   # 即使一行没删，DELETE 也已经开了写事务，必须提交，否则一直占着锁
    return n


def dict_backfill(conn, quiet=True):
    """把库里缺释义的词用离线词典补上。装词典前收的词靠这个自愈。"""
    if not dict_db():
        return 0
    rows = conn.execute(
        "SELECT id, word FROM words WHERE definition IS NULL OR definition = ''"
        " OR definition_en IS NULL OR definition_en = ''").fetchall()
    n = 0
    for r in rows:
        d = dict_lookup(r["word"])
        if not d:
            continue
        conn.execute(
            "UPDATE words SET phonetic=?, pos=?, definition=COALESCE(NULLIF(?,''),definition),"
            " definition_en=COALESCE(NULLIF(?,''),definition_en), updated_at=? WHERE id=?",
            (d.get("phonetic"), d.get("pos"), d.get("definition"),
             d.get("definition_en"), now_iso(), r["id"]))
        n += 1
    conn.commit()   # 同上，UPDATE 一执行就开了事务
    if n and not quiet:
        print(dim("  顺手补了 %d 个词的释义" % n))
    return n


def cmd_serve(args, cfg):
    from http.server import ThreadingHTTPServer
    import secrets
    _c = db()
    gc_orphans(_c)
    dict_backfill(_c)
    _c.close()
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    token = ""
    if args.lan:
        token = (os.environ.get("VOCAB_TOKEN") or cfg.get("lan_token") or "").strip()
        if not token:                       # 生成一次就存下来，链接从此不变
            token = secrets.token_urlsafe(9)
            raw = {}
            if os.path.exists(CFG_PATH):
                try:
                    with open(CFG_PATH, "r", encoding="utf-8") as f:
                        raw = json.load(f) or {}
                except Exception:
                    raw = {}
            raw["lan_token"] = token
            save_cfg(raw)
    try:
        httpd = ThreadingHTTPServer((host, args.port), make_handler(cfg, token))
    except OSError as e:
        if getattr(e, "errno", None) in (48, 98):
            print(red("端口 %d 被占用了。" % args.port))
            print(dim("  已经开着的话直接访问就行；换个端口用 v serve -p 8888"))
            return 1
        raise
    if args.lan:
        import subprocess as _sp
        try:
            hostname = _sp.run(["scutil", "--get", "LocalHostName"], timeout=5,
                               stdout=_sp.PIPE).stdout.decode().strip()
        except Exception:
            hostname = ""
        url = "http://%s:%d/?k=%s" % (_lan_ip(), args.port, token)
        print(yellow("  局域网模式：同一 wifi 下、拿到这个链接的人都能读写你的词库。"))
        print(dim("  别在公共 wifi 上开。手机浏览器打开："))
        if hostname:
            print("  %s" % bold("http://%s.local:%d/?k=%s" % (hostname, args.port, token)))
            print(dim("  换了 wifi 导致 IP 变化时，上面这条仍然有效；下面这条是 IP 直连备用："))
    else:
        url = "http://127.0.0.1:%d/" % args.port
    print("  %s" % bold(url))
    print(dim("  Ctrl-C 退出"))
    if args.open and shutil.which("open"):
        subprocess.run(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    return 0


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(prog="v", description="本地生词本")
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("add", help="收一个词")
    a.add_argument("word", nargs="+")
    a.add_argument("-c", "--context", help="遇到它的那句话")
    a.add_argument("-s", "--source", help="来源，比如书名/网站")
    a.add_argument("--no-ai", action="store_true", help="不调 AI，只存词")
    a.set_defaults(func=cmd_add)

    r = sub.add_parser("review", help="开始复习")
    r.set_defaults(func=cmd_review)

    l = sub.add_parser("list", help="列出词")
    l.add_argument("-q", "--query", help="搜词或释义")
    l.add_argument("--due", action="store_true", help="只看今天到期的")
    l.add_argument("--new", action="store_true", help="只看新词")
    l.add_argument("--known", action="store_true", help="只看已掌握的")
    l.add_argument("-n", "--limit", type=int, default=50)
    l.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="看一个词的详情")
    s.add_argument("word")
    s.set_defaults(func=cmd_show)

    st = sub.add_parser("stats", help="统计")
    st.set_defaults(func=cmd_stats)

    d = sub.add_parser("rm", help="删除一个词")
    d.add_argument("word")
    d.set_defaults(func=cmd_rm)

    f = sub.add_parser("ai-fill", help="给缺释义的词批量补全")
    f.add_argument("-n", "--limit", type=int, default=50)
    f.set_defaults(func=cmd_ai_fill)

    w = sub.add_parser("watch", help="盯着剪贴板自动抓生词")
    w.add_argument("--once", action="store_true", help="只处理当前剪贴板内容一次")
    w.add_argument("-v", "--verbose", action="store_true", help="连跳过的也打印出来")
    w.add_argument("--server", help="把抓到的词发给远端服务器，例如 https://x.com/?k=口令")
    w.set_defaults(func=cmd_watch)

    ib = sub.add_parser("inbox", help="确认自动抓来的待定词")
    ib.add_argument("--accept-all", action="store_true", help="全部收下")
    ib.add_argument("--clear", action="store_true", help="全部丢弃")
    ib.set_defaults(func=cmd_inbox)

    sv = sub.add_parser("serve", help="起一个本地复习网页")
    sv.add_argument("-p", "--port", type=int, default=8765)
    sv.add_argument("--lan", action="store_true", help="局域网可访问（手机刷），带 token")
    sv.add_argument("--open", action="store_true", help="顺手打开浏览器")
    sv.set_defaults(func=cmd_serve)

    pv = sub.add_parser("preview", help="本机预览改动，数据隔离，不影响正式库")
    pv.add_argument("-p", "--port", type=int, default=8790)
    pv.add_argument("--fresh", action="store_true", help="重新从正式库拷一份数据")
    pv.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    pv.set_defaults(func=cmd_preview)

    ep = sub.add_parser("export", help="把词库导出成 JSON（备份/迁移）")
    ep.add_argument("path", nargs="?")
    ep.set_defaults(func=cmd_export)

    ip = sub.add_parser("import", help="从 JSON 导回词库")
    ip.add_argument("path")
    ip.set_defaults(func=cmd_import)

    rt = sub.add_parser("roots", help="按词根把词库串起来")
    rt.add_argument("--analyze", action="store_true", help="给还没分析的词跑词根分析")
    rt.add_argument("--min", type=int, default=1, help="至少几个同根词才成组")
    rt.set_defaults(func=cmd_roots)

    pk = sub.add_parser("pick", help="推荐词")
    pk.add_argument("-n", "--size", type=int, default=None)
    pk.add_argument("--reset", action="store_true", help="换一批")
    pk.set_defaults(func=cmd_pick)

    nw = sub.add_parser("news", help="每天的英文商业新闻")
    nw.add_argument("--fetch", action="store_true", help="立刻抓一次")
    nw.add_argument("--all", action="store_true", help="列出全部")
    nw.add_argument("--summarize", action="store_true", help="给还没摘要的写 AI 摘要")
    nw.add_argument("-n", "--limit", type=int, default=10)
    nw.set_defaults(func=cmd_news)

    ex = sub.add_parser("examples", help="给每个词生成 3 个例句（需要 AI）")
    ex.add_argument("-n", "--limit", type=int, default=100)
    ex.set_defaults(func=cmd_examples)

    dc = sub.add_parser("dict", help="离线词典：不用 AI 也有释义")
    dc.add_argument("action", choices=["status", "import"])
    dc.add_argument("path", nargs="?")
    dc.set_defaults(func=cmd_dict)

    fq = sub.add_parser("freq", help="词频表：粗筛掉你肯定认识的词")
    fq.add_argument("action", choices=["status", "import"])
    fq.add_argument("path", nargs="?")
    fq.set_defaults(func=cmd_freq)

    ky = sub.add_parser("key", help="设置 DeepSeek API key（粘贴即可，会自测一次）")
    ky.set_defaults(func=cmd_key)

    cf = sub.add_parser("config", help="查看/修改配置")
    cf.add_argument("action", choices=["show", "set"])
    cf.add_argument("key", nargs="?")
    cf.add_argument("value", nargs="?")
    cf.set_defaults(func=cmd_config)

    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    try:
        return args.func(args, load_cfg())
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main() or 0)
