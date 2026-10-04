#!/bin/sh
# Гейт зоны notary: прогон всех unittest-файлов scripts/notary/tests.
# Каждый файл — отдельный процесс (так они и написаны: __main__ + unittest.main()).
# Код возврата: 0 — все зелёные, 1 — есть падения.
#
# Аргумент (опционально): маска файлов, например `sh gate.sh 'test_iss5*'`.
#
# ВАЖНО (находка прогона): постоянного CI на эту зону в проекте НЕТ —
# .github/workflows покрывают только сервисы vexa. Эта команда и есть
# единственный автоматический гейт зоны.

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TESTS="$ROOT/scripts/notary/tests"
MASK="${1:-test_*.py}"

cd "$TESTS" || exit 2

# Герметичность (возобновление 05.10.2026): заглушка `claude` первой в PATH. Два наследных теста
# доходили до настоящей модели и тратили подписку на каждом прогоне гейта. Вызовы заглушки
# считаются и печатаются в итоге: ненулевое число = негерметичный тест, его надо чинить.
# Красный/зелёный от этого не меняется — заглушка даёт путь «модель недоступна».
STUB_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gate-no-claude.XXXXXX")" || exit 2
cat > "$STUB_DIR/claude" <<EOF
#!/bin/sh
echo "\${GATE_TESTFILE:-?} \$*" >> "$STUB_DIR/calls.log"
echo 'claude недоступен в гейте' >&2
exit 1
EOF
chmod +x "$STUB_DIR/claude"
PATH="$STUB_DIR:$PATH"
export PATH

# Сторож настоящих файлов (возобновление 05.10.2026): тесты уже писали в эти пути — журнал
# обучения и телеметрию правок, состояние подтверждений и памяти решений, барьер стартов. Всё, что
# изменилось за прогон, печатается в итоге. Красный/зелёный от этого не меняется, но ненулевое
# число = тест снова пишет мимо временной папки, его надо чинить.
# Сравнивается снимок «время в наносекундах + размер» до и после: `test -nt` в системном sh
# различает только секунды и пропускал запись в ту же секунду (поймано положительным контролем).
REAL_FILES="$HOME/Projects/meeting-notary/_feedback_edits/_learning/feedback-learning.jsonl
$HOME/Projects/meeting-notary/_feedback_edits/_telemetry/patch-tiers.jsonl
$HOME/.local/state/meeting-notary-watson/reply-ack-state.json
$HOME/.local/state/meeting-notary-watson/decision-memory.jsonl
$HOME/Projects/me/встречи/.state.json"
real_state() {
  printf '%s\n' "$REAL_FILES" | python3 -c '
import os, sys
for p in sys.stdin.read().splitlines():
    if p:
        try:
            st = os.stat(p)
            print("%s\t%d\t%d" % (p, st.st_mtime_ns, st.st_size))
        except OSError:
            print("%s\t-\t-" % p)
'
}
real_state > "$STUB_DIR/real-before"

total=0
failed=0
failed_list=""

for f in $MASK; do
  [ -f "$f" ] || continue
  total=$((total + 1))
  out="$(GATE_TESTFILE="$f" python3 "$f" 2>&1)"
  rc=$?
  if [ $rc -ne 0 ]; then
    failed=$((failed + 1))
    # Поимённый список упавших проверок — карантин сверяется по именам, а хвост в 25 строк
    # у файла с многими падениями их обрезал (находка доводки 05.09).
    failed_list="$failed_list
--- $f (rc=$rc) ---
упавшие проверки:
$(printf '%s\n' "$out" | grep -E '^(FAIL|ERROR): ' | sed 's/^/  /')
хвост вывода:
$(printf '%s' "$out" | tail -25)"
  fi
done

calls=0
[ -f "$STUB_DIR/calls.log" ] && calls="$(wc -l < "$STUB_DIR/calls.log" | tr -d ' ')"
real_state > "$STUB_DIR/real-after"
touched="$(paste "$STUB_DIR/real-before" "$STUB_DIR/real-after" | awk -F'\t' '$2 != $5 || $3 != $6 {print "  " $1}')"

echo "======================================================================"
echo "ГЕЙТ ЗОНЫ notary: файлов прогнано $total, упало $failed"
echo "Вызовов claude из тестов (заглушка): $calls"
[ "$calls" -ne 0 ] && sed 's/^/  /' "$STUB_DIR/calls.log"
if [ -n "$touched" ]; then
  echo "Тесты изменили настоящие файлы:"
  printf '%s\n' "$touched"
else
  echo "Тесты изменили настоящие файлы: нет"
fi
rm -f "$STUB_DIR/claude" "$STUB_DIR/calls.log" "$STUB_DIR/real-before" "$STUB_DIR/real-after"
rmdir "$STUB_DIR" 2>/dev/null
if [ $failed -ne 0 ]; then
  printf '%s\n' "$failed_list"
  echo "======================================================================"
  echo "РЕЗУЛЬТАТ: КРАСНЫЙ"
  exit 1
fi
echo "РЕЗУЛЬТАТ: ЗЕЛЁНЫЙ"
exit 0
