#!/usr/bin/env bash
set -euo pipefail

# 自动给脚本加执行权限（可保留，也可删除）
chmod +x "$0" >/dev/null 2>&1 || true

# ========= 基本配置 =========

# 当前脚本所在目录（你是从 /root 运行，那就是 /root）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 所有运行时文件都放在 quant 子目录中，避免把 /root 搞乱
DCF_DIR="$SCRIPT_DIR/quant"

# Python 监控脚本路径
PY_SCRIPT="$DCF_DIR/quant.py"

# Python 命令（如未来用虚拟环境，再改这里）
PYTHON_CMD="python3"

# PID & 日志文件也放在 quant 目录
PID_FILE="$DCF_DIR/quant.pid"
LOG_FILE="$DCF_DIR/quant.log"

# 推送配置由 Web 端管理；安装脚本只保留 push.conf 文件，不提供交互配置。
PUSH_CONF="$DCF_DIR/push.conf"

# venv 目录（依赖安装优先走 venv）
VENV_DIR="$DCF_DIR/.venv"

# GitHub 项目地址（安装脚本只下载 quant.sh，本脚本会再拉取完整项目文件）
REPO_OWNER="byilrq"
REPO_NAME="quant"
REPO_BRANCH="main"
REPO_TARBALL_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/refs/heads/${REPO_BRANCH}.tar.gz"

# sudo 兼容：root 环境可能没有 sudo；非 root 则优先用 sudo
if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
else
    SUDO=""
fi


# ========= 终端颜色（支持时启用） =========
if [ -t 1 ]; then
    C_RESET=$'\033[0m'
    C_BOLD=$'\033[1m'
    C_DIM=$'\033[2m'
    C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'
    C_MAGENTA=$'\033[35m'
    C_CYAN=$'\033[36m'
else
    C_RESET=""
    C_BOLD=""
    C_DIM=""
    C_RED=""
    C_GREEN=""
    C_YELLOW=""
    C_BLUE=""
    C_MAGENTA=""
    C_CYAN=""
fi

# ========= 公共函数 =========


setup_interactive_input() {
    if [ -t 0 ]; then
        stty sane 2>/dev/null || true
        stty erase '^?' 2>/dev/null || true
        bind "set enable-bracketed-paste off" >/dev/null 2>&1 || true
        bind "set editing-mode emacs" >/dev/null 2>&1 || true
    fi
}

prompt_read() {
    local __var_name="$1"
    local __prompt="$2"
    local __default="${3-}"
    local __value=""

    if [ -t 0 ]; then
        setup_interactive_input
        if [ -n "$__default" ]; then
            read -e -r -p "$__prompt" -i "$__default" __value || return 1
        else
            read -e -r -p "$__prompt" __value || return 1
        fi
    else
        read -r -p "$__prompt" __value || return 1
    fi

    printf -v "$__var_name" '%s' "$__value"
}

ensure_quant_dir() {
    if [ ! -d "$DCF_DIR" ]; then
        echo "创建目录: $DCF_DIR"
        mkdir -p "$DCF_DIR"
    fi
}


# ============================================
# Let's Encrypt 证书处理（Web 管理端）
# ============================================
get_public_ipv4() {
    local ip=""
    ip="$(ip route get 8.8.8.8 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' || true)"
    if [ -z "$ip" ] && command -v curl >/dev/null 2>&1; then
        ip="$(curl -4fsS --max-time 5 https://ipv4.icanhazip.com 2>/dev/null | tr -d '[:space:]' || true)"
    fi
    echo "$ip"
}

find_cert_name_by_domain() {
    local cert_domain="$1"
    local d=""
    if [ -f "/etc/letsencrypt/live/${cert_domain}/fullchain.pem" ] && [ -f "/etc/letsencrypt/live/${cert_domain}/privkey.pem" ]; then
        echo "$cert_domain"
        return 0
    fi
    for d in /etc/letsencrypt/live/"${cert_domain}"*; do
        [ -d "$d" ] || continue
        if [ -f "$d/fullchain.pem" ] && [ -f "$d/privkey.pem" ]; then
            basename "$d"
            return 0
        fi
    done
    return 1
}

get_cert_paths() {
    local cert_domain="$1"
    local cert_name=""
    cert_name="$(find_cert_name_by_domain "$cert_domain" 2>/dev/null || true)"
    [ -n "$cert_name" ] || return 1
    [ -f "/etc/letsencrypt/live/${cert_name}/fullchain.pem" ] || return 1
    [ -f "/etc/letsencrypt/live/${cert_name}/privkey.pem" ] || return 1
    echo "${cert_name}|/etc/letsencrypt/live/${cert_name}/fullchain.pem|/etc/letsencrypt/live/${cert_name}/privkey.pem"
}

cert_files_exist() {
    get_cert_paths "$1" >/dev/null 2>&1
}

cert_is_valid() {
    local cert_domain="$1"
    local cert_info="" cert_file=""
    cert_info="$(get_cert_paths "$cert_domain" 2>/dev/null || true)"
    [ -n "$cert_info" ] || return 1
    cert_file="$(echo "$cert_info" | cut -d'|' -f2)"
    [ -f "$cert_file" ] || return 1
    openssl x509 -checkend 0 -noout -in "$cert_file" >/dev/null 2>&1 || return 1
    if openssl x509 -in "$cert_file" -noout -text 2>/dev/null | grep -A1 "Subject Alternative Name" | grep -qw "DNS:${cert_domain}"; then
        return 0
    fi
    openssl x509 -in "$cert_file" -noout -subject 2>/dev/null | grep -Eq "CN[[:space:]]*=[[:space:]]*${cert_domain}([,/]|$)"
}

cert_key_matches() {
    local cert_domain="$1"
    local cert_info="" cert_file="" key_file="" cert_pub="" key_pub=""
    cert_info="$(get_cert_paths "$cert_domain" 2>/dev/null || true)"
    [ -n "$cert_info" ] || return 1
    cert_file="$(echo "$cert_info" | cut -d'|' -f2)"
    key_file="$(echo "$cert_info" | cut -d'|' -f3)"
    [ -f "$cert_file" ] && [ -f "$key_file" ] || return 1
    cert_pub="$(openssl x509 -in "$cert_file" -pubkey -noout 2>/dev/null | openssl pkey -pubin -outform pem 2>/dev/null || true)"
    key_pub="$(openssl pkey -in "$key_file" -pubout -outform pem 2>/dev/null || true)"
    [ -n "$cert_pub" ] && [ -n "$key_pub" ] && [ "$cert_pub" = "$key_pub" ]
}

show_local_cert_info() {
    local cert_domain="$1"
    local cert_info="" cert_file=""
    cert_info="$(get_cert_paths "$cert_domain" 2>/dev/null || true)"
    cert_file="$(echo "$cert_info" | cut -d'|' -f2)"
    if [ -f "$cert_file" ]; then
        echo "域名: ${cert_domain}"
        openssl x509 -noout -subject -issuer -dates -in "$cert_file" 2>/dev/null || true
    fi
}

