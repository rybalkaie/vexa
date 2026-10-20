"""Ручное восстановление ПОРТАЛЬНОЙ встречи по наряду (сторона бота).

Зовёт обёртка `tools/ingest-recording.py`. План:
plans/2026-10-20-watson-model-names-from-catalog.md, Фаза 2.

ЗАЧЕМ. У синтетической meta ручного восстановления нет ни комнаты, ни времени
встречи — по ним finalize наряд не находит, компания не определяется, и знания
компании (каталог, глоссарий, оргструктура) в протокол не попадают: Ватсон пишет
названия моделей на слух. Протокол в карточку портала и номер версии при этом
шли руками мимо очереди, и правка ответом на PDF до портала не доходила.

ЧТО ДЕЛАЕТ МОДУЛЬ.
  • `resolve_portal_order` — наряд `portal:<id>` из реестра бота по серии
    `portal-<id>` (или явному `--portal-order`), с отказом ДО распознавания: нет
    наряда, номер встречи не тот, компания вне канона.
  • `portal_company_fields` — компания наряда для meta СБОРКИ протокола.
  • `stamp_portal_order` — наряд в meta ДОСТАВКИ (`delivery_route.stamp_meta`).
  • `knowledge_facts` — сколько файлов знаний подключит сборка (для итоговой строки).
  • `next_portal_version` / `enqueue_portal_result` — протокол в очередь доставки
    портала со следующим номером версии по счёту бота.

Опасная тройка: в журнал — только id наряда, компания и счётчики, текста встречи нет.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ingest-recording.portal")

#: Префикс серии, которую ведёт портал, — ровно `series_memory.PORTAL_SERIES_PREFIX`.
PORTAL_SERIES_PREFIX = "portal-"
#: Префикс id наряда — ровно `orders.order_id_for_meeting`.
PORTAL_ORDER_PREFIX = "portal:"
#: Пометка порталу, куда ушёл протокол: лично владельцу бота на проверку (путь А).
#: Та же форма, что у ручных отправок, которые портал уже принимал.
PORTAL_TELEGRAM_NOTE = {"mode": "dm", "reason": "manual_recovery"}

_ORDER_ID_RE = re.compile(r"[A-Za-z0-9._\-]{1,128}")


class PortalOrderError(ValueError):
    """Наряд портальной встречи не годится — отказ ДО распознавания (ничего не тратится).

    `code` уезжает в итоговую строку обёртки полем `error`."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def portal_meeting_id_from_series(series: Optional[str]) -> Optional[str]:
    """Номер карточки из серии `portal-<id>`; не портальная серия → None."""
    if not isinstance(series, str) or not series.startswith(PORTAL_SERIES_PREFIX):
        return None
    mid = series[len(PORTAL_SERIES_PREFIX):].strip()
    return mid or None


def normalize_portal_order_id(raw: Optional[str]) -> str:
    """`--portal-order`: `portal:<id>` или голый `<id>` → `portal:<id>`."""
    value = (raw or "").strip()
    if value.startswith(PORTAL_ORDER_PREFIX):
        value = value[len(PORTAL_ORDER_PREFIX):].strip()
    if not value or not _ORDER_ID_RE.fullmatch(value):
        raise PortalOrderError("portal_order_invalid",
                               "--portal-order: ожидался номер карточки портала (portal:<id>)")
    return PORTAL_ORDER_PREFIX + value


