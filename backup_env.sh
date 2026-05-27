#!/bin/bash
# backup_env.sh — 备份/恢复 Media Refiner .env 配置
# Usage:
#   ./backup_env.sh save     从 NAS 备份 .env 到本地
#   ./backup_env.sh restore  从本地恢复到 NAS
#   ./backup_env.sh verify   验证 NAS 和本地配置是否完整
set -euo pipefail

NAS_HOST="nas"
REMOTE_BASE="/volume2/docker/media-refiner"
LOCAL_ENV="config/.env"
REMOTE_ENV="${REMOTE_BASE}/config/.env"
BACKUP_DIR="config/env_backups"

# 预期的环境变量键（不含 REFINER_ 前缀）
REQUIRED_KEYS=(
    "EMBY_HOST"
    "EMBY_API_KEY"
    "MOVIEPILOT_URL"
    "MOVIEPILOT_TOKEN"
    "HDHIVE_API_KEY"
    "CLOUD115_COOKIE"
    "CLOUD115_FOLDER_ID"
    "EXCLUDE_LIBRARY_IDS"
    "SCAN_SCHEDULE"
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

save_env() {
    info "从 NAS 备份 .env ..."
    if ssh "$NAS_HOST" "test -f $REMOTE_ENV" 2>/dev/null; then
        mkdir -p "$BACKUP_DIR"
        local backup_file="${BACKUP_DIR}/.env.backup.$(date +%Y%m%d_%H%M%S)"
        ssh "$NAS_HOST" "cat $REMOTE_ENV" > "$backup_file"
        # 同时也覆盖本地 config/.env 作为当前工作副本
        cp "$backup_file" "$LOCAL_ENV"
        ok "已备份到 $backup_file"
        ok "已同步到本地 $LOCAL_ENV"
    else
        warn "NAS 上不存在 $REMOTE_ENV，跳过备份"
        if [ -f "$LOCAL_ENV" ]; then
            warn "但本地有 $LOCAL_ENV，可以 restore"
        fi
    fi
}

restore_env() {
    info "从本地恢复 .env 到 NAS ..."
    if [ -f "$LOCAL_ENV" ]; then
        cat "$LOCAL_ENV" | ssh "$NAS_HOST" "cat > $REMOTE_ENV"
        ok "已恢复 $REMOTE_ENV"
    else
        err "本地 $LOCAL_ENV 不存在，无法恢复！"
        # 尝试找最近的备份
        local latest=$(ls -t "$BACKUP_DIR"/.env.backup.* 2>/dev/null | head -1)
        if [ -n "$latest" ]; then
            warn "找到备份: $latest，尝试恢复..."
            cp "$latest" "$LOCAL_ENV"
            cat "$LOCAL_ENV" | ssh "$NAS_HOST" "cat > $REMOTE_ENV"
            ok "已从备份恢复 $REMOTE_ENV"
        else
            err "无可用备份，请手动创建 $LOCAL_ENV（参考 config/.env.example）"
            return 1
        fi
    fi
}

verify_env() {
    local missing=0
    local env_file=""
    local source_label=""

    # 优先检查 NAS 上的 .env
    if ssh "$NAS_HOST" "test -f $REMOTE_ENV" 2>/dev/null; then
        env_file=$(ssh "$NAS_HOST" "cat $REMOTE_ENV")
        source_label="NAS"
    elif [ -f "$LOCAL_ENV" ]; then
        env_file=$(cat "$LOCAL_ENV")
        source_label="本地"
        warn "NAS 上无 .env，检查本地副本"
    else
        err "NAS 和本地都找不到 .env！"
        return 1
    fi

    echo ""
    echo "━━━ .env 配置完整性检查 ━━━"
    echo "来源: $source_label"
    echo ""

    for key in "${REQUIRED_KEYS[@]}"; do
                local refiner_key="REFINER_${key}"
        local line=$(echo "$env_file" | grep "^${refiner_key}=" | head -1)
        if [ -z "$line" ]; then
            err "$refiner_key  → 缺失！"
            missing=$((missing + 1))
        else
            local value=$(echo "$line" | cut -d'=' -f2-)
            if [ -z "$value" ] || [ "$value" = "your_emby_api_key" ]; then
                warn "$refiner_key  → 未配置（空值/模板值）"
            else
                local masked="${value:0:4}••••"
                ok "$refiner_key  → $masked"
            fi
        fi
    done

    echo ""
    if [ $missing -eq 0 ]; then
        ok "所有必需键均存在"
    else
        err "缺失 $missing 个键"
        return 1
    fi
}

# ─── Main ───

case "${1:-save}" in
    save)
        save_env
        ;;
    restore)
        restore_env
        ;;
    verify)
        verify_env
        ;;
    *)
        echo "Usage: $0 {save|restore|verify}"
        echo ""
        echo "  save    从 NAS 备份 .env 到本地（默认）"
        echo "  restore 从本地恢复到 NAS"
        echo "  verify  检查 .env 配置完整性"
        exit 1
        ;;
esac