ensure_certbot_installed() {
    if command -v certbot >/dev/null 2>&1; then
        return 0
    fi
    echo "未检测到 certbot，正在安装..."
    if command -v apt-get >/dev/null 2>&1; then
        ${SUDO} apt-get update -y
        ${SUDO} apt-get install -y certbot
    elif command -v yum >/dev/null 2>&1; then
        ${SUDO} yum install -y certbot
    else
        echo "❌ 未检测到 apt-get/yum，无法自动安装 certbot。"
        return 1
    fi
}

issue_cert_webroot() {
    local cert_domain="$1"
    local acme_root="/var/www/acme"
    local acme_site="/etc/nginx/sites-available/quant-acme-${cert_domain}.conf"
    local acme_link="/etc/nginx/sites-enabled/quant-acme-${cert_domain}.conf"
    ${SUDO} mkdir -p "$acme_root" /etc/nginx/sites-available /etc/nginx/sites-enabled
    ${SUDO} tee "$acme_site" >/dev/null <<ACME_NGINX
server {
    listen 80;
    listen [::]:80;
    server_name ${cert_domain};

    location ^~ /.well-known/acme-challenge/ {
        root ${acme_root};
        default_type "text/plain";
    }

    location / {
        return 404;
    }
}
ACME_NGINX
    ${SUDO} ln -sf "$acme_site" "$acme_link"
    if ! ${SUDO} nginx -t; then
        echo "❌ nginx ACME 临时配置检查失败。"
        return 1
    fi
    ${SUDO} systemctl reload nginx 2>/dev/null || ${SUDO} systemctl restart nginx || return 1
    ${SUDO} certbot certonly --webroot -w "$acme_root" --non-interactive --agree-tos --register-unsafely-without-email -d "$cert_domain"
}

issue_cert_standalone() {
    local cert_domain="$1"
    echo "webroot 方式失败，尝试 standalone 方式申请证书..."
    ${SUDO} systemctl stop nginx 2>/dev/null || true
    ${SUDO} systemctl stop apache2 2>/dev/null || true
    ${SUDO} systemctl stop caddy 2>/dev/null || true
    sleep 2
    if ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE '(^|:)80$'; then
        echo "❌ 80 端口仍被占用，无法使用 standalone 方式申请证书。"
        ss -lntp 2>/dev/null | grep -E '(^|:)80 ' || true
        ${SUDO} systemctl start nginx 2>/dev/null || true
        return 1
    fi
    ${SUDO} certbot certonly --standalone --non-interactive --agree-tos --register-unsafely-without-email -d "$cert_domain"
}

prepare_web_cert_for_domain() {
    local cert_domain="$1"
    local server_ip="" resolved_ip="" cert_info=""
    echo "检查域名证书：${cert_domain}"

    if cert_files_exist "$cert_domain" && cert_is_valid "$cert_domain" && cert_key_matches "$cert_domain"; then
        echo "检测到有效 Let's Encrypt 证书，直接复用。"
        show_local_cert_info "$cert_domain"
        return 0
    fi

    echo "未找到可用正式证书，将自动申请 Let's Encrypt 证书。"
    server_ip="$(get_public_ipv4)"
    resolved_ip="$(getent ahostsv4 "$cert_domain" 2>/dev/null | awk 'NR==1{print $1}' || true)"
    if [ -n "$server_ip" ] && [ -n "$resolved_ip" ] && [ "$server_ip" != "$resolved_ip" ]; then
        echo "⚠️ 域名解析 IP 与本机公网 IP 可能不一致："
        echo "   域名解析: $resolved_ip"
        echo "   本机公网: $server_ip"
        echo "   证书申请可能失败，请确认 DNS 已指向本机。"
    fi

    ensure_certbot_installed || return 1

    if issue_cert_webroot "$cert_domain"; then
        :
    else
        issue_cert_standalone "$cert_domain" || {
            ${SUDO} systemctl start nginx 2>/dev/null || true
            echo "❌ Let's Encrypt 证书申请失败。请检查域名解析、80端口、防火墙/安全组。"
            return 1
        }
    fi

    ${SUDO} systemctl start nginx 2>/dev/null || true

    if cert_files_exist "$cert_domain" && cert_is_valid "$cert_domain" && cert_key_matches "$cert_domain"; then
        echo "✅ Let's Encrypt 证书已就绪。"
        show_local_cert_info "$cert_domain"
        return 0
    fi

    echo "❌ 证书文件存在性/有效性/私钥匹配校验未通过。"
    return 1
}

backup_path_if_exists() {
    local target="$1"
    local ts
    ts="$(date '+%Y%m%d_%H%M%S')"
    if [ -e "$target" ]; then
        cp -a "$target" "${target}.bak.${ts}"
    fi
}

copy_project_file_overwrite() {
    local src="$1"
    local dst="$2"
    if [ -f "$src" ]; then
        backup_path_if_exists "$dst"
        cp -a "$src" "$dst"
        echo "✅ 已更新: $(basename "$dst")"
    fi
}

copy_project_file_if_missing() {
    local src="$1"
    local dst="$2"
    if [ -f "$src" ] && [ ! -f "$dst" ]; then
        cp -a "$src" "$dst"
        echo "✅ 已初始化: $(basename "$dst")"
    elif [ -f "$src" ]; then
        echo "ℹ️  保留已有配置: $(basename "$dst")"
    fi
}

copy_project_dir_overwrite() {
    local src="$1"
    local dst="$2"
    local ts
    ts="$(date '+%Y%m%d_%H%M%S')"
    if [ -d "$src" ]; then
        if [ -e "$dst" ]; then
            mv "$dst" "${dst}.bak.${ts}"
        fi
        cp -a "$src" "$dst"
        echo "✅ 已更新目录: $(basename "$dst")"
    fi
}