def resolve_portal_order(series: str, explicit: Optional[str] = None) -> Optional[dict]:
    """Наряд встречи для ручного восстановления. None — серия не портальная и
    `--portal-order` не задан: поведение обёртки как раньше.

    Наряд — из реестра бота (`.orders.json`) по id `portal:<номер карточки>`: номер
    из серии `portal-<id>` или из `--portal-order`. Проверки до распознавания:
    наряд есть; номер встречи в нём тот же, что в его id, а серия совпадает с ним
    (по номеру карточки или по имени серии из наряда); компания — из канона.
    Компания берётся ТОЛЬКО из наряда: протокол чужой компании не получит знаний
    Anzhee, даже если slug серии где-то совпал."""
    series_mid = portal_meeting_id_from_series(series)
    if explicit is None and series_mid is None:
        return None
    order_id = (normalize_portal_order_id(explicit) if explicit is not None
                else PORTAL_ORDER_PREFIX + series_mid)
    try:
        from lib import orders as orders_lib  # noqa: PLC0415

        order = orders_lib.get_order(order_id)
    except Exception as e:  # noqa: BLE001 — «не прочитали» ≠ «наряда нет»
        raise PortalOrderError(
            "portal_order_unreadable",
            "реестр нарядов не прочитан (%s) — чья это встреча, не выяснено" % type(e).__name__,
        ) from e
    if order is None:
        raise PortalOrderError(
            "portal_order_missing",
            "наряда %s в реестре бота нет — без него не подключить знания компании и не "
            "отдать протокол в портал; проверь номер карточки (или укажи --portal-order)"
            % order_id,
        )
    order_mid = str(order.get("meeting_id") or "").strip()
    if (not order_mid or str(order.get("id") or order_id) != order_id
            or PORTAL_ORDER_PREFIX + order_mid != order_id):
        raise PortalOrderError("portal_order_mismatch",
                               "в наряде %s другой номер встречи — не он" % order_id)
    allowed_series = {str(order.get("series") or "").strip(),
                      orders_lib._slug_from_meeting_id(order_mid)} - {""}
    if series not in allowed_series:
        raise PortalOrderError(
            "portal_order_mismatch",
            "серия %s не совпадает с нарядом %s (номер встречи другой)" % (series, order_id),
        )
    raw_company = order.get("company")
    company = raw_company.strip().lower() if isinstance(raw_company, str) else ""
    try:
        from notary.cli.registry import VALID_COMPANIES  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 — канон недоступен → не доверяем
        raise PortalOrderError("portal_company_invalid",
                               "канон компаний не прочитан (%s)" % type(e).__name__) from e
    if company not in VALID_COMPANIES:
        raise PortalOrderError(
            "portal_company_invalid",
            "компания наряда %s вне канона %s — знания не подключаю"
            % (order_id, sorted(VALID_COMPANIES)),
        )
    logger.info("[portal] встреча по наряду %s (компания %s)", order_id, company)
    return order


def stamp_portal_order(meta: dict, order: Optional[dict]) -> dict:
    """Проштамповать наряд в meta тем же `delivery_route.stamp_meta`, что у finalize:
    `meta["order"]` — наряд, `meta["company"]` — его компания. Так штампуется meta
    ДОСТАВКИ: по ней правка ответом на PDF находит наряд (`order_for_meeting`) и
    ставит следующую версию в очередь портала. Без наряда meta не трогается."""
    if not order:
        return meta
    from lib import delivery_route  # noqa: PLC0415

    meta["order"] = dict(order)
    delivery_route.stamp_meta(meta, session_uid=meta.get("sessionUid"))
    return meta


def portal_company_fields(order: Optional[dict]) -> dict:
    """Компания наряда (`{"company": …}`) для meta СБОРКИ протокола — finalize и сборки
    по тексту. Значение даёт тот же `stamp_meta`.

    🔴 Сам наряд (`order`) в meta сборки НЕ кладём, только компанию. С нарядом в meta
    finalize адресует уточнения («кто говорил» с цитатой из расшифровки, «сколько
    задач») в личку владельца НАРЯДА — сотруднику (`delivery_route.clarify_chat_for_meeting`).
    Ручное восстановление идёт лично владельцу бота на проверку, людям без его слова
    ничего не уходит. Компании достаточно, чтобы подключились знания компании
    (`series_markup.company_for_meeting` читает `meta["company"]` по канону)."""
    if not order:
        return {}
    stamped = stamp_portal_order({}, order)
    return {"company": stamped["company"]} if stamped.get("company") else {}


