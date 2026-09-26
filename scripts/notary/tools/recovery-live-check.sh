#!/usr/bin/env bash
# Живая проверка фазы 5 (план 2026-09-23-telemost-update-resilience) — ТОЛЬКО на
# тестовой копии скриптов и в песочнице. Боевые очередь, архив, реестр, замки и
# слушатель не трогаются; любая доставка уходит только в личку владельца.
#
#   (а) путь А (`--mode personal`) и путь Б (`--mode standard`) восстановления на
#       тестовой записи — обе серии песочницы без групповой привязки, поэтому бот
#       шлёт PDF в личку владельца (TELEGRAM_NOTARIUS_CHAT_ID из .env.notary);
#   (б) правка ответом находит протокол: для настоящего номера доставленного
#       сообщения зовём ту же функцию, что слушатель (`feedback_worker.
#       find_delivered_protocol`, её вызывает `route_feedback_reply`), с папками
#       песочницы; плюс перевыпуск находит расшифровку и протокол.
#
# Запуск на сервере (из тестовой копии, не из /srv):
#   WAV=/путь/к/синтетической/записи.wav /home/dev/resil-test/scripts/notary/tools/recovery-live-check.sh
# Необязательно: SANDBOX=<папка песочницы> PY=<python> DATE=YYYY-MM-DD ENV_FILE=<.env.notary>
#
# Расшифровка — тем же движком, что у бота (STT из .env.notary: центы за короткую
# запись). Побочные записи финализатора вне песочницы выключены флагами ниже
# (словарь, задачи, знания, уточнения). Остаётся одна строка метаданных в боевом
# журнале скорости расшифровки (`/srv/meeting-notary/logs/bench-*-prod.log`, путь
# зашит в finalize) — без текста встречи.
set -euo pipefail