ssetup_domain_https() {
    echo -e "${C_CYAN}========== 域名与 HTTPS 设置 ==========${C_RESET}"
    
    # 安全处理未定义变量（set -u 环境下）
    local DOMAIN=""
    local PUBLIC_PORT=""
    local INTERNAL_PORT=""
    
    # ---------- 1. 读取当前配置（直接从 web_portal.json）----------
    local cfg_file="$DCF_DIR/web_portal.json"
    if [[ -f "$cfg_file" ]]; then
        # 使用 python 解析 JSON，避免依赖 jq
        DOMAIN=$($VENV_DIR/bin/python -c "import json; print(json.load(open('$cfg_file')).get('domain', ''))" 2>/dev/null || echo "")
        PUBLIC_PORT=$($VENV_DIR/bin/python -c "import json; print(json.load(open('$cfg_file')).get('public_port', ''))" 2>/dev/null || echo "")
        INTERNAL_PORT=$($VENV_DIR/bin/python -c "import json; print(json.load(open('$cfg_file')).get('internal_port', ''))" 2>/dev/null || echo "")
    fi
    
    # 如果读取失败，使用默认值或提示输入
    if [[ -z "$DOMAIN" ]]; then
        read -p "请输入域名（例如 xiany.de）: " DOMAIN
    else
        read -p "当前域名: $DOMAIN，确认请直接回车，修改请输入新域名: " new_domain
        [[ -n "$new_domain" ]] && DOMAIN="$new_domain"
    fi
    
    if [[ -z "$PUBLIC_PORT" ]]; then
        read -p "请输入对外公开端口（默认 819）: " PUBLIC_PORT
        PUBLIC_PORT=${PUBLIC_PORT:-819}
    else
        read -p "当前公开端口: $PUBLIC_PORT，确认直接回车，修改请输入新端口: " new_port
        [[ -n "$new_port" ]] && PUBLIC_PORT="$new_port"
    fi
    
    if [[ -z "$INTERNAL_PORT" ]]; then
        INTERNAL_PORT=1819
    fi
    
    echo -e "${C_GREEN}将配置域名: $DOMAIN，HTTPS 端口: $PUBLIC_PORT，后端端口: $INTERNAL_PORT${C_RESET}"
    
    # ---------- 2. 检查并安装 certbot ----------
    if ! command -v certbot &>/dev/null; then
        echo -e "${C_YELLOW}正在安装 Certbot...${C_RESET}"
        apt update && apt install -y certbot python3-certbot-nginx
        if [[ $? -ne 0 ]]; then
            echo -e "${C_RED}Certbot 安装失败，请手动安装后重试。${C_RESET}"
            return 1
        fi
    fi
    
    # ---------- 3. 确保 80 端口可以访问本域名（临时配置） ----------
    local temp_nginx_conf="/etc/nginx/sites-available/temp_${DOMAIN}"
    local temp_enabled="/etc/nginx/sites-enabled/temp_${DOMAIN}"
    local need_cleanup=0
    
    if ! nginx -T 2>/dev/null | grep -q "server_name.*$DOMAIN"; then
        echo -e "${C_YELLOW}当前 Nginx 未配置域名 $DOMAIN 的 HTTP 访问，将临时添加配置用于证书验证...${C_RESET}"
        cat > "$temp_nginx_conf" <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    root /var/www/html;
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }
}
EOF
        ln -sf "$temp_nginx_conf" "$temp_enabled"
        nginx -t && systemctl reload nginx
        need_cleanup=1
    fi
    
    # ---------- 4. 申请 SSL 证书 ----------
    echo -e "${C_GREEN}正在申请 SSL 证书...${C_RESET}"
    mkdir -p /var/www/html/.well-known/acme-challenge
    certbot certonly --webroot -w /var/www/html -d "$DOMAIN" \
        --non-interactive --agree-tos --register-unsafely-without-email \
        --keep-until-expiring
    if [[ $? -ne 0 ]]; then
        echo -e "${C_RED}证书申请失败，请检查域名解析是否正确（需指向本服务器 IP）以及 80 端口是否可访问。${C_RESET}"
        [[ $need_cleanup -eq 1 ]] && rm -f "$temp_enabled" "$temp_nginx_conf" && systemctl reload nginx
        return 1
    fi
    
    # 清理临时配置
    if [[ $need_cleanup -eq 1 ]]; then
        rm -f "$temp_enabled" "$temp_nginx_conf"
        systemctl reload nginx
    fi
    
    # 证书路径
    local cert_path="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
    local key_path="/etc/letsencrypt/live/$DOMAIN/privkey.pem"
    if [[ ! -f "$cert_path" ]]; then
        echo -e "${C_RED}证书文件未找到，配置中止。${C_RESET}"
        return 1
    fi
    
    # ---------- 5. 配置 Nginx 支持 HTTPS ----------
    local nginx_conf="/etc/nginx/sites-available/quant-${DOMAIN}-ssl"
    local nginx_enabled="/etc/nginx/sites-enabled/quant-${DOMAIN}-ssl"
    
    cat > "$nginx_conf" <<EOF
