#!/bin/bash
# PDCA API 部署脚本（在 VPS 上执行）
# 用法: bash deploy.sh
set -e

DEST=/opt/pdca-api
SERVICE=pdca-api

echo "==> 创建目录"
sudo mkdir -p "$DEST"
sudo chown "$USER":"$USER" "$DEST"

echo "==> 复制文件"
cp pdca_api.py "$DEST/"
cp targets.json "$DEST/"

echo "==> 安装依赖"
cd "$DEST"
python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn asyncpg --quiet

echo "==> 写入 systemd 服务"
sudo tee /etc/systemd/system/${SERVICE}.service > /dev/null <<EOF
[Unit]
Description=PDCA API
After=network.target postgresql.service

[Service]
User=$USER
WorkingDirectory=$DEST
Environment="DATABASE_URL=postgresql://odoo:CHANGE_ME@localhost:5432/odoo17"
Environment="USD_TO_CNY=7.2"
Environment="TARGETS_FILE=$DEST/targets.json"
ExecStart=$DEST/venv/bin/uvicorn pdca_api:app --host 127.0.0.1 --port 8765 --workers 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "==> 启动服务"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"
sudo systemctl restart "$SERVICE"
sleep 2
sudo systemctl status "$SERVICE" --no-pager

echo ""
echo "==> 测试健康检查"
curl -s http://127.0.0.1:8765/api/pdca/health | python3 -m json.tool

echo ""
echo "==> 完成！记得在 Nginx 加转发规则："
echo "    location /api/pdca/ { proxy_pass http://127.0.0.1:8765; }"
