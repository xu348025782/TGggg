1. 准备

去 https://my.telegram.org 申请 api_id / api_hash
目标频道提前绑好讨论组，你的号是那个频道的管理员

2. 装环境，一次粘贴回车

Debian / Ubuntu 的服务器用这组：

apt update && apt install -y python3 python3-venv python3-pip git curl

Alpine 的服务器用这组：

apk add python3 py3-pip git curl

3. 从 GitHub 拉代码

mkdir -p ~/copy-thread && cd ~/copy-thread
git clone https://github.com/xu348025782/TGggg.git .
ls -lh
看到 copy_listener.py 就说明拉成功了。

4. 装 Telethon

python3 -m venv venv
source venv/bin/activate
pip install telethon
5. 第一次前台跑，登录

把下面 3 行里的 3 处换成你自己的，再一次粘贴回车：

export TG_API_ID=你的api_id
export TG_API_HASH=你的api_hash
python3 copy_listener.py --dest-channel "@你的频道" --self-mode
跑起来后它会在终端问你手机号 + 验证码，输完就登录成功了。别走开，这一步要你输验证码。

6. 放后台常驻

登录成功后按 Ctrl+C 停掉，再跑这句：

nohup python3 copy_listener.py --dest-channel "@你的频道" --self-mode > listener.log 2>&1 &
7. 怎么用

在手机上把喜欢的频道帖子转发到自己的“收藏夹”就行，不用发链接，关掉手机它也在 VPS 上自己跑，搬完在收藏夹里回你。

收藏夹里能发的命令：

/set_dest @新频道
/dest
/traffic
/today
/help
8. 以后更新

我改了代码，你传到 GitHub 后，VPS 上跑这两句就更新了，更新完要先停掉旧的再重新跑第 6 步：

cd ~/copy-thread
git pull
查日志看它在干什么：

tail -f listener.log
