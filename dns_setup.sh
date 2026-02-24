# 装 Nginx 和 Certbot（免费 HTTPS 证书）
sudo apt install -y nginx certbot python3-certbot-nginx

# 创建 Nginx 配置
sudo tee /etc/nginx/sites-available/cc-manager << 'EOF'
server {
    listen 80;
    server_name cc.weitong.chat;  # 改成你的域名

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
EOF

# 启用配置
sudo ln -sf /etc/nginx/sites-available/cc-manager /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl restart nginx

# 申请免费 HTTPS 证书（按提示输入邮箱）
sudo certbot --nginx -d cc.weitong.chat