def knowledge_facts(meta: dict) -> dict:
    """`company` и `knowledge_files` для итоговой строки: сколько файлов знаний
    компании подключит сборка протокола. Тот же расчёт, что у `generate_protocol`
    (`series_markup.company_for_meeting` → `context_knowledge.load_protocol_knowledge`),
    — пустые знания видно сразу, а не через неделю. В журнал — только числа."""
    try:
        from lib import context_knowledge, series_markup  # noqa: PLC0415

        company = series_markup.company_for_meeting(meta)
        files = 0
        if company:
            bundle = context_knowledge.load_protocol_knowledge(company)
            files = len(bundle.catalog) + len(bundle.brief)
    except Exception as e:  # noqa: BLE001 — сводка не имеет права уронить восстановление
        logger.warning("[knowledge] знания не посчитаны (%s)", type(e).__name__)
        return {"company": None, "knowledge_files": None}
    logger.info("[knowledge] компания=%s knowledge-files=%d", company, files)
    return {"company": company, "knowledge_files": files}


def next_portal_version(order: dict, meta: Optional[dict]) -> int:
    """Номер версии результата по счёту бота (`feedback_reissue._next_portal_version`:
    очередь + отметка `portal_version` в meta).

    Одно отличие: если ни очередь, ни meta об этом наряде не знают ничего, в портал от
    бота ещё ничего не уходило — первая версия (`result_version` наряда, как у
    `collector._portal_enqueue_result`), а не «2»: портал отвергает разрывы версий."""
    from lib import feedback_reissue, portal_delivery  # noqa: PLC0415

    order_id = str(order.get("id") or "")
    try:
        marked = int((meta or {}).get("portal_version") or 0)
    except (TypeError, ValueError):
        marked = 0
    known = marked > 0 or any(
        isinstance(i, dict) and str(i.get("order_id") or "") == order_id
        for i in portal_delivery.list_items()
    )
    if known:
        return feedback_reissue._next_portal_version(order_id, meta=meta)
    version = max(1, int(order.get("result_version") or 1))
    while portal_delivery.item_path(portal_delivery.delivery_key(order_id, version)).exists():
        version += 1
    return version


def _read_text(path: Optional[Path]) -> str:
    try:
        return Path(path).read_text(encoding="utf-8") if path else ""
    except OSError:
        return ""


def enqueue_portal_result(
    *, order: dict, meta: dict, sid: str, protocol_path: Path, transcript_path: Path,
    telegram_status: str,
) -> dict:
    """Результат восстановления → очередь доставки портала (`portal_delivery.enqueue`),
    тело — `portal_ingest.build_payload`, как у `collector._portal_enqueue_result`.
    Портал не принял — очередь повторяет сама, как у обычной встречи.

    Возвращает сводку для итоговой строки: `status` (`queued` | `disabled` | `error` |
    `duplicate`), `version`. Отметку `portal_version` в файл meta пишет вызывающий —
    при `queued`."""
    from lib import portal_delivery, portal_ingest  # noqa: PLC0415

    if not portal_delivery.is_enabled():
        logger.warning("[portal] доставка в портал выключена (ENABLE_PORTAL_ORDERS) — в "
                       "очередь не ставлю")
        return {"status": "disabled"}
    order_id = str(order.get("id") or "")
    version = next_portal_version(order, meta)
    payload = portal_ingest.build_payload(
        order_id=order_id,
        meeting_id=order.get("meeting_id"),
        version=version,
        company=order.get("company"),
        session_uid=sid,
        protocol_md=_read_text(protocol_path),
        transcript_text=_read_text(transcript_path),
        # Как у первой доставки и перевыпуска: задачи портал разбирает из protocol_md.
        tasks=[],
        participants=meta.get("participants") or [],
        expected_participants=meta.get("expectedParticipants") or [],
        # У синтетической meta `startTs` — полночь (ISS-65): время встречи — из наряда.
        started_at=order.get("start_at") or meta.get("startTs"),
        ended_at=meta.get("endTs"),
        duration_seconds=meta.get("durationS"),
        telegram_delivery={"status": telegram_status, **PORTAL_TELEGRAM_NOTE},
        order_status=order.get("status"),
        revision_of=(version - 1) if version > 1 else None,
        edits=[],
    )
    try:
        key, created = portal_delivery.enqueue(
            order_id=order_id, meeting_id=order.get("meeting_id"), version=version,
            session_uid=sid, company=order.get("company"), payload=payload, sources={},
        )
    except Exception as e:  # noqa: BLE001
        logger.error("[portal] результат НЕ поставлен в очередь портала (%s)", type(e).__name__)
        return {"status": "error", "version": version, "error": type(e).__name__}
    if not created:
        logger.error("[portal] версия %d наряда %s НЕ записана: ключ %s уже занят",
                     version, order_id, key)
        return {"status": "duplicate", "version": version, "key": key}
    logger.info("[portal] версия %d наряда %s поставлена в очередь портала", version, order_id)
    return {"status": "queued", "version": version, "key": key}


