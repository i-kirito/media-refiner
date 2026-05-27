#!/bin/bash
# deploy.sh — Media Refiner 全量部署脚本
# 功能：备份 .env → 构建镜像 → 传输到 NAS → docker-compose 部署 → 验证 .env
# Usage:
#   ./deploy.sh              全量构建部署
#   ./deploy.sh quick [file]  热更新单个文件到运行容器（不重启）
#   ./deploy.sh env-save      仅备份 .env
#   ./deploy.sh env-restore   仅恢复 .env
#   ./deploy.sh env-verify    仅验证 .env 完整性
set -euo pipefail

NAS_HOST="nas"
REMOTE_BASE="/volume2/docker/media-refiner"
LOCAL_ENV="config/.env"
REMOTE_ENV="${REMOTE_BASE}/config/.env"
BACKUP_DIR="config/env_backups"

# 预期必需的环境变量键（不含 REFINER_ 前缀）
REQUIRED_KEYS=(
    "EMBY_HOST"
    "EMBY_API_KEY"
    "CLOUD115_COOKIE"
    "CLOUD115_FOLDER_ID"
    "PROXY"
    "TG_BOT_TOKEN"
    "TG_CHAT_ID"
    "TG_NOTIFY_EVENTS"
)

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${BLUE}ℹ${NC} $1"; }
ok()    { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}⚠${NC} $1"; }
err()   { echo -e "${RED}✗${NC} $1"; }

# ═══════════════════════════════════════
#   .env 备份 / 恢复 / 验证
# ═══════════════════════════════════════

env_save() {
    info "从 NAS 备份 .env ..."
    if ssh "$NAS_HOST" "test -f $REMOTE_ENV" 2>/dev/null; then
        mkdir -p "$BACKUP_DIR"
        local backup_file="${BACKUP_DIR}/.env.backup.$(date +%Y%m%d_%H%M%S)"
        ssh "$NAS_HOST" "cat $REMOTE_ENV" > "$backup_file"
        cp "$backup_file" "$LOCAL_ENV" 2>/dev/null || true
        ok "已备份: $backup_file"
        ok "已同步到本地: $LOCAL_ENV"
    else
        warn "NAS 上无 .env"
        if [ -f "$LOCAL_ENV" ]; then
            warn "但本地有 $LOCAL_ENV"
        fi
    fi
}

env_restore() {
    info "恢复 .env 到 NAS ..."
    if [ -f "$LOCAL_ENV" ]; then
        cat "$LOCAL_ENV" | ssh "$NAS_HOST" "cat > $REMOTE_ENV"
        ok "已恢复 $REMOTE_ENV"
    else
        local latest=$(ls -t "$BACKUP_DIR"/.env.backup.* 2>/dev/null | head -1)
        if [ -n "$latest" ]; then
            warn "本地 $LOCAL_ENV 不存在，从备份恢复: $latest"
            cp "$latest" "$LOCAL_ENV"
            cat "$LOCAL_ENV" | ssh "$NAS_HOST" "cat > $REMOTE_ENV"
            ok "已从备份恢复"
        else
            err "无可用的备份或本地副本！请手动创建 $LOCAL_ENV"
            return 1
        fi
    fi
}

env_verify() {
    local missing=0
    local empty=0
    local env_content=""
    local source_label=""

    if ssh "$NAS_HOST" "test -f $REMOTE_ENV" 2>/dev/null; then
        env_content=$(ssh "$NAS_HOST" "cat $REMOTE_ENV")
        source_label="NAS ($REMOTE_ENV)"
    elif [ -f "$LOCAL_ENV" ]; then
        env_content=$(cat "$LOCAL_ENV")
        source_label="本地 ($LOCAL_ENV)"
        warn "⚠ NAS 上无 .env，检查本地副本"
    else
        err "❌ NAS 和本地都找不到 .env！"
        return 1
    fi

    echo ""
    echo "━━━ .env 配置完整性检查 ━━━"
    echo "来源: $source_label"
    echo ""

    for key in "${REQUIRED_KEYS[@]}"; do
        local refiner_key="REFINER_${key}"
        local line=$(echo "$env_content" | grep "^${refiner_key}=" | head -1)
        if [ -z "$line" ]; then
            err "✗ $refiner_key 缺失！"
            missing=$((missing + 1))
        else
            local value=$(echo "$line" | cut -d'=' -f2-)
            if [ -z "$value" ] || [ "$value" = "your_emby_api_key" ]; then
                warn "⚠ $refiner_key 未配置（空值/模板值）"
                empty=$((empty + 1))
            else
                local masked="${value:0:4}••••"
                ok "✓ $refiner_key → $masked"
            fi
        fi
    done

    echo ""
    if [ $missing -eq 0 ] && [ $empty -eq 0 ]; then
        ok "✅ 所有配置完整"
    elif [ $missing -eq 0 ]; then
        warn "⚠ 配置完整但有 $empty 个可选键未填"
    else
        err "❌ 缺失 $missing 个必需键！"
        return 1
    fi
}

