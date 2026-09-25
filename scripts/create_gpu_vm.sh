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
zone_accelerator=nvidia-l4
accelerator_flag=
if [[ "$MACHINE" == n1-* ]]; then
    zone_accelerator=${ACCELERATOR:-nvidia-tesla-t4}
    accelerator_flag="--accelerator=type=$zone_accelerator,count=1"
fi

action=create
yes=false
usage() {
    printf '用法：bash scripts/create_gpu_vm.sh [--yes] [--stop|--start|--delete]\n'
    printf '預設只顯示建立指令；--yes 才會執行。--delete 必須搭配 --yes。\n'
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

if [[ "$yes" != true ]]; then
    if [[ "$action" == create ]]; then
        printf '預覽：實際執行時會先查詢 us-central1 有 %s 的 zone，再依序嘗試：\n' "$zone_accelerator"
        printf 'gcloud compute accelerator-types list --project=%s --filter=name=%s --format=value(zone)\n' "$PROJECT" "$zone_accelerator"
        printf 'gcloud compute instances create %s --project=%s --zone=<查得的 zone> --machine-type=%s' "$NAME" "$PROJECT" "$MACHINE"
        if [[ -n "$accelerator_flag" ]]; then printf ' %s' "$accelerator_flag"; fi
        printf ' --image-family=%s --image-project=%s --boot-disk-size=%s --boot-disk-type=%s --provisioning-model=SPOT --instance-termination-action=STOP --maintenance-policy=TERMINATE --scopes=storage-rw --metadata=install-nvidia-driver=True\n' \
            "$IMAGE_FAMILY" "$IMAGE_PROJECT" "$DISK_SIZE" "$DISK_TYPE"
    else
        printf '預覽：實際執行時會先查詢 %s 的 zone，再執行：\n' "$NAME"
        printf 'gcloud compute instances %s %s --project=%s --zone=<現有 VM 的 zone>' "$action" "$NAME" "$PROJECT"
        if [[ "$action" == delete ]]; then printf ' --delete-disks=boot'; fi
        printf '\n'
    fi
    printf '費用提醒：Spot GPU 在執行期間持續計費，停機後開機磁碟仍計費；用完請停機或刪除。\n'
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
    exit 0
fi

if ! found=$(gcloud compute accelerator-types list --project="$PROJECT" --filter="name=$zone_accelerator" --format='value(zone)' 2>&1); then
    printf '查詢 %s zone 失敗：\n%s\n' "$zone_accelerator" "$found" >&2
    exit 1
fi
mapfile -t zones < <(printf '%s\n' "$found" | sed -E 's@.*/@@' | grep -E '^us-central1-[a-z]$' | sort -u)
if (( ${#zones[@]} == 0 )); then
    printf 'us-central1 沒有查到提供 %s 的 zone。\n' "$zone_accelerator" >&2
    exit 1
fi

for zone in "${zones[@]}"; do
    printf '正在嘗試 %s …\n' "$zone"
    command=(gcloud compute instances create "$NAME"
        "--project=$PROJECT" "--zone=$zone" "--machine-type=$MACHINE")
    if [[ -n "$accelerator_flag" ]]; then command+=("$accelerator_flag"); fi
    command+=(
        "--image-family=$IMAGE_FAMILY" "--image-project=$IMAGE_PROJECT"
        "--boot-disk-size=$DISK_SIZE" "--boot-disk-type=$DISK_TYPE"
        --provisioning-model=SPOT --instance-termination-action=STOP
        --maintenance-policy=TERMINATE --scopes=storage-rw
        --metadata=install-nvidia-driver=True)
    if output=$("${command[@]}" 2>&1); then
        printf '%s\n' "$output"
        printf 'VM 已建立；SSH 指令：gcloud compute ssh %s --zone %s\n' "$NAME" "$zone"
        printf '費用提醒：用完請停機或刪除，停機後開機磁碟仍計費。\n'
        exit 0
    fi
    if [[ "$output" =~ ZONE_RESOURCE_POOL_EXHAUSTED|RESOURCE_POOL_EXHAUSTED|does\ not\ have\ enough\ resources|not\ enough\ resources ]]; then
        printf '%s 資源不足，改試下一個 zone。\n' "$zone" >&2
        continue
    fi
    printf '建立失敗，停止嘗試：\n%s\n' "$output" >&2
    exit 1
done
printf '所有可用 zone 均沒有足夠的 %s Spot 資源；稍後再試。\n' "$zone_accelerator" >&2
exit 1
