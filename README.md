# v — 本地生词本

单文件 Python + SQLite，零依赖。

    代码  ~/.vocab/vocab.py      命令  ~/.local/bin/v
    数据  ~/.vocab/vocab.db      配置  ~/.vocab/config.json (600)

## 不用终端也能用

已配好开机自启（launchd），登录就自动运行，不用管：

    ~/Library/LaunchAgents/com.local.vocab.serve.plist   网页 http://127.0.0.1:8765
    ~/Library/LaunchAgents/com.local.vocab.watch.plist   剪贴板抓词

桌面上的「生词本」双击即可打开。要停掉：

    launchctl bootout gui/$(id -u)/com.local.vocab.watch
    launchctl bootout gui/$(id -u)/com.local.vocab.serve

## 释义来源

离线词典 ECDICT（40 万条，~/.vocab/dict.db，44MB），完全本地、免费、毫秒级，
自带音标、词性、词形还原（rescinded 自动并进 rescind）和词频。

配了 api_key 的话，词典查不到的词才会问 AI；不配就纯离线，什么都不外发。
api_key 若不是合法格式（含中文、太短）会被自动忽略，不会拿去请求。

## 用法

    v add rescind -c "the board voted to rescind the offer" -s FT
    v add rescind --no-ai          # 不调 AI，只存词
    v review                       # 空格翻面，1 忘了 / 2 模糊 / 3 会了，q 退出
    v list [--due|--new|--known] [-q 关键词]
    v show rescind
    v stats
    v ai-fill                      # 给缺释义的词批量补释义
    v config show / v config set <key> <value>

## 剪贴板自动抓词

    v watch                        # 前台盯着，Ctrl-C 退出
    v watch -v                     # 连「跳过了什么、为什么」也打印
    v watch --once                 # 只处理当前剪贴板一次（调试用）
    v freq status                  # 看词频表有多大
    v freq import <文件>           # 每行一个词、按词频排序，导入后省 AI 调用
                                   # 已导入 google-10000-english（~/.vocab/freq-10k.txt）

后台常驻：

    nohup v watch >> ~/.vocab/watch.log 2>&1 &

## 逻辑要点

- 重复添加不报错：encounter_count +1、例句追加。反复撞见 = 优先学。
- AI 返回 lemma，rescinded / rescind 自动并成一条。
- 复习排程 SM-2 简化版：忘了→归 1 天且 ease-0.2；模糊→×1.2；会了→×ease 且 ease+0.1。
  间隔 ≥60 天自动毕业归档，不再打扰。
- 队列 = 今天到期的（逾期久的靠前）+ 新词（按遇见次数降序，每天上限 daily_new）。
- 凌晨 4 点为一天的分界，跟 Anki 一致。
- 没配 api_key 也能用，只是没有释义。

## 本地网页

    v serve                        # http://127.0.0.1:8765
    v serve --open                 # 顺手打开浏览器
    v serve --lan                  # 手机也能刷：绑 0.0.0.0，URL 带 token
    v serve -p 8888                # 换端口

三个 tab：Words（首页）、Review、⚙。深色跟随系统，数据和 CLI 同一个库。

**页面上不出现任何中文**，为的是逼出英文思维：
- 每个词下面是英文释义（ECDICT 的 WordNet 释义，已按词性挑义项、去掉 n./a. 前缀）
- 中文释义和例句中文翻译做成悬浮提示，不占任何布局空间：
  鼠标移到单词或英文例句上浮出来，手机点一下浮出来，右上角 ZH 可永久显示
- 顶部只有一个输入框：打字即筛选，回车即添加（两用，避免点错）
- Review 标签上的绿色数字 = 今天待复习数量
- 每行右侧 × = 删词，同时记一笔「我认识」，剪贴板以后不再抓它
- **双击页面上任何一个英文单词** = 复制 + 入库（释义里、例句里的词都行）
- 每行右侧的星星 = 背了几次。复习时评分自动 +1 颗，也可以直接点星星手动 +1，
  option-点 减 1

--lan 只靠 URL 里的 token 挡人，同一 wifi 下拿到链接就能读写，别在公共 wifi 上开。

## 商业新闻（News 标签）

每天自动抓 10 条英文商业新闻，来源轮流取，五家各占一点：
BBC / CNBC / Guardian / NPR / MarketWatch（都是公开 RSS，不需要账号）。

    v news              # 列出
    v news --fetch      # 立刻抓一次

网页 News 标签：按日期分组，一行一条（来源 + 标题），点标题展开摘要，
底部「Read the full article ↗」跳原站看全文。**新闻正文里双击任何单词照样入库**，
这是这个标签存在的意义——读着读着词就攒起来了。

只存标题和媒体自己在 RSS 里发布的摘要，不抓取转存原文（版权，也不稳）。
守护进程每 5 分钟检查一次，当天不足 10 条就补。

## 例句

每个词配 3 个短例句（英文 + 中文悬浮），在清单和复习卡片里显示。**必须有 AI 密钥**，
离线词典里没有例句。

有密钥时：剪贴板守护进程兼任后台工人，每约 10 秒补一个词的例句，页面上没例句的词
显示「◌ writing examples…」，并每 8 秒自动刷新，直到补完。也可以在 ⚙ 里一次性批量生成。
没密钥时：不显示等待标记，例句永远不会出现。CLI 等价命令 `v examples`。

例句存在 examples 表，生成失败会写一条空占位行避免无限重试（显示层会过滤掉）。

## 抓词的判定顺序

复制任何内容 → 依次过闸，任何一关拦下就零成本结束：

1. **护栏**：超过 500 字符 / 英文占比 <50% / 像密钥密码 / 像链接路径 / 像代码 /
   像随机串 → 直接忽略。密钥密码绝不入库、绝不外发。
2. **粗筛**：拆词 → 词干还原 → 查词频表，排名 ≤ freq_skip_rank 的常见词剔除；
   已掌握(known)的剔除；曾在 inbox 丢弃过的剔除（等于告诉工具「这个我认识」）。
3. **已有词免费 +1**：候选词若已在库里，直接 encounter_count +1 并追加这句例句，
   不调 AI。反复撞见的词会自动排到复习队列前面。
4. **剩下的直接入库**：查离线词典填好释义音标，直接进复习队列，不再有待确认环节。
   配了 AI 的话，生词分 <0.4 的会被丢掉。

inbox 表现在只用来存「我认识」的词（status='rejected'），即你手动删掉过的词。

例外：**单独复制一个词**时绕过第 2 步的词频表，且最差也进 inbox 不静默丢弃——
你特意复制了一个词，就是明确的「我要这个」。

已知取舍：整句里常见词的生僻用法（"the play had a long **run**"）会被词频表挡掉。
想要就单独复制那个词，或者 `v add run -c "整句"`。降低 freq_skip_rank 能少漏，
但 AI 调用会变多。

采集那一刻绝不打断你：全部静默入库/进 inbox，只弹一条 macOS 通知。

## 备份

    cp ~/.vocab/vocab.db <随便哪儿>      # 单文件，拷走即备份
