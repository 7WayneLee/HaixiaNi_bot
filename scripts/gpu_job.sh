#!/usr/bin/env bash
# 在 GPU VM 上以 systemd 服務跑長時間工作；SSH 斷線或 tmux 被殺掉都不影響。
set -euo pipefail

work_dir=${HAIXIA_WORK:-$HOME/haixia-work}
env_file="$work_dir/env.sh"

usage() {
    cat <<'EOF'
用法：
  bash scripts/gpu_job.sh start <名稱> -- <指令…>
  bash scripts/gpu_job.sh status <名稱>
  bash scripts/gpu_job.sh log <名稱> [行數]
  bash scripts/gpu_job.sh stop <名稱>

start   以 systemd 服務 haixia-<名稱>.service 執行指令，工作目錄是執行 start 時的目錄。
        指令前會先 source ~/haixia-work/env.sh（setup_gpu.sh 產生）。輸出寫到
        ~/haixia-work/<名稱>.log（舊的 log 改名為 <名稱>.log.prev），結束碼寫到
        ~/haixia-work/<名稱>.exit。同名服務正在跑時拒絕啟動。
        要串接多個指令時，寫成 bash -c '指令一 && 指令二'。
status  印一行：
          DONE exit=N                      工作已結束，N 是結束碼（0 表示成功）
          RUN active=<狀態> age=<秒>s fail=<行數>
                                           還沒寫出結束碼。active 是服務狀態，age 是
                                           log 多久沒更新，fail 是 log 裡含「失敗」的行數。
                                           active 不是 active 就表示工作已經不在了
                                           （被停止、被殺掉或 VM 重開機），要查 log 後重跑。
log     印 log 最後幾行（預設 40 行），\r 換成換行。
stop    停止服務（會殺掉整個工作的所有子程序）。

名稱只能用英文字母、數字、底線、點和連字號，開頭必須是英文字母或數字。
EOF
}

die() {
    printf '%s\n' "$1" >&2
    exit "${2:-1}"
}

check_name() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "名稱不合法：$1（只能用英文字母、數字、底線、點和連字號）" 2
}

unit_state() {
    systemctl show -p ActiveState --value "$1" 2>/dev/null || printf 'unknown'
}

file_mtime() {
    stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"
}

start_job() {
    local name=$1
    shift
    [[ "${1:-}" == -- ]] || die '用法：bash scripts/gpu_job.sh start <名稱> -- <指令…>' 2
    shift
    (( $# > 0 )) || die '缺少要執行的指令。' 2
    [[ -f "$env_file" ]] || die "找不到 ${env_file}；請先跑 scripts/setup_gpu.sh。"

    local unit="haixia-$name.service" log="$work_dir/$name.log" exit_file="$work_dir/$name.exit"
    local state
    state=$(unit_state "$unit")
    case "$state" in
        active|activating|deactivating|reloading|refreshing)
            die "$unit 正在執行（${state}），不重複啟動。要重跑請先 bash scripts/gpu_job.sh stop ${name}。"
            ;;
    esac
    # 上次被停止或被殺掉的服務會留在 failed 狀態，同名 unit 無法再建立。
    sudo systemctl reset-failed "$unit" >/dev/null 2>&1 || true

    mkdir -p "$work_dir"
    rm -f "$exit_file"
    if [[ -e "$log" ]]; then mv -f "$log" "$log.prev"; fi
    {
        printf '開始：%s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
        printf '工作目錄：%s\n' "$PWD"
        printf '指令：%s\n' "$*"
    } > "$log"

    local command_text script
    printf -v command_text '%q ' "$@"
    printf -v script '{ source %q && %s; } >>%q 2>&1; code=$?; printf %q "$(date %q)" "$code" >>%q; echo "$code" >%q' \
        "$env_file" "$command_text" "$log" '結束：%s exit=%s\n' '+%Y-%m-%d %H:%M:%S' "$log" "$exit_file"

    if ! sudo systemd-run --unit="$unit" \
        --uid="$(id -u)" --gid="$(id -g)" \
        --setenv=HOME="$HOME" --setenv=USER="$(id -un)" --setenv=PATH="$PATH" \
        --setenv=LANG="${LANG:-C.UTF-8}" --setenv=PYTHONUNBUFFERED=1 \
        --working-directory="$PWD" \
        --property=KillMode=control-group \
        /bin/bash -c "$script"; then
        printf 'systemd-run 啟動失敗：%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$log"
        die "$unit 啟動失敗；見上方 systemd-run 的錯誤訊息。"
    fi
    printf '已啟動 %s。\n看狀態：bash scripts/gpu_job.sh status %s\n看 log：bash scripts/gpu_job.sh log %s\n' "$unit" "$name" "$name"
}

status_job() {
    local name=$1
    local unit="haixia-$name.service" log="$work_dir/$name.log" exit_file="$work_dir/$name.exit"
    if [[ -f "$exit_file" ]]; then
        printf 'DONE exit=%s\n' "$(tr -d '[:space:]' < "$exit_file")"
        return 0
    fi
    local state age=- fail=0
    state=$(unit_state "$unit")
    if [[ ! -e "$log" ]]; then
        [[ "$state" == active ]] || die "找不到工作 ${name}：沒有 ${log}，也沒有 ${exit_file}。"
    else
        age=$(( $(date +%s) - $(file_mtime "$log") ))
        fail=$(grep -c -- '失敗' "$log" || true)
    fi
    printf 'RUN active=%s age=%ss fail=%s\n' "$state" "$age" "${fail:-0}"
}

log_job() {
    local name=$1 lines=${2:-40}
    local log="$work_dir/$name.log"
    [[ "$lines" =~ ^[1-9][0-9]*$ ]] || die "行數必須是正整數：$lines" 2
    [[ -f "$log" ]] || die "找不到 ${log}。"
    # 進度列用 \r 覆寫同一行；只讀檔尾，避免 log 很大時整檔轉換。
    tail -c 4000000 "$log" | tr -s '\r' '\n' | tail -n "$lines"
}

stop_job() {
    local name=$1
    local unit="haixia-$name.service" log="$work_dir/$name.log"
    local state
    state=$(unit_state "$unit")
    case "$state" in
        active|activating|reloading|refreshing) ;;
        *) die "$unit 沒有在執行（${state}）。" ;;
    esac
    sudo systemctl stop "$unit"
    if [[ -f "$log" ]]; then
        printf '手動停止：%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$log"
    fi
    printf '已停止 %s。\n' "$unit"
}

case "${1:-}" in -h|--help|help) usage; exit 0 ;; esac
(( $# >= 2 )) || { usage >&2; exit 2; }
subcommand=$1
name=$2
shift 2
check_name "$name"
case "$subcommand" in
    start) start_job "$name" "$@" ;;
    status) (( $# == 0 )) || die '用法：bash scripts/gpu_job.sh status <名稱>' 2; status_job "$name" ;;
    log) (( $# <= 1 )) || die '用法：bash scripts/gpu_job.sh log <名稱> [行數]' 2; log_job "$name" "$@" ;;
    stop) (( $# == 0 )) || die '用法：bash scripts/gpu_job.sh stop <名稱>' 2; stop_job "$name" ;;
    *) usage >&2; exit 2 ;;
esac
