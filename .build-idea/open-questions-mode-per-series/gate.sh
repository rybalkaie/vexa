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

total=0
failed=0
failed_list=""

for f in $MASK; do
  [ -f "$f" ] || continue
  total=$((total + 1))
  out="$(python3 "$f" 2>&1)"
  rc=$?
  if [ $rc -ne 0 ]; then
    failed=$((failed + 1))
    failed_list="$failed_list
--- $f (rc=$rc) ---
$(printf '%s' "$out" | tail -25)"
  fi
done

echo "======================================================================"
echo "ГЕЙТ ЗОНЫ notary: файлов прогнано $total, упало $failed"
if [ $failed -ne 0 ]; then
  printf '%s\n' "$failed_list"
  echo "======================================================================"
  echo "РЕЗУЛЬТАТ: КРАСНЫЙ"
  exit 1
fi
echo "РЕЗУЛЬТАТ: ЗЕЛЁНЫЙ"
exit 0
