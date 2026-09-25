#!/usr/bin/env bash
# 在 movie-nas 執行；預設只顯示指令，--yes 才會呼叫 gcloud。
set -euo pipefail

NAME=${NAME:-haixia-gpu}
PROJECT=${PROJECT:-vmdemo1-507014}
MACHINE=${MACHINE:-g2-standard-4}
IMAGE_FAMILY=${IMAGE_FAMILY:-common-cu129-ubuntu-2404-nvidia-580}
IMAGE_PROJECT=${IMAGE_PROJECT:-deeplearning-platform-release}
DISK_SIZE=${DISK_SIZE:-200GB}
DISK_TYPE=${DISK_TYPE:-pd-balanced}
PROVISIONING=$(printf '%s' "${PROVISIONING:-STANDARD}" | tr '[:lower:]' '[:upper:]')
zone_accelerator=nvidia-l4
accelerator_flag=
if [[ "$MACHINE" == n1-* ]]; then
    zone_accelerator=${ACCELERATOR:-nvidia-tesla-t4}
    accelerator_flag="--accelerator=type=$zone_accelerator,count=1"
fi

# 專案的防火牆預設只允許使用者家裡的 IP 連 SSH；這條規則只放行 movie-nas 的內部 IP。
network_tag=haixia-gpu
firewall_rule=haixia-gpu-ssh-from-movie-nas
movie_nas_range=10.138.0.2/32

case "$PROVISIONING" in
    STANDARD)
        provisioning_label=一般計費
        provisioning_flags=(--provisioning-model=STANDARD)
        ;;
    SPOT)
        provisioning_label=Spot
        provisioning_flags=(--provisioning-model=SPOT --instance-termination-action=STOP)
        ;;
    *)
        printf 'PROVISIONING 只能是 STANDARD 或 SPOT，收到：%s\n' "$PROVISIONING" >&2
        exit 2
        ;;
esac

action=create
yes=false
usage() {
    printf '用法：[PROVISIONING=STANDARD|SPOT] bash scripts/create_gpu_vm.sh [--yes] [--stop|--start|--delete]\n'
    printf '預設只顯示要執行的指令；--yes 才會執行。--delete 必須搭配 --yes。\n'
    printf 'PROVISIONING 預設 STANDARD（一般計費，不會被收回）；SPOT 較便宜但可能隨時被收回。\n'
}

for argument in "$@"; do
    case "$argument" in
        --yes) yes=true ;;
        --stop|--start|--delete)
            if [[ "$action" != create ]]; then
                printf '只能指定一種操作。\n' >&2
                exit 2
            fi
            action=${argument#--}
            ;;
        -h|--help) usage; exit 0 ;;
        *) printf '未知參數：%s\n' "$argument" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ "$action" == delete && "$yes" != true ]]; then
    printf '刪除 VM 和開機磁碟必須明確加上 --yes。\n' >&2
    exit 2
fi

# 顯示指令時只替含特殊字元的參數加單引號，貼回 shell 就能執行。
shell_quote() {
    local safe='^[-A-Za-z0-9_./:=,@<>+]+$'
    if [[ "$1" =~ $safe ]]; then
        printf '%s' "$1"
    else
        printf "'%s'" "${1//\'/\'\\\'\'}"
    fi
}

show_command() {
    local argument separator=
    for argument in "$@"; do
        printf '%s' "$separator"
        shell_quote "$argument"
        separator=' '
    done
    printf '\n'
}

existing_command=(gcloud compute instances list "--project=$PROJECT" "--filter=name=$NAME" '--format=value(name,zone)')
firewall_check_command=(gcloud compute firewall-rules describe "$firewall_rule" "--project=$PROJECT" '--format=value(name)')
firewall_create_command=(gcloud compute firewall-rules create "$firewall_rule" "--project=$PROJECT"
    --network=default --direction=INGRESS --action=ALLOW --rules=tcp:22
    "--source-ranges=$movie_nas_range" "--target-tags=$network_tag"
    '--description=SSH from movie-nas internal IP to haixia-gpu only')
firewall_delete_command=(gcloud compute firewall-rules delete "$firewall_rule" "--project=$PROJECT")
zones_command=(gcloud compute accelerator-types list "--project=$PROJECT" "--filter=name=$zone_accelerator" '--format=value(zone)')

build_create_command() {
    create_command=(gcloud compute instances create "$NAME"
        "--project=$PROJECT" "--zone=$1" "--machine-type=$MACHINE")
    if [[ -n "$accelerator_flag" ]]; then create_command+=("$accelerator_flag"); fi
    create_command+=(
        "--image-family=$IMAGE_FAMILY" "--image-project=$IMAGE_PROJECT"
        "--boot-disk-size=$DISK_SIZE" "--boot-disk-type=$DISK_TYPE"
        "${provisioning_flags[@]}"
        --maintenance-policy=TERMINATE "--tags=$network_tag" --scopes=storage-rw
        --metadata=install-nvidia-driver=True)
}