# HTTPS server block for $DOMAIN (port $PUBLIC_PORT)
server {
    listen $PUBLIC_PORT ssl;
    server_name $DOMAIN;

    ssl_certificate $cert_path;
    ssl_certificate_key $key_path;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    location / {
        proxy_pass http://127.0.0.1:$INTERNAL_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}

# (可选) 将原有的 HTTP 访问强制跳转到 HTTPS
server {
    listen 80;
    server_name $DOMAIN;
    return 301 https://\$server_name:$PUBLIC_PORT\$request_uri;
}
EOF

    # 如果用户希望使用标准 443 端口，可以额外添加一个监听 443 的 server 块
    if [[ "$PUBLIC_PORT" != "443" ]]; then
        echo -e "${C_YELLOW}提示：您设置的 HTTPS 端口是 $PUBLIC_PORT，访问请使用 https://$DOMAIN:$PUBLIC_PORT${C_RESET}"
        read -p "是否同时配置标准 443 端口（需要 443 未占用）？(y/N): " add_443
        if [[ "$add_443" =~ ^[Yy]$ ]]; then
            cat >> "$nginx_conf" <<EOF

# Standard 443 port fallback
server {
    listen 443 ssl;
    server_name $DOMAIN;
    ssl_certificate $cert_path;
    ssl_certificate_key $key_path;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
    location / {
        proxy_pass http://127.0.0.1:$INTERNAL_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
        fi
    fi
    
    # 启用配置并测试
    ln -sf "$nginx_conf" "$nginx_enabled"
    nginx -t
    if [[ $? -ne 0 ]]; then
        echo -e "${C_RED}Nginx 配置文件语法错误，已取消启用。请手动检查 $nginx_conf${C_RESET}"
        rm -f "$nginx_enabled"
        return 1
    fi
    
    systemctl reload nginx
    echo -e "${C_GREEN}✅ HTTPS 配置成功！${C_RESET}"
    echo -e "访问地址：${C_BOLD}https://$DOMAIN:$PUBLIC_PORT${C_RESET}"
    echo -e "证书自动续期任务已由 Certbot 定时服务管理（检查：systemctl status certbot.timer）"
}

update_project_files() {
    ensure_quant_dir
    echo "================================="
    echo "开始下载/更新 Quant 项目文件..."
    echo "来源: $REPO_TARBALL_URL"
    echo "目标: $DCF_DIR"
    echo "================================="

    if ! command -v curl >/dev/null 2>&1; then
        echo "❌ 未检测到 curl，请先安装 curl。"
        return 1
    fi
    if ! command -v tar >/dev/null 2>&1; then
        echo "❌ 未检测到 tar，请先安装 tar。"
        return 1
    fi

    local tmpdir srcdir
    tmpdir="$(mktemp -d)"

    if ! curl -fsSL "$REPO_TARBALL_URL" -o "$tmpdir/quant.tar.gz"; then
        echo "❌ 下载项目压缩包失败，请检查 GitHub 网络访问。"
        rm -rf "$tmpdir"
        return 1
    fi
    if ! tar -xzf "$tmpdir/quant.tar.gz" -C "$tmpdir"; then
        echo "❌ 解压项目压缩包失败。"
        rm -rf "$tmpdir"
        return 1
    fi

    srcdir="$tmpdir/${REPO_NAME}-${REPO_BRANCH}"
    if [ ! -d "$srcdir" ]; then
        echo "❌ 未找到解压后的项目目录: $srcdir"
        rm -rf "$tmpdir"
        return 1
    fi

    # 代码/模板文件：更新时覆盖，但会自动备份旧版本
    copy_project_file_overwrite "$srcdir/quant.py" "$DCF_DIR/quant.py"
    copy_project_file_overwrite "$srcdir/strategy.py" "$DCF_DIR/strategy.py"
    copy_project_file_overwrite "$srcdir/quant_web.py" "$DCF_DIR/quant_web.py"
    copy_project_file_overwrite "$srcdir/backtest_quant.py" "$DCF_DIR/backtest_quant.py"
    copy_project_file_overwrite "$srcdir/requirements.txt" "$DCF_DIR/requirements.txt"
    copy_project_dir_overwrite "$srcdir/web_templates" "$DCF_DIR/web_templates"
    copy_project_dir_overwrite "$srcdir/web_static" "$DCF_DIR/web_static"
	copy_project_file_overwrite "$srcdir/push.py" "$DCF_DIR/push.py"
	copy_project_file_overwrite "$srcdir/market_data.py" "$DCF_DIR/market_data.py"
	copy_project_file_overwrite "$srcdir/push.conf" "$DCF_DIR/push.conf"
	copy_project_file_overwrite "$srcdir/quant.yaml" "$DCF_DIR/quant.yaml"
	
	market_data.py

    # 用户配置/运行状态：只在缺失时初始化，避免覆盖实盘参数和推送密钥
    copy_project_file_if_missing "$srcdir/quant.yaml" "$DCF_DIR/quant.yaml"
    copy_project_file_if_missing "$srcdir/push.conf" "$DCF_DIR/push.conf"

    [ -f "$DCF_DIR/push.conf" ] && chmod 600 "$DCF_DIR/push.conf" || true

    echo "================================="
    echo "项目文件更新完成 ✅"
    echo "注意：quant.yaml / push.conf 如已存在不会被覆盖。"
    echo "================================="

    rm -rf "$tmpdir"
}

self_check_project_files() {
    ensure_quant_dir
    local ok=1
    echo "=============== Quant 项目文件检查 ==============="
    for f in quant.py strategy.py quant_web.py backtest_quant.py quant.yaml; do
        if [ -f "$DCF_DIR/$f" ]; then
            echo "✅ $f"
        else
            echo "❌ 缺少 $f"
            ok=0
        fi
    done
    for d in web_templates web_static; do
        if [ -d "$DCF_DIR/$d" ]; then
            echo "✅ $d/"
        else
            echo "❌ 缺少 $d/"
            ok=0
        fi
    done
    if [ "$ok" -eq 1 ]; then
        echo "项目文件检查通过。"
    else
        echo "项目文件不完整，请执行菜单 1 下载/更新项目与依赖。"
        return 1
    fi
}

# ============================================
# 依赖安装/更新（系统依赖 + Python依赖）
# 通过 update_rely() 实现
# ============================================
update_rely() {
    ensure_quant_dir
    echo "================================="
    echo "开始安装/更新依赖..."
    echo "目标目录: $DCF_DIR"
    echo "虚拟环境: $VENV_DIR"
    echo "================================="

    # ---------- 基本检查 ----------
    if [ "$(id -u)" -ne 0 ] && [ -z "$SUDO" ]; then
        echo "❌ 当前不是 root，且未检测到 sudo，无法安装系统依赖。"
        return 1
    fi

    # ---------- 1) 系统依赖 ----------
    echo "[1/5] 安装系统依赖（python3-venv / python3-pip 等）"
    if ! ${SUDO} apt-get update -y; then
        echo "❌ apt-get update 失败。可能是网络/源/锁占用问题。"
        echo "   你可以先执行：${SUDO} lsof /var/lib/dpkg/lock-frontend 或等待系统自动更新完成。"
        return 1
    fi

    if ! ${SUDO} apt-get install -y \
        python3 python3-venv python3-pip \
        ca-certificates curl wget tar nginx openssl cron \
        build-essential; then
        echo "❌ apt-get install 失败。"
        return 1
    fi

    # ---------- 2) 下载/更新项目文件 ----------
    echo "[2/5] 下载/更新项目文件"
    if ! update_project_files; then
        echo "❌ 项目文件更新失败。"
        return 1
    fi

    # ---------- 3) 创建/更新虚拟环境 ----------
    echo "[3/5] 准备虚拟环境: $VENV_DIR"

    if [ -d "$VENV_DIR" ] && [ ! -x "$VENV_DIR/bin/python" ]; then
        echo "⚠️ 检测到虚拟环境可能损坏（缺少 $VENV_DIR/bin/python），将重建..."
        rm -rf "$VENV_DIR"
    fi

    if [ ! -d "$VENV_DIR" ]; then
        if ! python3 -m venv "$VENV_DIR"; then
            echo "❌ 创建虚拟环境失败。"
            return 1
        fi
    fi

    # shellcheck disable=SC1090
    source "$VENV_DIR/bin/activate" || {
        echo "❌ 激活虚拟环境失败。"
        return 1
    }

    local VPY="$VENV_DIR/bin/python"
    local VPIP="$VENV_DIR/bin/pip"

    echo "   使用 Python: $($VPY -V 2>/dev/null)"
    echo "   使用 pip:    $($VPIP -V 2>/dev/null)"

    echo "[4/5] 升级 pip/setuptools/wheel"
    if ! $VPY -m pip install -U pip setuptools wheel; then
        echo "❌ pip 基础组件升级失败。"
        deactivate || true
        return 1
    fi

    # ---------- 3) 安装 Python 依赖 ----------
    echo "[5/5] 安装 Python 依赖"

    if [ -f "$DCF_DIR/requirements.txt" ]; then
        echo "   检测到 requirements.txt，按其安装/更新依赖..."
        # 确保 requirements.txt 中包含 easyquotation 或手动添加
        if ! $VPY -m pip install -U -r "$DCF_DIR/requirements.txt"; then
            echo "❌ requirements.txt 安装失败。"
            deactivate || true
            return 1
        fi
        # 额外确保 easyquotation 已安装（若 requirements.txt 中缺失）
        if ! $VPY -m pip show easyquotation >/dev/null 2>&1; then
            echo "   补充安装 easyquotation..."
            $VPY -m pip install -U easyquotation || {
                echo "❌ easyquotation 安装失败。"
                deactivate || true
                return 1
            }
        fi
    else
        echo "   未检测到 requirements.txt，安装默认依赖（requests / pyyaml / json5 / pandas / yfinance / scipy / flask / gunicorn / ruamel.yaml / werkzeug / baostock / easyquotation）"
        if ! $VPY -m pip install -U requests pyyaml json5 pandas yfinance scipy flask gunicorn ruamel.yaml werkzeug baostock easyquotation; then
            echo "❌ 依赖安装失败。"
            deactivate || true
            return 1
        fi
    fi

    # ---------- BaoStock：A股含权息回测源依赖 ----------
    echo "   安装/更新 BaoStock（A股含权息回测源）..."
    if ! $VPY -m pip install -U baostock; then
        echo "❌ BaoStock 安装失败。"
        deactivate || true
        return 1
    fi

    # ---------- 自检：import 测试 ----------
    echo "   进行依赖自检（import requests/yaml/json5/pandas/yfinance/scipy/flask/ruamel/baostock/easyquotation）..."
    if ! $VPY - <<'PY'
import sys
ok = True
checks = [
    ("requests","requests"), ("yaml","yaml"), ("json5","json5"), ("pandas","pandas"),
    ("yfinance","yfinance"), ("scipy","scipy"), ("flask","flask"), ("ruamel","ruamel.yaml"),
    ("baostock","baostock"), ("easyquotation","easyquotation")   # <-- 新增 easyquotation
]
for label, mod in checks:
    try:
        __import__(mod)
        print(f"✅ import {label} OK")
    except Exception as e:
        ok = False
        print(f"❌ import {label} FAILED: {e}")
sys.exit(0 if ok else 1)
PY
    then
        echo "❌ 依赖自检未通过。请检查网络、pip 源或 Python 版本。"
        deactivate || true
        return 1
    fi

    echo "已安装的关键包版本："
    "$VPY" - <<'PY'
import yaml, json5, requests, pandas, yfinance, scipy, flask, ruamel.yaml, baostock, easyquotation
print("requests:", requests.__version__)
print("pyyaml:  ", yaml.__version__)
print("json5:   ", json5.__version__)
print("pandas:  ", pandas.__version__)
print("yfinance:", yfinance.__version__)
print("scipy:   ", scipy.__version__)
print("flask:   ", flask.__version__)
print("ruamel:  ", ruamel.yaml.__version__)
print("baostock:", getattr(baostock, "__version__", "unknown"))
print("easyquotation:", getattr(easyquotation, "__version__", "unknown"))
PY

    echo "================================="
    echo "依赖安装完成 ✅"
    echo "Python: $($VPY -V)"
    echo "pip:    $($VPIP -V)"
    echo "================================="

    deactivate || true

    echo
    prompt_read _web_ans "是否现在配置 Web 管理端（nginx 819 + 登录页）？(y/n): " "n"
    if [[ "${_web_ans:-n}" =~ ^[yY]$ ]]; then
        configure_web_portal
    fi

    return 0
}

add_cron_watchdog() {
    local cron_line="*/5 * * * * bash $SCRIPT_DIR/quant.sh --cron-check >/dev/null 2>&1"
    (crontab -l 2>/dev/null | grep -v "quant.sh --cron-check" || true; echo "$cron_line") | crontab -
    echo "已在 crontab 中添加每5分钟检查任务。"
}

remove_cron_watchdog() {
    (crontab -l 2>/dev/null | grep -v "quant.sh --cron-check" || true) | crontab - 2>/dev/null || true
    echo "已从 crontab 中移除检查任务（如存在）。"
}

cron_check() {
    ensure_quant_dir

    if [ -f "$PID_FILE" ]; then
        local PID
        PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
        if [ -n "${PID}" ] && ps -p "$PID" >/dev/null 2>&1; then
            exit 0
        else
            rm -f "$PID_FILE"
        fi
    fi

    echo "$(date '+%Y.%m.%d.%H:%M:%S') [cron-check] 检测到 quant.py 未运行，自动重启..." >> "$LOG_FILE"

    if [ -x "$VENV_DIR/bin/python" ]; then
        nohup "$VENV_DIR/bin/python" "$PY_SCRIPT" >> "$LOG_FILE" 2>&1 &
    else
        nohup "$PYTHON_CMD" "$PY_SCRIPT" >> "$LOG_FILE" 2>&1 &
    fi

    local NEW_PID=$!
    echo "$NEW_PID" > "$PID_FILE"
    echo "$(date '+%Y.%m.%d.%H:%M:%S') [cron-check] 已重新启动 quant.py，PID=$NEW_PID" >> "$LOG_FILE"
}

start_quant() {
    ensure_quant_dir

    if [ ! -f "$PY_SCRIPT" ]; then
        echo "找不到 $PY_SCRIPT，请先执行菜单 1 下载/更新项目与依赖。"
        return 1
    fi

    if [ -f "$PID_FILE" ]; then
        local PID
        PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
        if [ -n "${PID}" ] && ps -p "$PID" >/dev/null 2>&1; then
            echo "quant.py 已在运行中（PID=$PID），如需重启请先选择“停止脚本”。"
            return 0
        else
            rm -f "$PID_FILE"
        fi
    fi

    echo "启动 quant.py ..."
    echo "日志文件：$LOG_FILE"

    if [ -x "$VENV_DIR/bin/python" ]; then
        nohup "$VENV_DIR/bin/python" "$PY_SCRIPT" >> "$LOG_FILE" 2>&1 &
    else
        echo "提示：未检测到虚拟环境 $VENV_DIR，建议先执行菜单 1 下载/更新项目与依赖。"
        nohup "$PYTHON_CMD" "$PY_SCRIPT" >> "$LOG_FILE" 2>&1 &
    fi

    local NEW_PID=$!
    echo "$NEW_PID" > "$PID_FILE"
    echo "quant.py 已启动，PID=$NEW_PID"
    add_cron_watchdog
}

stop_quant() {
    ensure_quant_dir

    if [ ! -f "$PID_FILE" ]; then
        echo "没有找到 PID 文件，可能 quant.py 未在运行。"
        remove_cron_watchdog
        return 0
    fi

    local PID
    PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [ -z "${PID}" ] || ! ps -p "$PID" >/dev/null 2>&1; then
        echo "PID 文件存在但进程未运行，清理 PID 文件。"
        rm -f "$PID_FILE"
        remove_cron_watchdog
        return 0
    fi

    echo "正在停止 quant.py (PID=$PID)..."
    kill "$PID" || true

    sleep 2
    if ps -p "$PID" >/dev/null 2>&1; then
        echo "进程未退出，尝试强制 kill -9..."
        kill -9 "$PID" || true
    fi

    rm -f "$PID_FILE"
    echo "quant.py 已停止。"
    remove_cron_watchdog
}

# ============================================
# 推送配置说明
# ============================================
# push.conf 由 Web 管理端的“推送”页面维护；脚本不再提供推送配置入口。

# ============================================
# 状态查询（含 cron）
# ============================================
show_status() {
    ensure_quant_dir
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
        if [ -n "${PID}" ] && ps -p "$PID" > /dev/null 2>&1; then
            echo "quant.py 正在运行（PID=$PID）。"
        else
            echo "PID 文件存在，但进程未运行。"
        fi
    else
        echo "quant.py 当前未在运行。"
    fi
    echo "当前cron任务："
    crontab -l 2>/dev/null | grep "quant.sh --cron-check" || echo "无相关cron任务。"
}




# ========= 若以 --cron-check 启动，则只做检查后退出 =========
if [ "${1:-}" = "--cron-check" ]; then
    cron_check
    exit 0
fi

# ============================================
# Web 管理端（Flask + gunicorn + nginx）
# ============================================
WEB_APP_FILE="$DCF_DIR/quant_web.py"
WEB_CONF_FILE="$DCF_DIR/web_portal.json"
WEB_SERVICE_FILE="/etc/systemd/system/quant-web.service"
WEB_NGINX_SITE="/etc/nginx/sites-available/quant-web-2096.conf"
WEB_NGINX_LINK="/etc/nginx/sites-enabled/quant-web-2096.conf"
WEB_INTERNAL_PORT="2097"
WEB_PUBLIC_PORT="2096"

configure_web_portal() {
    ensure_quant_dir

    if [ ! -f "$WEB_APP_FILE" ]; then
        echo "❌ 未找到 $WEB_APP_FILE，请先把 quant_web.py / web_templates / web_static 放到 $DCF_DIR"
        return 1
    fi

    # 端口定义：外部端口 2096(HTTPS)，内部端口 2097( gunicorn)
    local WEB_INTERNAL_PORT="2097"
    local WEB_PUBLIC_PORT="2096"

    local domain="sharq.eu.org"
    local admin_user="admin"
    local admin_pass=""
    local cert_file=""
    local key_file=""
    local cert_info=""
    local cert_name=""

    # 从配置文件读取已保存的参数
    if [ -f "$WEB_CONF_FILE" ]; then
        domain="$(grep -oP '"domain"\s*:\s*"\K[^"]+' "$WEB_CONF_FILE" 2>/dev/null || echo "sharq.eu.org")"
        admin_user="$(grep -oP '"admin_username"\s*:\s*"\K[^"]+' "$WEB_CONF_FILE" 2>/dev/null || echo "admin")"
    fi

    echo
    echo "${C_CYAN}${C_BOLD}Web 管理端配置${C_RESET}"
    prompt_read domain "请输入访问域名（默认 $domain）: " "$domain"
    domain="${domain:-sharq.eu.org}"
    prompt_read admin_user "请输入管理账号（默认 $admin_user）: " "$admin_user"
    admin_user="${admin_user:-admin}"
    prompt_read admin_pass "请输入登录密码: " ""

    if [ -z "$admin_pass" ]; then
        echo "❌ 密码不能为空。"
        return 1
    fi

    if [ ! -x "$VENV_DIR/bin/python" ]; then
        echo "❌ 未检测到虚拟环境 Python：$VENV_DIR/bin/python，请先执行菜单 3。"
        return 1
    fi

    # 生成随机 SECRET_KEY（保留，用于会话加密）
    local secret_key
    secret_key="$($VENV_DIR/bin/python - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)"

    # 写入配置：直接存储明文密码（字段名改为 password）
    ADMIN_USER="$admin_user" \
    PASSWORD_PLAIN="$admin_pass" \
    SECRET_KEY="$secret_key" \
    DOMAIN_NAME="$domain" \
    WEB_CONF_FILE="$WEB_CONF_FILE" \
    $VENV_DIR/bin/python - <<'PY'
import json
import os
from pathlib import Path

cfg = {
    "app_name": "闲云量化",
    "admin_username": os.environ["ADMIN_USER"],
    "password": os.environ["PASSWORD_PLAIN"],      # 明文保存
    "secret_key": os.environ["SECRET_KEY"],
    "domain": os.environ["DOMAIN_NAME"],
    "public_port": 2096,
    "internal_port": 2097,
}
Path(os.environ["WEB_CONF_FILE"]).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
PY

    if ! prepare_web_cert_for_domain "$domain"; then
        echo "❌ Web 管理端证书准备失败，已停止配置。"
        return 1
    fi
    cert_info="$(get_cert_paths "$domain")" || return 1
    cert_name="$(echo "$cert_info" | cut -d'|' -f1)"
    cert_file="$(echo "$cert_info" | cut -d'|' -f2)"
    key_file="$(echo "$cert_info" | cut -d'|' -f3)"

    # 为 nginx 配置设置环境变量
    export QUANT_DOMAIN="$domain"
    export QUANT_CERT_FILE="$cert_file"
    export QUANT_KEY_FILE="$key_file"

    echo "检查 Web 程序与模板目录..."
    if [ ! -d "$DCF_DIR/web_templates" ] || [ ! -d "$DCF_DIR/web_static" ]; then
        echo "❌ 缺少 web_templates 或 web_static 目录，请确认已复制到 $DCF_DIR"
        return 1
    fi
    if ! (cd "$DCF_DIR" && "$VENV_DIR/bin/python" -c "import quant_web; print('quant_web import ok')") ; then
        echo "❌ quant_web.py 导入失败，请检查依赖或文件内容。"
        return 1
    fi

    # 创建 systemd 服务（使用新端口 2096）
    ${SUDO} tee "$WEB_SERVICE_FILE" >/dev/null <<SERVICE
[Unit]
Description=Quant Web Portal
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$DCF_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/python -m gunicorn -w 2 -b 127.0.0.1:$WEB_INTERNAL_PORT quant_web:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICE

    # 配置 nginx（监听 2096 端口）
    ${SUDO} tee "$WEB_NGINX_SITE" >/dev/null <<EOF
server {
    listen $WEB_PUBLIC_PORT ssl;
    listen [::]:$WEB_PUBLIC_PORT ssl;
    server_name $domain;

    ssl_certificate     $cert_file;
    ssl_certificate_key $key_file;
    ssl_session_timeout 1d;
    ssl_session_cache shared:SSL:10m;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;

    client_max_body_size 10m;

    location / {
        proxy_pass http://127.0.0.1:$WEB_INTERNAL_PORT;
        proxy_set_header Host \\\$host;
        proxy_set_header X-Real-IP \\\$remote_addr;
        proxy_set_header X-Forwarded-For \\\$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 60;
    }
}

server {
    listen 80;
    listen [::]:80;
    server_name $domain;
    return 301 https://\\\$server_name:$WEB_PUBLIC_PORT\\\$request_uri;
}
EOF

    ${SUDO} ln -sf "$WEB_NGINX_SITE" "$WEB_NGINX_LINK"
    ${SUDO} nginx -t || return 1
    ${SUDO} systemctl daemon-reload
    ${SUDO} systemctl enable quant-web >/dev/null 2>&1 || true
    ${SUDO} systemctl restart quant-web

    sleep 2
    if ! ${SUDO} systemctl is-active --quiet quant-web; then
        echo "❌ quant-web 服务启动失败，最近日志如下："
        ${SUDO} systemctl --no-pager --full status quant-web || true
        echo "----------------------------------------"
        ${SUDO} journalctl -u quant-web -n 50 --no-pager || true
        return 1
    fi

    if ! curl -ksS "http://127.0.0.1:$WEB_INTERNAL_PORT/login" >/dev/null 2>&1; then
        echo "❌ 后端服务未正常响应 http://127.0.0.1:$WEB_INTERNAL_PORT/login"
        ${SUDO} journalctl -u quant-web -n 50 --no-pager || true
        return 1
    fi

    ${SUDO} systemctl reload nginx

    echo
    echo "✅ Web 管理端已配置完成"
    echo "访问地址: https://$domain:$WEB_PUBLIC_PORT/login"
    echo "证书来源: Let's Encrypt (/etc/letsencrypt/live/${cert_name})"
    echo "⚠️  密码已明文保存，请务必同步修改 quant_web.py 中的登录验证逻辑（改为直接比对明文）"
}


restart_web_portal() {
    echo "=============== 重启 Web 管理端 ==============="
    if [ ! -f "$DCF_DIR/quant_web.py" ]; then
        echo "❌ 未找到 $DCF_DIR/quant_web.py"
        return 1
    fi
    if [ ! -x "$VENV_DIR/bin/python" ]; then
        echo "❌ 未找到虚拟环境 Python：$VENV_DIR/bin/python"
        echo "   请先执行：菜单 1) 下载/更新项目与依赖"
        return 1
    fi

    echo "检查 nginx 配置..."
    if ! ${SUDO} nginx -t; then
        echo "❌ nginx 配置检查失败，已取消重启网页端。"
        return 1
    fi

    if ! ${SUDO} systemctl list-unit-files quant-web.service >/dev/null 2>&1; then
        echo "❌ quant-web 服务未安装。"
        echo "   请先执行：菜单 5) 配置/安装网页端"
        return 1
    fi

    echo "重启 quant-web 服务..."
    ${SUDO} systemctl restart quant-web
    sleep 1

    if ! ${SUDO} systemctl is-active --quiet quant-web; then
        echo "❌ quant-web 服务启动失败，最近日志如下："
        ${SUDO} systemctl --no-pager --full status quant-web || true
        echo "--------------------------------------------"
        ${SUDO} journalctl -u quant-web -n 80 --no-pager || true
        return 1
    fi

    echo "重新加载 nginx..."
    ${SUDO} systemctl reload nginx || ${SUDO} systemctl restart nginx
    echo "✅ Web 管理端已重启。"
    echo "--------------------------------------------"
    web_portal_status
}

web_portal_status() {
    echo "=============== Web 管理端状态 ==============="
    if [ -f "$WEB_CONF_FILE" ]; then
        cat "$WEB_CONF_FILE"
    else
        echo "web_portal.json 未配置"
    fi
    echo "--------------------------------------------"
    ${SUDO} systemctl status quant-web --no-pager -n 5 2>/dev/null || echo "quant-web 服务未安装"
    echo "--------------------------------------------"
    ${SUDO} nginx -t 2>/dev/null || true
}

uninstall_quant() {
    echo -e "${C_RED}========== 卸载 Quant 程序 ==========${C_RESET}"
    echo -e "${C_YELLOW}警告：此操作将删除以下内容：${C_RESET}"
    echo "  - $DCF_DIR 目录（所有程序文件、配置、日志）"
    echo "  - /etc/systemd/system/quant-web.service"
    echo "  - /etc/nginx/sites-available/quant-web-*.conf"
    echo "  - /etc/nginx/sites-enabled/quant-web-*.conf"
    echo "  - crontab 中的定时任务"
    echo ""
    read -p "确认卸载？(yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        echo "已取消卸载。"
        return 0
    fi

    echo "正在停止服务..."
    ${SUDO} systemctl stop quant-web 2>/dev/null || true
    ${SUDO} systemctl stop quant 2>/dev/null || true

    echo "正在删除 systemd 服务..."
    ${SUDO} rm -f /etc/systemd/system/quant-web.service
    ${SUDO} rm -f /etc/systemd/system/quant.service
    ${SUDO} systemctl daemon-reload 2>/dev/null || true

    echo "正在删除 nginx 配置..."
    ${SUDO} rm -f /etc/nginx/sites-available/quant-web-*.conf
    ${SUDO} rm -f /etc/nginx/sites-enabled/quant-web-*.conf
    ${SUDO} nginx -t >/dev/null 2>&1 && ${SUDO} systemctl reload nginx 2>/dev/null || true

    echo "正在删除 crontab 任务..."
    crontab -l 2>/dev/null | grep -v "quant.sh --cron-check" | crontab - 2>/dev/null || true

    echo "正在删除程序目录..."
    ${SUDO} rm -rf "$DCF_DIR"

    echo ""
    echo -e "${C_GREEN}✅ Quant 程序已卸载完毕${C_RESET}"
    echo "如需完全清理，还需手动删除："
    echo "  - /etc/letsencrypt/live/ 中的证书（可选）"
    echo "  - $SCRIPT_DIR/quant.sh 脚本本身（可选）"
}


show_menu() {
    echo -e "${C_CYAN}===============================${C_RESET}"
    echo -e "${C_BOLD}${C_GREEN}  Quant 网格监控 管理菜单${C_RESET}"
    echo -e "${C_DIM} （管理脚本目录：$SCRIPT_DIR）${C_RESET}"
    echo -e "${C_DIM} （运行文件目录：$DCF_DIR）${C_RESET}"
    echo -e "${C_CYAN}===============================${C_RESET}"
    echo -e "${C_YELLOW}1)${C_RESET} 下载/更新项目与依赖"
    echo -e "${C_GREEN}2)${C_RESET} 启动脚本"
    echo -e "${C_RED}3)${C_RESET} 停止脚本"
    echo -e "${C_CYAN}4)${C_RESET} 查看运行状态"
    echo -e "${C_YELLOW}5)${C_RESET} 配置/安装网页端"
    echo -e "${C_YELLOW}6)${C_RESET} 重启网页端"
    echo -e "${C_CYAN}7)${C_RESET} 查看网页端状态"
    echo -e "${C_CYAN}8)${C_RESET} 检查项目文件"
    echo -e "${C_GREEN}9)${C_RESET} 域名设置（配置 HTTPS）"
    echo -e "${C_RED}10)${C_RESET} 卸载程序"
    echo -e "${C_RED}0)${C_RESET} 退出"
    echo -e "${C_CYAN}===============================${C_RESET}"
}

# ========= 主循环 =========
while true; do
    show_menu
    echo -ne "${C_BOLD}请选择操作: ${C_RESET}"
    read -r choice
case "$choice" in
    1) update_rely ;;
    2) start_quant ;;
    3) stop_quant ;;
    4) show_status ;;
    5) configure_web_portal ;;
    6) restart_web_portal ;;
    7) web_portal_status ;;
    8) self_check_project_files ;;
    9) setup_domain_https ;;
    10) uninstall_quant ;;
    0) exit 0 ;;
    *) echo "无效选项，请重新输入。" ;;
