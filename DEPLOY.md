# 部署到服务器

给帮忙搭服务器的人看。目标：把生词本跑在一台常开的机器上，
这样手机在 4G 下也能用，Mac 关机不影响。

## 需要什么

- Linux，Python 3.9 以上（只用标准库，不需要 pip 装任何东西）
- 磁盘约 200MB（离线词典占大头）
- 一个域名 + HTTPS（强烈建议，见下面「安全」）

## 一、装起来

```bash
git clone https://github.com/jianghui0013-lgtm/daily-vocab.git /opt/vocab
cd /opt/vocab
export VOCAB_HOME=/opt/vocab/data      # 数据放这儿，和代码分开
mkdir -p "$VOCAB_HOME"

# 离线词典（约 63MB 的 csv，导入后生成 55MB 的 dict.db，只需一次）
curl -L -o /tmp/ecdict.csv \
  https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv
python3 vocab.py dict import /tmp/ecdict.csv && rm /tmp/ecdict.csv

# 把已有的词库导进去
python3 vocab.py import seed/words.json
```

## 二、配置（都走环境变量，不要写进仓库）

| 变量 | 作用 |
|---|---|
| `VOCAB_HOME` | 数据目录，词库和词典都放这儿 |
| `VOCAB_TOKEN` | 访问口令，URL 里的 `?k=` 就是它。**自己生成一个长的** |
| `VOCAB_API_KEY` | DeepSeek 密钥，用于写例句和新闻摘要。不填则这两项功能关闭 |

生成口令：`python3 -c "import secrets;print(secrets.token_urlsafe(24))"`

## 三、两个常驻进程

```bash
# 网页服务（--lan 表示绑 0.0.0.0，交给 nginx 反代）
python3 vocab.py serve --lan -p 8765

# 后台任务：每天 6:00 抓 10 篇科技商业新闻、写摘要、补例句
# 服务器上没有剪贴板，这个命令会自动只跑后台任务
python3 vocab.py watch
```

systemd 示例见本文件末尾。

## 四、Mac 上继续抓词

服务器没有剪贴板，抓词仍然要在 Mac 上跑，但要把词发到服务器而不是本机：

```bash
python3 ~/.vocab/vocab.py watch --server "https://你的域名/?k=口令"
```

这样只有一份词库（服务器上的），Mac 和手机看到的完全一致。

## 五、安全（务必看）

**这个程序没有账号系统，唯一的保护就是 URL 里的口令。** 上公网前至少做到：

1. **必须 HTTPS**。否则口令会以明文在网络上传输。
2. 口令用上面的命令生成，别用短的、别提交进仓库。
3. 建议在 nginx 上再加一层 HTTP Basic Auth，双保险。
4. 只反代 `/`，不要把 8765 端口直接暴露到公网。

拿到口令的人可以读写整个词库（加词、删词）。这是个人学习工具，
里面没有敏感数据，但也别把链接贴到公开地方。

## 六、备份

```bash
cp $VOCAB_HOME/vocab.db  备份位置       # 或者
python3 vocab.py export  备份位置/words.json
```

`dict.db` 不用备份，随时可以从 ecdict.csv 重新生成。

## systemd 单元

```ini
# /etc/systemd/system/vocab-web.service
[Unit]
Description=Vocab web
After=network.target

[Service]
Environment=VOCAB_HOME=/opt/vocab/data
Environment=VOCAB_TOKEN=换成你生成的口令
Environment=VOCAB_API_KEY=换成 DeepSeek 密钥
ExecStart=/usr/bin/python3 /opt/vocab/vocab.py serve --lan -p 8765
Restart=always
User=vocab

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/vocab-jobs.service  —— 同上，把 ExecStart 换成：
ExecStart=/usr/bin/python3 /opt/vocab/vocab.py watch
```