ssh_hint() {
    printf '在 movie-nas 用這行連線：gcloud compute ssh %s --zone %s --internal-ip\n' "$NAME" "$1"
}

cost_reminder() {
    if [[ "$PROVISIONING" == SPOT ]]; then
        printf '費用提醒：Spot 較便宜，但隨時可能被收回（2026-09-25 實測開機 11 分鐘就被收回），收回後 VM 會停止；執行期間持續計費，停機後開機磁碟仍計費。用完請停機或刪除。\n'
    else
        printf '費用提醒：一般計費不會被收回，但執行期間 GPU、CPU 持續計費，停機後開機磁碟仍計費。用完請停機或刪除。\n'
    fi
}

print_firewall_missing() {
    printf '找不到防火牆規則 %s。沒有這條規則，movie-nas 無法以 SSH 連到 GPU VM，所以不建立 VM。\n' "$firewall_rule" >&2
    printf '建立規則會修改專案的網路設定，請先問使用者；同意後在 movie-nas 執行：\n' >&2
    show_command "${firewall_create_command[@]}" >&2
    printf '建好後再重跑這個腳本。\n' >&2
}

if [[ "$yes" != true ]]; then
    if [[ "$action" == create ]]; then
        build_create_command '<zone>'
        printf '預覽（%s，PROVISIONING=%s）：加上 --yes 才會依序執行下列步驟。\n' "$provisioning_label" "$PROVISIONING"
        printf '1. 確認沒有同名 VM；已經有的話停止，請改用 --start：\n   '
        show_command "${existing_command[@]}"
        printf '2. 確認防火牆規則 %s 存在；不存在時停止並印出建立指令（不會自動建立）：\n   ' "$firewall_rule"
        show_command "${firewall_check_command[@]}"
        printf '3. 查詢 us-central1 有 %s 的 zone：\n   ' "$zone_accelerator"
        show_command "${zones_command[@]}"
        printf '4. 依序在各 zone 建立。缺貨時把錯誤訊息 zonesAvailable 列出的 zone 排到下一個；每個 zone 最多試一次；其他錯誤立即停止：\n   '
        show_command "${create_command[@]}"
        printf '成功後'
        ssh_hint '<zone>'
    else
        printf '預覽：實際執行時會先查詢 %s 的 zone，再執行：\n' "$NAME"
        printf 'gcloud compute instances %s %s --project=%s --zone=<現有 VM 的 zone>' "$action" "$NAME" "$PROJECT"
        if [[ "$action" == delete ]]; then printf ' --delete-disks=boot'; fi
        printf '\n'
    fi
    cost_reminder
    exit 0
fi

command -v gcloud >/dev/null || { printf '找不到 gcloud；請在 movie-nas 上執行。\n' >&2; exit 1; }

if [[ "$action" != create ]]; then
    if ! found=$(gcloud compute instances list --project="$PROJECT" --filter="name=$NAME" --format='value(zone)' 2>&1); then
        printf '查詢 VM 失敗：\n%s\n' "$found" >&2
        exit 1
    fi
    zone=$(printf '%s\n' "$found" | sed -E 's@.*/@@' | grep -E '^us-central1-[a-z]$' | head -n 1 || true)
    if [[ -z "$zone" ]]; then
        printf '找不到 %s（us-central1）；請確認名稱及專案。\n' "$NAME" >&2
        exit 1
    fi
    command=(gcloud compute instances "$action" "$NAME" "--project=$PROJECT" "--zone=$zone")
    if [[ "$action" == delete ]]; then command+=(--delete-disks=boot); fi
    "${command[@]}"
    printf '已%s %s（%s）。\n' "$action" "$NAME" "$zone"
    if [[ "$action" == start ]]; then ssh_hint "$zone"; fi
    if [[ "$action" == delete ]]; then
        printf '防火牆規則 %s 仍保留。確認不再需要後，先問使用者，再執行：\n' "$firewall_rule"
        show_command "${firewall_delete_command[@]}"
    fi
    exit 0
fi

# 1. 同名 VM 已存在時不建立第二台，避免在另一個 zone 多開一台 GPU。
if ! existing=$("${existing_command[@]}" 2>&1); then
    printf '查詢現有 VM 失敗：\n%s\n' "$existing" >&2
    exit 1
fi
if [[ -n "$existing" ]]; then
    printf '已經有同名 VM，不建立第二台：\n%s\n請改用 --start --yes 啟動。\n' "$existing" >&2
    exit 1
fi