esac
done


setup_domain_https() {
    echo -e "${C_CYAN}========== 域名与 HTTPS 设置 ==========${C_RESET}"
    
    # 安全处理未定义变量（set -u 环境下）
    local DOMAIN=""
    local PUBLIC_PORT=""
    local INTERNAL_PORT=""
    
    # 读取当前配置
    local cfg_file="$DCF_DIR/web_portal.json"
    if [[ -f "$cfg_file" ]]; then
        DOMAIN=$($VENV_DIR/bin/python -c "import json; print(json.load(open('$cfg_file')).get('domain', ''))" 2>/dev/null || echo "")
        PUBLIC_PORT=$($VENV_DIR/bin/python -c "import json; print(json.load(open('$cfg_file')).get('public_port', ''))" 2>/dev/null || echo "")
        INTERNAL_PORT=$($VENV_DIR/bin/python -c "import json; print(json.load(open('$cfg_file')).get('internal_port', ''))" 2>/dev/null || echo "")
    fi
    
    if [[ -z "$DOMAIN" ]]; then
        read -p "请输入域名（例如 xiany.de）: " DOMAIN
    else
        read -p "当前域名: $DOMAIN，确认请直接回车，修改请输入新域名: " new_domain
        [[ -n "$new_domain" ]] && DOMAIN="$new_domain"
    fi
    
    if [[ -z "$PUBLIC_PORT" ]]; then
        read -p "请输入对外公开端口（默认 819）: " PUBLIC_PORT
        PUBLIC_PORT=${PUBLIC_PORT:-819}
    else
        read -p "当前公开端口: $PUBLIC_PORT，确认直接回车，修改请输入新端口: " new_port
        [[ -n "$new_port" ]] && PUBLIC_PORT="$new_port"
    fi
    
    if [[ -z "$INTERNAL_PORT" ]]; then
        INTERNAL_PORT=1819
    fi
    
    echo -e "${C_GREEN}将配置域名: $DOMAIN，HTTPS 端口: $PUBLIC_PORT，后端端口: $INTERNAL_PORT${C_RESET}"
    
    # 检查 certbot
    if ! command -v certbot &>/dev/null; then
        echo -e "${C_YELLOW}正在安装 Certbot...${C_RESET}"
        apt update && apt install -y certbot python3-certbot-nginx
        if [[ $? -ne 0 ]]; then
            echo -e "${C_RED}Certbot 安装失败，请手动安装后重试。${C_RESET}"
            return 1
        fi
    fi
    
    # 临时配置 80 端口（用于验证）
    local temp_nginx_conf="/etc/nginx/sites-available/temp_${DOMAIN}"
    local temp_enabled="/etc/nginx/sites-enabled/temp_${DOMAIN}"
    local need_cleanup=0
    
    if ! nginx -T 2>/dev/null | grep -q "server_name.*$DOMAIN"; then
        echo -e "${C_YELLOW}当前 Nginx 未配置域名 $DOMAIN 的 HTTP 访问，将临时添加配置用于证书验证...${C_RESET}"
        cat > "$temp_nginx_conf" <<EOF_TEMP
server {
    listen 80;
    server_name $DOMAIN;
    root /var/www/html;
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }
}
NGINX_TEMP
        ln -sf "$temp_nginx_conf" "$temp_enabled"
        nginx -t && systemctl reload nginx
        need_cleanup=1
    fi
    
    # 申请证书
    echo -e "${C_GREEN}正在申请 SSL 证书...${C_RESET}"
    mkdir -p /var/www/html/.well-known/acme-challenge
    certbot certonly --webroot -w /var/www/html -d "$DOMAIN" \
        --non-interactive --agree-tos --register-unsafely-without-email \
        --keep-until-expiring
    if [[ $? -ne 0 ]]; then
        echo -e "${C_RED}证书申请失败，请检查域名解析是否正确（需指向本服务器 IP）以及 80 端口是否可访问。${C_RESET}"
        [[ $need_cleanup -eq 1 ]] && rm -f "$temp_enabled" "$temp_nginx_conf" && systemctl reload nginx
        return 1
    fi
    
    # 清理临时配置
    if [[ $need_cleanup -eq 1 ]]; then
        rm -f "$temp_enabled" "$temp_nginx_conf"
        systemctl reload nginx
    fi
    
    # 证书路径
    local cert_path="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
    local key_path="/etc/letsencrypt/live/$DOMAIN/privkey.pem"
    if [[ ! -f "$cert_path" ]]; then
        echo -e "${C_RED}证书文件未找到，配置中止。${C_RESET}"
        return 1
    fi
    
    # 配置 Nginx HTTPS
    local nginx_conf="/etc/nginx/sites-available/quant-${DOMAIN}-ssl"
    local nginx_enabled="/etc/nginx/sites-enabled/quant-${DOMAIN}-ssl"
    
    cat > "$nginx_conf" <<EOF_SSL
