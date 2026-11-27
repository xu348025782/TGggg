# Telegram 转发搬运

## 实现了什么功能

- 把频道帖子转发到收藏夹，自动搬到你的频道
- 有评论区的帖子：帖子 + 评论串一起搬，多少条都全搬
- 没评论区的帖子：也搬，单视频、多图相册都行
- 不带“转发自”，源删了也不影响已搬好的
- 标题里一个字下面藏着的链接、加粗等格式一起搬，图片下面的链接按钮也一起搬（只有网址按钮能搬，回调按钮搬不了）
- 单文件上限 10G，一批上限 10G，尽量 10 张一组搬
- 流量限制：双向算，默认每月 6T，每月 1 号清零，可改成每月几号清零
- 每天早上 8 点自动推昨日流量统计，也能随时查当日 / 本账期用了多少

## 怎么在 VPS 上部署

只写 Debian / Ubuntu，不考虑 Alpine。

1. 在 https://my.telegram.org 申请 api_id / api_hash

2. 目标频道提前绑好讨论组

3. 装 git 和 Python（这 2 个必须先装，不然下面会报错）：

```
apt update && apt install -y git python3 python3-venv python3-pip curl
apt install -y python3.11-venv
```

如果第 5 步建 venv 时提示 `ensurepip is not available`，就先跑 `apt install -y python3.11-venv`，再删掉重建：`rm -rf venv` 后重新跑第 5 步。

4. 从 GitHub 拉代码：

```
mkdir -p ~/copy-thread && cd ~/copy-thread
git clone https://github.com/xu348025782/TGggg.git .
```

5. 装依赖：

```
python3 -m venv venv
source venv/bin/activate
pip install telethon
```

6. 第一次前台跑，输手机号 + 验证码登录：

```
export TG_API_ID=你的api_id
export TG_API_HASH=你的api_hash
export TG_BOT_TOKEN=你的Bot Token
python3 copy_listener.py --self-mode
```

7. 登录成功后按 Ctrl+C 停掉，建个启动脚本，以后就不用每次输 api 和 Token 了：

```
cat > start.sh << 'EOF'
#!/bin/bash
cd ~/copy-thread
source venv/bin/activate
export TG_API_ID=你的api_id
export TG_API_HASH=你的api_hash
export TG_BOT_TOKEN=你的Bot Token
nohup python3 copy_listener.py --self-mode > listener.log 2>&1 &
EOF
chmod 700 start.sh
echo "start.sh" >> .gitignore
```

8. 放后台跑：

```
./start.sh
tail -n 20 listener.log
```

## 换服务器怎么跑

1. 旧服务器上先停掉：`pkill -f copy_listener.py`
2. 保存这 6 个文件，拷到新服务器的 `~/copy-thread/` 里：
   - `copy_listener.session`（用户号登录，不用再收验证码）
   - `bot_listener.session`（Bot 登录）
   - `dest_channel.txt`（当前目标频道）
   - `traffic.json`（本月流量用了多少）
   - `settings.json`（绑定的频道 + 流量限额这些设置）
   - `start.sh`（里面有 api_id、api_hash、Bot Token，不用再找）
3. 新服务器上按上面部署的第 3、4、5 步装好 git、Python、venv 和 telethon，`git clone` 你的仓库
4. 把上面 6 个文件放进 `~/copy-thread/`，跑 `./start.sh` 就行，不用再收验证码、不用再找 api

## 怎么使用

- 把喜欢的频道帖子转发到自己的“收藏夹”就行，不用发链接，关掉手机它也在 VPS 上自己跑，搬完在收藏夹里回你
- 第三方转发到收藏夹不带来源时，改用 Bot 触发：先在 BotFather 建个 Bot 拿到 Token，启动时加 `--bot-token 你的Token`，再把帖子转发给这个 Bot 就开始搬，Bot 里也能发 `/set_dest` 等命令
- 设目标频道：在收藏夹里发 `/set_dest @你的频道`
- 换频道：在收藏夹里发 `/set_dest @新频道`
- 查流量：在收藏夹里发 `/traffic` 查本账期用了多少、还剩多少，发 `/today` 查当日用了多少
- 改清零日：在收藏夹里发 `/set_traffic_day 15`，就改成每月 15 号清零
- 看帮助：在收藏夹里发 `/help`
- 查日志：`tail -f listener.log`
- 以后更新：`cd ~/copy-thread` 再 `git pull`，更新完重新跑部署第 7 步

## 注意事项

- 目标频道要提前绑好讨论组，不然新帖没有评论区
- 你的号必须是目标频道管理员，且能在目标讨论组里发言
- 你的号要能看到源频道、能进源讨论组
- 不要把 `*.session`、`dest_channel.txt`、`traffic.json`、`settings.json`、`start.sh` 传到 GitHub，里面有登录凭证和你的频道、流量记录
- 用主号跑量大了容易被限流，先拿一两条测试
- 换服务器时先停掉旧的，别两台同时跑，再把上面 6 个文件拷到新服务器