# 2. 防火牆規則只檢查，不自動建立。
if ! firewall=$("${firewall_check_command[@]}" 2>&1); then
    if [[ "$firewall" =~ not\ found|notFound ]]; then
        print_firewall_missing
    else
        printf '查詢防火牆規則 %s 失敗：\n%s\n' "$firewall_rule" "$firewall" >&2
    fi
    exit 1
fi

# 3. 查詢提供 GPU 的 zone。
if ! found=$("${zones_command[@]}" 2>&1); then
    printf '查詢 %s zone 失敗：\n%s\n' "$zone_accelerator" "$found" >&2
    exit 1
fi
queue=()
while IFS= read -r zone; do
    [[ -n "$zone" ]] && queue+=("$zone")
done < <(printf '%s\n' "$found" | sed -E 's@.*/@@' | grep -E '^us-central1-[a-z]$' | sort -u || true)
if (( ${#queue[@]} == 0 )); then
    printf 'us-central1 沒有查到提供 %s 的 zone。\n' "$zone_accelerator" >&2
    exit 1
fi

# 4. 依序嘗試；缺貨時優先改試錯誤訊息列出的 zone，每個 zone 最多一次。
exhausted_pattern='RESOURCE_POOL_EXHAUSTED|STOCKOUT|does not have enough resources|not enough resources|currently unavailable'
tried=()
reasons=()

was_tried() {
    local item
    for item in ${tried[@]+"${tried[@]}"}; do
        [[ "$item" == "$1" ]] && return 0
    done
    return 1
}

# 從錯誤訊息的 errorInfo.metadatas.zonesAvailable 取出 zone（可能以逗號或空白分隔）。
zones_available_in() {
    printf '%s\n' "$1" | grep -F zonesAvailable | sed -E 's/.*zonesAvailable//' \
        | grep -Eo '[a-z]+-[a-z]+[0-9]+-[a-z]' || true
}

first_error_line() {
    local line
    line=$(printf '%s\n' "$1" | grep -m 1 -E 'ERROR|message:' || true)
    [[ -n "$line" ]] || line=$(printf '%s\n' "$1" | head -n 1)
    line=$(printf '%s' "$line" | sed -E 's/^[[:space:]]+//')
    printf '%s' "${line:0:240}"
}

print_summary() {
    local index
    if (( ${#tried[@]} == 0 )); then return; fi
    printf '各 zone 的結果：\n' >&2
    for index in "${!tried[@]}"; do
        printf '  %s：%s\n' "${tried[$index]}" "${reasons[$index]}" >&2
    done
}

while (( ${#queue[@]} > 0 )); do
    zone=${queue[0]}
    queue=("${queue[@]:1}")
    if was_tried "$zone"; then continue; fi
    printf '正在嘗試 %s（%s）…\n' "$zone" "$provisioning_label"
    build_create_command "$zone"
    tried+=("$zone")
    if output=$("${create_command[@]}" 2>&1); then
        printf '%s\n' "$output"
        printf 'VM 已建立於 %s。\n' "$zone"
        ssh_hint "$zone"
        cost_reminder
        exit 0
    fi
    if [[ "$output" =~ $exhausted_pattern ]]; then
        code=$(printf '%s\n' "$output" | grep -Eo '[A-Z_]*(RESOURCE_POOL_EXHAUSTED|STOCKOUT)[A-Z_]*' | head -n 1 || true)
        reason="缺貨（${code:-資源不足}）"
        hinted=()
        ignored=()
        for candidate in $(zones_available_in "$output"); do
            if [[ ! "$candidate" =~ ^us-central1-[a-z]$ ]]; then
                ignored+=("$candidate")
            elif ! was_tried "$candidate"; then
                hinted+=("$candidate")
            fi
        done
        if (( ${#hinted[@]} > 0 )); then
            reason+="；錯誤訊息列出有容量的 zone：${hinted[*]}"
            printf '%s 缺貨；錯誤訊息列出 %s 目前有容量，下一個先試。\n' "$zone" "${hinted[*]}" >&2
            queue=("${hinted[@]}" ${queue[@]+"${queue[@]}"})
        else
            printf '%s 缺貨，改試下一個 zone。\n' "$zone" >&2
        fi
        if (( ${#ignored[@]} > 0 )); then
            printf '錯誤訊息也列出 %s，但不在 us-central1，不試。\n' "${ignored[*]}" >&2
        fi
        reasons+=("$reason")
        continue
    fi
    reasons+=("其他錯誤：$(first_error_line "$output")")
    printf '在 %s 建立失敗，而且不是缺貨，停止嘗試。完整錯誤：\n%s\n' "$zone" "$output" >&2
    print_summary
    exit 1
done
printf '所有 zone 都無法建立 %s VM（%s）；稍後再試，或改用另一種 PROVISIONING（先問使用者）。\n' "$zone_accelerator" "$provisioning_label" >&2
print_summary
exit 1