# HTTPS server block for $DOMAIN (port $PUBLIC_PORT)
server {
    listen $PUBLIC_PORT ssl;
    server_name $DOMAIN;

    ssl_certificate $cert_path;
    ssl_certificate_key $key_path;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    location / {
        proxy_pass http://127.0.0.1:$INTERNAL_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}

# HTTP 跳转到 HTTPS
server {
    listen 80;
    server_name $DOMAIN;
    return 301 https://\$server_name:$PUBLIC_PORT\$request_uri;
}
NGINX_SSL

    if [[ "$PUBLIC_PORT" != "443" ]]; then
        echo -e "${C_YELLOW}提示：您设置的 HTTPS 端口是 $PUBLIC_PORT，访问请使用 https://$DOMAIN:$PUBLIC_PORT${C_RESET}"
        read -p "是否同时配置标准 443 端口（需要 443 未占用）？(y/N): " add_443
        if [[ "$add_443" =~ ^[Yy]$ ]]; then
            cat >> "$nginx_conf" <<EOF_443
# Standard 443 port fallback
server {
    listen 443 ssl;
    server_name $DOMAIN;
    ssl_certificate $cert_path;
    ssl_certificate_key $key_path;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
    location / {
        proxy_pass http://127.0.0.1:$INTERNAL_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINX_443
        fi
    fi
    
    ln -sf "$nginx_conf" "$nginx_enabled"
    nginx -t
    if [[ $? -ne 0 ]]; then
        echo -e "${C_RED}Nginx 配置文件语法错误，已取消启用。请手动检查 $nginx_conf${C_RESET}"
        rm -f "$nginx_enabled"
        return 1
    fi
    
    systemctl reload nginx
    echo -e "${C_GREEN}✅ HTTPS 配置成功！${C_RESET}"
    echo -e "访问地址：${C_BOLD}https://$DOMAIN:$PUBLIC_PORT${C_RESET}"
    echo -e "证书自动续期任务已由 Certbot 定时服务管理（检查：systemctl status certbot.timer）"
}