NOTARY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
case "$NOTARY" in
  /srv/*) echo "Это боевая копия скриптов ($NOTARY) — запускай из тестовой." >&2; exit 2 ;;
esac
WAV="${WAV:?укажи WAV=<путь к тестовой записи .wav>}"
[ -f "$WAV" ] || { echo "нет файла записи: $WAV" >&2; exit 2; }
# Каждый прогон — своя новая папка: ничего не удаляем и не упираемся в файлы прошлого прогона.
SANDBOX="${SANDBOX:-/home/dev/resil-test/recovery-check/run-$(date -u +%Y%m%dT%H%M%S)}"
[ -e "$SANDBOX/transcripts" ] && { echo "песочница уже занята: $SANDBOX — укажи новую" >&2; exit 2; }
PY="${PY:-/home/dev/meeting-notary/venv/bin/python}"
DATE="${DATE:-$(date -u +%F)}"
ENV_FILE="${ENV_FILE:-/srv/meeting-notary/.env.notary}"
SERIES_A="resil-recovery-a"
SERIES_B="resil-recovery-b"

mkdir -p "$SANDBOX"/{registry,transcripts,protocols,archive,state,logs,failed,pending,feedback,runlog}
# Реестр песочницы: групповых привязок нет → доставка только в личку владельца.
printf 'watched: []\n' > "$SANDBOX/registry/watched.yaml"
echo "== песочница: $SANDBOX"

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
export PYTHONPATH="$(dirname "$NOTARY")" HOME=/home/dev MEETING_NOTARY_LOCAL_FINALIZE=1
# Все пути — в песочницу.
export MEETING_NOTARY_REGISTRY_DIR="$SANDBOX/registry"
export MEETING_NOTARY_TRANSCRIPTS_DIR="$SANDBOX/transcripts"
export TELEMOST_PROTOCOL_DIR="$SANDBOX/protocols" MEETING_NOTARY_PROTOCOLS_DIR="$SANDBOX/protocols"
export MEETING_NOTARY_ARCHIVE_DIR="$SANDBOX/archive"
export MEETING_NOTARY_DELIVERED_ROOTS="$SANDBOX/transcripts:$SANDBOX/protocols:$SANDBOX/archive"
export MEETING_NOTARY_FINALIZE_LOCK_DIRS="$SANDBOX/state" MEETING_NOTARY_STATE_DIR="$SANDBOX/state"
export MEETING_NOTARY_LOG_DIR="$SANDBOX/logs" MEETING_NOTARY_RUN_LOG_DIR="$SANDBOX/runlog"
export MEETING_NOTARY_FAILED_DIR="$SANDBOX/failed" MEETING_NOTARY_PENDING_DIR="$SANDBOX/pending"
export MEETING_NOTARY_FEEDBACK_DIR="$SANDBOX/feedback" AAI_SPEND_LEDGER_PATH="$SANDBOX/state/aai-spend.jsonl"
# Побочные действия финализатора вне песочницы — выключены.
export ENABLE_PORTAL_ORDERS=0 ENABLE_KNOWLEDGE_DISTILL=0 ENABLE_TASK_EXTRACTION=0 ENABLE_TASK_ROUTING=0
export ENABLE_STAKEHOLDER_TRACK_CLOSE=0 DISABLE_AUTO_VOCAB=1 ENABLE_MEETING_DECISION_DRAFTS=0
export ENABLE_VOCATIVE_JOURNAL=0 ENABLE_LLM_CLARIFY=0 PROTOCOL_DELIVERY_GRACE_SEC=0 ENABLE_CROSS_MEMORY=0
export ENABLE_VSTRECHI_INDEX=0 ENABLE_PROTOCOL_SELFREVIEW=0 ENABLE_PROTOCOL_REVIEW=0
export ENABLE_PROTOCOL_KNOWLEDGE_REVIEW=0
unset ENABLE_PROTOCOL_DELIVERY FAKE_DIARIZATION_PATH

run() {  # $1 — серия, $2 — путь
  local out="$SANDBOX/logs/ingest-$2.json" err="$SANDBOX/logs/ingest-$2.log" rc=0
  "$PY" "$NOTARY/tools/ingest-recording.py" "$WAV" --series "$1" --date "$DATE" --kind audio \
      --mode "$2" --participants "Тест-диктор" >"$out" 2>"$err" || rc=$?
  echo "== путь $2: rc=$rc; итог: $(grep -ao '{.*}' "$out" | tail -1)"
}
run "$SERIES_A" personal
run "$SERIES_B" standard

echo "== (б) ответ-правка на доставленные протоколы:"
"$PY" - "$SANDBOX" <<'PYEOF'
import json, sys
from pathlib import Path
from notary.lib import feedback_worker, feedback_reissue

sandbox = Path(sys.argv[1])
bad = 0
for mode in ("personal", "standard"):
    lines = [l for l in (sandbox / "logs" / f"ingest-{mode}.json").read_text(encoding="utf-8").splitlines()
             if l.strip().startswith("{")]
    res = json.loads(lines[-1]) if lines else {}
    meta_path = res.get("meta_path")
    if not res.get("ok") or not meta_path:
        print(f"{mode}: доставки нет — проверять нечего ({res.get('error') or res.get('note')})")
        bad += 1
        continue
    meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    recs = meta.get("delivered") or []
    recs = [recs] if isinstance(recs, dict) else recs
    for rec in recs:
        if not rec.get("chat_id") or not rec.get("message_ids"):
            continue
        mid = int(rec["message_ids"][0])
        found = feedback_worker.find_delivered_protocol(rec["chat_id"], mid, use_cache=False)
        other = feedback_worker.find_delivered_protocol(rec["chat_id"], mid + 100000, use_cache=False)
        paths = feedback_reissue._resolve_paths(
            {"series": meta.get("series"), "date": (meta.get("startTs") or "")[:10], "chat_id": rec["chat_id"]},
            Path(meta_path), meta) if found else (None, None)
        ok = bool(found) and other is None and all(p and Path(p).is_file() for p in paths)
        bad += 0 if ok else 1
        print(f"{mode}: сообщение {mid} → {'НАЙДЕН' if found else 'НЕ НАЙДЕН'} "
              f"(серия {found and found.get('series')}, meta {found and Path(found['meta_path']).name}); "
              f"чужой номер → {'не найден' if other is None else 'НАЙДЕН (ошибка)'}; "
              f"перевыпуску есть из чего: {'да' if all(paths) else 'нет'}")
print("ИТОГ:", "всё найдено" if bad == 0 else f"проблем: {bad}")
sys.exit(1 if bad else 0)
PYEOF