def remember_portal_version(meta: dict, meta_path: Optional[Path], version: int) -> None:
    """Отметка `portal_version` в meta доставки — счёт версий переживает уборку очереди:
    правка ответом на PDF даст следующую версию, повтор с --force — новую, не дубль.
    Best-effort: версия уже в очереди, сбой записи отметки её не откатывает."""
    meta["portal_version"] = int(version)
    if meta_path is None:
        return
    try:
        on_disk = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        on_disk = None
    if not isinstance(on_disk, dict):
        on_disk = dict(meta)
    try:
        if int(on_disk.get("portal_version") or 0) >= int(version):
            return
        on_disk["portal_version"] = int(version)
        from lib.llm_postprocess import _atomic_write_text  # noqa: PLC0415

        _atomic_write_text(Path(meta_path), json.dumps(on_disk, ensure_ascii=False, indent=2))
    except Exception as e:  # noqa: BLE001
        logger.warning("[portal] отметка portal_version не записана в meta (%s)", type(e).__name__)


def deliver_to_portal(
    *, order: dict, meta: dict, meta_path: Optional[Path], sid: str,
    protocol_path: Path, transcript_path: Path, delivered_ok: bool,
) -> dict:
    """Протокол портальной встречи — в карточку очередью доставки портала, как у
    обычной встречи (повторы при отказе портала — забота очереди), и отметка версии
    в meta доставки. Ручной скрипт отправки больше не нужен."""
    portal = enqueue_portal_result(
        order=order, meta=meta, sid=sid, protocol_path=protocol_path,
        transcript_path=transcript_path, telegram_status="sent" if delivered_ok else "error",
    )
    if portal.get("status") == "queued":
        remember_portal_version(meta, meta_path, int(portal["version"]))
    return portal


def preflight(series: str, explicit: Optional[str], *, standard_mode: bool) -> Optional[dict]:
    """Всё, что проверяется ДО распознавания: наряд (`resolve_portal_order`) и путь.

    Портальная встреча — только путь А (лично владельцу): путь Б по наряду разослал
    бы PDF сотрудникам по маршруту наряда мимо проверки владельца."""
    order = resolve_portal_order(series, explicit)
    if order is not None and standard_mode:
        raise PortalOrderError(
            "portal_order_personal_only",
            "портальную встречу восстанавливаю только лично владельцу (--mode personal): "
            "в карточку протокол уйдёт очередью портала",
        )
    return order


def summary_facts(series: str, order: Optional[dict]) -> dict:
    """Поля итоговой строки обёртки: `company`, `knowledge_files` (+ `portal_order`)."""
    facts = knowledge_facts({"series": series, **portal_company_fields(order)})
    if order is not None:
        facts["portal_order"] = order.get("id")
    return facts
