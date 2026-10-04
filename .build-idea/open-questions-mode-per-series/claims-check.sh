#!/bin/sh
# Закрывающая проверка по десяти утверждениям плана (Т1–Т10): каждая строка — исполняемое
# доказательство, тест-классы взяты из сквозной проверки (artifacts/final-seams.md) и
# сторожей доводки 05.09. Заведено на возобновлении 05.10.
#
# Аргумент: каталог scripts/notary/tests того дерева, которое проверяем (по умолчанию — ветка
# прогона). Так одна и та же таблица снимается и с ветки, и с копии рабочей линии.
# Печатает строку на утверждение: «Т<N> ✅|❌ <что упало>». Код возврата 0 — все ✅.

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
T="${1:-$ROOT/scripts/notary/tests}"
cd "$T" || exit 2

STUB_DIR="$(mktemp -d "${TMPDIR:-/tmp}/claims-no-claude.XXXXXX")" || exit 2
printf '#!/bin/sh\necho "claude недоступен" >&2\nexit 1\n' > "$STUB_DIR/claude"
chmod +x "$STUB_DIR/claude"
PATH="$STUB_DIR:$PATH"; export PATH
PYTHONDONTWRITEBYTECODE=1; export PYTHONDONTWRITEBYTECODE

bad=0
check() {  # check <Т> <файл> <классы...>
  id="$1"; f="$2"; shift 2
  out="$(python3 "$f" "$@" 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then
    echo "$id ✅ $f $*"
  else
    bad=1
    echo "$id ❌ $f $*"
    printf '%s\n' "$out" | grep -E '^(FAIL|ERROR): |^(AttributeError|ImportError|ModuleNotFoundError)' | sed 's/^/      /' | head -12
  fi
}

check Т1  test_iss51_last_only_digest.py TestLastOnlyContent
check Т2  test_iss51_last_only_digest.py TestLastOnlyContent.test_no_status_request_anywhere TestLastOnlyContent.test_header_is_owner_draft_verbatim TestLastOnlyContent.test_caption_is_soft TestLastOnlyContent.test_pdf_cover_is_soft_too
check Т3  test_iss51_last_only_digest.py TestProtocolTailGate
check Т4  test_iss51_last_only_digest.py TestCarryUnchangedByteForByte TestStatusMachineSilence.test_status_store_write_allowed_in_carry TestStatusMachineSilence.test_protocol_status_reply_still_works_in_carry
check Т4  test_stale_date_record_prune.py
check Т5  test_iss51_mode_command.py TestSwitchBothWays TestModeQuery TestOwnerGate
check Т6  test_clean_slate_and_rejection_guards.py TestCleanSlateUsesFullHistory TestCleanSlateCoversMinorBucket TestCarrySinceValidation
check Т7  test_iss53_55_evening_reject_tz.py TestRejectIsRemembered TestRejectionRetention TestBrokenSnapshotItems
check Т7  test_clean_slate_and_rejection_guards.py TestYesCancelsNoAcrossKinds
check Т8  test_iss53_55_evening_reject_tz.py TestDisplayTimezone TestTimeMatchesEndToEnd
check Т9  test_iss54_meeting_dedup.py
check Т10 test_iss51_mode_registry.py TestSendArtifactWithoutReply TestBareFileNeedsReply
check Т10 test_iss51_send_artifact_ai.py TestVerblessRequests TestBareFileOnlyInReplyToUs TestMentioningIsNotAsking

rm -f "$STUB_DIR/claude"; rmdir "$STUB_DIR" 2>/dev/null
exit $bad
