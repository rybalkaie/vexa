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

echo "======================================================================"
echo "ГЕЙТ ЗОНЫ notary: файлов прогнано $total, упало $failed"
echo "Вызовов claude из тестов (заглушка): $calls"
[ "$calls" -ne 0 ] && sed 's/^/  /' "$STUB_DIR/calls.log"
rm -f "$STUB_DIR/claude" "$STUB_DIR/calls.log"
rmdir "$STUB_DIR" 2>/dev/null
if [ $failed -ne 0 ]; then
  printf '%s\n' "$failed_list"
  echo "======================================================================"
  echo "РЕЗУЛЬТАТ: КРАСНЫЙ"
  exit 1
fi
echo "РЕЗУЛЬТАТ: ЗЕЛЁНЫЙ"
exit 0
