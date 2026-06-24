#!/usr/bin/env bash
# Ф2 / R8 (ISS-21 Gap-1): рекуррентный pull клонов `*-context` на VPS, чтобы
# правки glossary.yaml/каталога с мака доезжали САМИ, без ручного `make deploy-context`.
#
# Что делает: для КАЖДОГО уже забутстрапленного клона в $CONTEXT_DIR делает
# `git fetch origin` + `git reset --hard origin/HEAD` (read-only снимок последнего
# состояния). Это НЕ bootstrap — первичный клон по-прежнему `make deploy-context`
# (deploy keys). Origin клонов уже указывает на host-алиасы deploy-ключей
# (github-notary-anzhee/-mpfirst), поэтому fetch ходит этими ключами без правок.
#
# Запускается systemd-таймером meeting-notary-context-pull.timer (как dev,
# HOME=/home/dev → ~/.ssh/config с deploy-ключами). Активация таймера = ПРОВИЖН на
# VPS (прод-нотариус, control-gate владельца) — см. DEPLOY-handoff Д2.x.
#
# Опасная тройка: знание (термины/каталог) — производная лексика, не сырьё реплик;
# тексты транскриптов сюда не попадают. Логируем только метаданные (репо/SHA/итог).
set -u

CONTEXT_DIR="${MEETING_NOTARY_CONTEXT_DIR:-/srv/meeting-notary/context}"
LOCK_FILE="${CONTEXT_PULL_LOCK:-/tmp/meeting-notary-context-pull.lock}"

log() { printf '%s context-pull: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Не наслаиваем прогоны (флок без ожидания — следующий тик и так через интервал).
exec 9>"$LOCK_FILE" 2>/dev/null || true
if command -v flock >/dev/null 2>&1; then
  if ! flock -n 9; then
    log "уже выполняется (lock $LOCK_FILE) — пропуск тика"
    exit 0
  fi
fi

if [ ! -d "$CONTEXT_DIR" ]; then
  log "нет $CONTEXT_DIR — клоны ещё не забутстраплены (make deploy-context). Пропуск."
  exit 0
fi

rc=0
found=0
for d in "$CONTEXT_DIR"/*/; do
  [ -d "${d}.git" ] || continue
  found=1
  name="$(basename "$d")"
  before="$(git -C "$d" rev-parse --short HEAD 2>/dev/null || echo '?')"
  if ! git -C "$d" fetch --quiet --prune origin 2>/dev/null; then
    log "✗ $name: git fetch не удался (проверь deploy key / доступ)"
    rc=1
    continue
  fi
  # origin/HEAD — дефолтная ветка; на shallow-клоне выставляем её явно (belt).
  git -C "$d" remote set-head origin --auto >/dev/null 2>&1 || true
  if ! git -C "$d" reset --hard --quiet origin/HEAD 2>/dev/null; then
    log "✗ $name: git reset --hard origin/HEAD не удался"
    rc=1
    continue
  fi
  after="$(git -C "$d" rev-parse --short HEAD 2>/dev/null || echo '?')"
  if [ "$before" = "$after" ]; then
    log "= $name: без изменений ($after)"
  else
    log "↑ $name: $before → $after"
  fi
done

if [ "$found" -eq 0 ]; then
  log "в $CONTEXT_DIR нет git-клонов — нечего пуллить (сначала make deploy-context)"
fi
exit "$rc"
