FROM nikolaik/python-nodejs:python3.9-nodejs20

WORKDIR /app

# 1. 安装底层依赖
RUN apt-get update && apt-get install -y ffmpeg xvfb sudo screen tmux

# 2. 安装 Python 依赖
COPY requirements.txt .
RUN pip install -r requirements.txt

# 3. 下载并直装 NapCatQQ
RUN curl -o napcat.sh https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh \
    && bash napcat.sh \
    --qq "3852667296" \
    --mode ws \
    --proxy 0 \
    --confirm

# 4. 把代码和配置文件拷贝进来
COPY app.py .
COPY face_config.json .
COPY all_output_result_kmj.txt .
COPY sensitive_words.txt .

# 5. 使用 sed 强制替换真实端口
ENV BOT_QQ="3852667296"

RUN echo '#!/bin/bash' > start.sh \
    && echo 'export TERM=xterm-256color' >> start.sh \
    && echo 'REAL_PORT=${PORT:-7860}' >> start.sh \
    && echo 'echo "🚀 正在将 Webhook 动态绑定到端口: $REAL_PORT"' >> start.sh \
    && echo 'mkdir -p ~/.config/QQ/NapCat/config ~/.config/QQ/napcat/config' >> start.sh \
    && echo 'cat <<EOF > /tmp/onebot_config.json' >> start.sh \
    && echo '{"network":{"httpServers":[{"name":"flask","enable":true,"port":3000,"host":"0.0.0.0","enableCors":true,"enableWebsocket":false,"messagePostFormat":"array"}],"httpClients":[{"name":"webhook","enable":true,"url":"http://127.0.0.1:MY_PORT_PLACEHOLDER/webhook","messagePostFormat":"array"}],"websocketServers":[],"websocketClients":[]}}' >> start.sh \
    && echo 'EOF' >> start.sh \
    && echo 'sed -i "s/MY_PORT_PLACEHOLDER/$REAL_PORT/g" /tmp/onebot_config.json' >> start.sh \
    && echo 'for dir in "/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/config" "/root/.config/QQ/NapCat/config"; do mkdir -p "$dir" && cp /tmp/onebot_config.json "$dir/onebot11_${BOT_QQ}.json"; done' >> start.sh \
    && echo 'echo "🚀 启动服务..."' >> start.sh \
    && echo 'napcat start ${BOT_QQ}' >> start.sh \
    && echo 'sleep 5' >> start.sh \
    && echo 'napcat log ${BOT_QQ} &' >> start.sh \
    && echo 'python -u app.py' >> start.sh \
    && chmod +x start.sh

EXPOSE 7860

CMD ["bash", "start.sh"]