# ═══════════════════════════════════════
#   快速热更新（复制文件到运行容器）
# ═══════════════════════════════════════

quick_deploy() {
    local files=("$@")
    if [ ${#files[@]} -eq 0 ]; then
        warn "请指定要更新的文件路径"
        echo "Usage: ./deploy.sh quick app/routers/scan.py"
        return 1
    fi

    for raw in "${files[@]}"; do
        local rel
        rel=$(echo "$raw" | sed -n 's|.*/media-refiner/||p')
        [ -z "$rel" ] && rel="$raw"

        if [ ! -f "$rel" ]; then
            warn "本地文件不存在: $rel，跳过"
            continue
        fi

        info "热更新: $rel"
        cat "$rel" | ssh "$NAS_HOST" "docker exec -i media-refiner bash -c 'cat > /workspace/$rel'"
        ok "已复制: $rel"
    done

    # Python 文件需要重启
    local needs_restart=false
    for raw in "${files[@]}"; do
        local rel
        rel=$(echo "$raw" | sed -n 's|.*/media-refiner/||p')
        [ -z "$rel" ] && rel="$raw"
        case "$rel" in
            *.py) needs_restart=true; break ;;
        esac
    done

    if [ "$needs_restart" = true ]; then
        info "Python 文件变更，提交镜像并重启..."
        ssh "$NAS_HOST" "docker commit --change='CMD [\"uvicorn\", \"app.main:app\", \"--host\", \"0.0.0.0\", \"--port\", \"10308\"]' media-refiner media-refiner:latest"
        ok "镜像已提交"
        ssh "$NAS_HOST" "cd $REMOTE_BASE && docker compose down && docker compose up -d"
        ok "容器已重启"
    else
        info "静态文件变更，已实时生效"
    fi
}

# ═══════════════════════════════════════
#   全量构建部署
# ═══════════════════════════════════════

full_deploy() {
    echo ""
    echo "╔════════════════════════════════════╗"
    echo "║   Media Refiner 全量部署           ║"
    echo "╚════════════════════════════════════╝"
    echo ""

    # 1) 备份 .env
    echo "── Step 1/5: 备份 .env ──"
    env_save

    # 2) 构建镜像
    echo ""
    echo "── Step 2/5: 构建 Docker 镜像 (linux/amd64) ──"
    docker build -t media-refiner:latest --platform linux/amd64 .
    ok "镜像构建完成"

    # 3) 传输到 NAS
    echo ""
    echo "── Step 3/5: 传输镜像到 NAS ──"
    docker save media-refiner:latest | ssh "$NAS_HOST" "docker load"
    ok "镜像传输完成"

    # 4) 部署
    echo ""
    echo "── Step 4/5: docker-compose 部署 ──"
    ssh "$NAS_HOST" "cd $REMOTE_BASE && docker compose down && docker compose up -d"
    sleep 3
    local status
    status=$(ssh "$NAS_HOST" "cd $REMOTE_BASE && docker compose ps --format '{{.Status}}'" 2>/dev/null || echo "unknown")
    ok "容器状态: $status"

    # 5) 验证 .env
    echo ""
    echo "── Step 5/5: 验证 .env ──"
    if ssh "$NAS_HOST" "test -f $REMOTE_ENV" 2>/dev/null; then
        ok ".env 存在于 NAS"
        env_verify || warn "部分配置待补充"
    else
        err ".env 在 NAS 上丢失！正在恢复..."
        env_restore && ok ".env 已恢复" || err ".env 恢复失败！请手动执行 ./deploy.sh env-restore"
    fi

    echo ""
    ok "🎉 部署完成！"
    echo "   服务地址: http://192.168.31.213:10309"
    echo ""
}

# ═══════════════════════════════════════
#   Main
# ═══════════════════════════════════════

chmod +x "$0"

case "${1:-}" in
    quick)
        shift
        quick_deploy "$@"
        ;;
    env-save)
        env_save
        ;;
    env-restore)
        env_restore
        ;;
    env-verify)
        env_verify
        ;;
    ""|full)
        full_deploy
        ;;
    *)
        echo "Media Refiner 部署脚本"
        echo ""
        echo "用法:"
        echo "  ./deploy.sh             全量构建部署（默认）"
        echo "  ./deploy.sh full        同上"
        echo "  ./deploy.sh quick <文件> 热更新单个文件到运行容器"
        echo "  ./deploy.sh env-save    仅备份 .env 到本地"
        echo "  ./deploy.sh env-restore 仅从本地恢复 .env 到 NAS"
        echo "  ./deploy.sh env-verify  仅检查 .env 完整性"
        exit 1
        ;;
esac
