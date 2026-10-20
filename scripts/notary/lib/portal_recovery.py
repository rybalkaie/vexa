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
    портала со следующим номером версии по счёту бота (служебные «бот не пришёл» —
    не версии).
  • `prepare_delivery_meta` — время встречи из наряда, «кто был», счёт версий: их
    повторит перевыпуск по правке ответом на PDF.
  • `deliver_to_portal` — в карточку только после PDF владельцу (`held` иначе) и
    строка владельцу, что с карточкой; `redeliver_after_force` — повтор с --force
    шлёт владельцу новую версию ревизией.

Опасная тройка: в журнал — только id наряда, компания и счётчики, текста встречи нет.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ingest-recording.portal")

#: Префикс серии, которую ведёт портал, — ровно `series_memory.PORTAL_SERIES_PREFIX`.
PORTAL_SERIES_PREFIX = "portal-"
#: Префикс id наряда — ровно `orders.order_id_for_meeting`.
PORTAL_ORDER_PREFIX = "portal:"

_ORDER_ID_RE = re.compile(r"[A-Za-z0-9._\-]{1,128}")
#: Москва — дата встречи по ней (перевода часов в России нет с 2014 года).
MSK = timezone(timedelta(hours=3))


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


def _parse_moment(raw) -> Optional[datetime]:
    """ISO-время СО смещением пояса → datetime; без пояса или мусор → None
    (как `orders._parse_dt`: время без пояса — источник «бот пришёл на три часа раньше»)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def order_start(order: Optional[dict]) -> Optional[datetime]:
    """Начало встречи наряда: плановый `start_at`; у разового (`kind=immediate`) —
    момент запуска бота `run.launched_at` (его `start_at` — время постановки наряда),
    как сопоставляет запись с нарядом `orders.find_order_for_recording`."""
    if not isinstance(order, dict):
        return None
    run = order.get("run")
    if order.get("kind") == "immediate" and isinstance(run, dict):
        launched = _parse_moment(run.get("launched_at"))
        if launched is not None:
            return launched
    return _parse_moment(order.get("start_at"))


def order_msk_dates(order: dict) -> set:
    """Даты встречи наряда по Москве — сам наряд и его прежние запуски (`previous_runs`
    — та же карточка)."""
    runs = [order] + [r for r in (order.get("previous_runs") or []) if isinstance(r, dict)]
    return {s.astimezone(MSK).date().isoformat() for s in map(order_start, runs) if s}


def _find_order_by_series_date(series: str, date: Optional[str]) -> Optional[str]:
    """Серия без префикса `portal-` (`anzhee-direktorat` и др.): наряд с той же серией
    и той же датой по Москве. Ровно один — его id; ни одного — None (встреча не
    портальная, обёртка как раньше); больше одного — отказ: выбирать наугад нельзя.
    Реестр не прочитан — None с предупреждением: так было до починки."""
    if not date:
        return None
    try:
        from lib import orders as orders_lib  # noqa: PLC0415

        found = [o for o in orders_lib.list_orders()
                 if str(o.get("series") or "").strip() == series and date in order_msk_dates(o)]
    except Exception as e:  # noqa: BLE001
        logger.warning("[portal] реестр нарядов не прочитан (%s) — наряд серии не искал",
                       type(e).__name__)
        return None
    ids = sorted({str(o.get("id") or "") for o in found} - {""})
    if len(ids) > 1:
        raise PortalOrderError(
            "portal_order_ambiguous",
            "у серии %s на %s нарядов несколько (%s) — укажи нужный: --portal-order <id>"
            % (series, date, ", ".join(ids)),
        )
    return ids[0] if ids else None


def resolve_portal_order(series: str, explicit: Optional[str] = None, *,
                         date: Optional[str] = None) -> Optional[dict]:
    """Наряд встречи для ручного восстановления. None — встреча не портальная:
    поведение обёртки как раньше.

    Где ищем: серия `portal-<id>` → наряд `portal:<id>`; `--portal-order` — явно; серия
    без префикса → наряд той же серии на ту же дату (`_find_order_by_series_date`).
    Проверки до распознавания: наряд есть; номер встречи в нём тот же, что в его id;
    серия — серия наряда (иначе протокол ляжет мимо папки, куда смотрит «Скачать PDF»
    карточки); дата `--date` — дата встречи наряда по Москве; компания — из канона.
    Компания берётся ТОЛЬКО из наряда: протокол чужой компании не получит знаний
    Anzhee, даже если slug серии где-то совпал. Возвращает наряд с приведёнными
    `id`, `meeting_id`, `company`."""
    series_mid = portal_meeting_id_from_series(series)
    if explicit is None and series_mid is None:
        explicit = _find_order_by_series_date(series, date)
        if explicit is None:
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
    order_series = str(order.get("series") or "").strip()
    expected_series = order_series or orders_lib._slug_from_meeting_id(order_mid)
    if series != expected_series:
        if series_mid == order_mid:
            # Серия называет ту же карточку, а наряд живёт в своей серии: протокол лёг
            # бы в portal-<id>/, а портал (`gateway.protocol_path_for_order`) ищет его
            # в папке серии наряда — «Скачать PDF» отдал бы чужой или никакой.
            raise PortalOrderError(
                "portal_order_series_mismatch",
                "наряд %s ведёт серия %s — запусти с --series %s" % (
                    order_id, order_series, order_series),
            )
        raise PortalOrderError(
            "portal_order_mismatch",
            "серия %s не совпадает с нарядом %s (номер встречи другой)" % (series, order_id),
        )
    if date:
        dates = order_msk_dates(order)
        if date not in dates:
            raise PortalOrderError(
                "portal_order_date_mismatch",
                "встреча наряда %s — %s, а --date %s: это другая встреча; проверь дату или "
                "карточку" % (order_id, ", ".join(sorted(dates)) or "без времени начала", date),
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
    return {**order, "id": order_id, "meeting_id": order_mid, "company": company}


def check_previous_recovery(previous_meta: Optional[dict], order: dict) -> None:
    """Прошлое восстановление той же серии и даты (то же имя сессии `recovery-<серия>-
    <дата>`, те же файлы протокола) было по ДРУГОМУ наряду → отказ: вторая встреча
    унаследовала бы доставку и номер версии первой, а её протокол затёр бы первый."""
    prev = (previous_meta or {}).get("order") if isinstance(previous_meta, dict) else None
    prev_id = str(prev.get("id") or "") if isinstance(prev, dict) else ""
    if prev_id and prev_id != str(order.get("id") or ""):
        raise PortalOrderError(
            "portal_order_conflict",
            "за эту дату серии уже восстановлена встреча наряда %s: файлы протокола у них "
            "общие, вторая затёрла бы первую — не запускаю, разбирать вручную" % prev_id,
        )


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
    """Номер версии результата по счёту бота — та же функция, что у перевыпуска по
    правке (`portal_versions.next_version`, ветка восстановления): выше уже
    поставленных в очередь результатов этого наряда (не «бот не пришёл») и отметки
    `portal_version` в meta; ничего ещё не уходило — `result_version` наряда (как у
    `collector._portal_enqueue_result`); занятый ключ пропускаем."""
    from lib import portal_versions  # noqa: PLC0415

    return portal_versions.next_version(
        str(order.get("id") or ""), meta=meta, order=order, recovery=True)


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
    тело — `portal_ingest.build_payload`, как у `collector._portal_enqueue_result`;
    время и присутствие — по тому же правилу, что у перевыпуска по правке
    (`feedback_reissue._portal_meeting_fields`). Портал не принял — очередь повторяет
    сама, как у обычной встречи.

    Возвращает сводку для итоговой строки: `status` (`queued` | `disabled` | `error` |
    `duplicate`), `version`. Не бросает: PDF владельцу к этому моменту уже ушёл, и
    итоговая строка обязана напечататься."""
    try:
        from lib import feedback_reissue, portal_delivery, portal_ingest  # noqa: PLC0415

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
            **feedback_reissue._portal_meeting_fields(meta, order),
            telegram_delivery=feedback_reissue._recovery_telegram_delivery(telegram_status),
            order_status=order.get("status"),
            revision_of=(version - 1) if version > 1 else None,
            edits=[],
        )
        key, created = portal_delivery.enqueue(
            order_id=order_id, meeting_id=order.get("meeting_id"), version=version,
            session_uid=sid, company=order.get("company"), payload=payload, sources={},
        )
    except Exception as e:  # noqa: BLE001
        logger.error("[portal] результат НЕ поставлен в очередь портала (%s)", type(e).__name__)
        return {"status": "error", "error": type(e).__name__}
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


def rebuild_hint(transcript_path) -> str:
    """Как пересобрать портальную встречу по готовой расшифровке: только обёрткой — она
    подключит знания компании и поставит протокол в карточку. Ручная команда
    пересборки боту соберёт без компании и в карточку не отдаст."""
    return ("встреча портала — пересобрать по готовой расшифровке тем же запуском обёртки "
            "с «--kind transcript %s --force» (те же --series и --date): так протокол "
            "соберётся со знаниями компании и встанет в карточку; ручная команда пересборки "
            "боту здесь не годится — соберёт без компании и в карточку не отдаст"
            % transcript_path)


_PORTAL_NOTES = {
    "disabled": "в карточку не ушло: отправка в портал выключена (ENABLE_PORTAL_ORDERS)",
    "error": "в карточку не ушло: очередь портала не приняла версию — смотри журнал обёртки",
    "duplicate": "в карточку не ушло: такая версия в очереди портала уже есть — смотри журнал",
}


def _owner_notice(series: str, date: str, portal: dict, degraded_note: Optional[str]) -> str:
    """Строка владельцу ПОСЛЕ PDF: что стало с карточкой портала (вместо «перешли в
    группу сам» — у портальной встречи протокол людям отдаёт карточка)."""
    head = f"♻️ Восстановил протокол встречи «{series}» {date} из присланной записи"
    if portal.get("status") == "held":
        head += " — но PDF до тебя не дошёл."
    else:
        head += " — PDF выше, в групповой чат не отправлял."
    if portal.get("status") == "queued":
        card = ("В карточку портала поставлена версия %d; правка ответом на этот PDF даст "
                "следующую." % int(portal["version"]))
    else:
        card = "⚠️ В карточку портала НЕ поставлено: %s." % portal.get("note", "причина в журнале")
    return head + "\n" + (degraded_note + "\n" if degraded_note else "") + card


def deliver_to_portal(
    *, order: dict, meta: dict, meta_path: Optional[Path], sid: str,
    protocol_path: Path, transcript_path: Path, delivered_ok: bool,
    series: str = "", date: str = "", degraded_note: Optional[str] = None,
) -> dict:
    """Протокол портальной встречи — в карточку очередью доставки портала, как у
    обычной встречи (повторы при отказе портала — забота очереди), отметка версии в
    meta доставки и строка владельцу, что с карточкой.

    Людям — не раньше, чем владельцу: PDF до владельца не дошёл → в очередь не ставим
    (`held`), итог подсказывает повтор."""
    if not delivered_ok:
        portal = {"status": "held",
                  "note": "PDF до владельца не дошёл — в карточку не отправлял; " + rebuild_hint(
                      transcript_path)}
        logger.error("[portal] PDF владельцу не доставлен — в очередь портала не ставлю")
    else:
        portal = enqueue_portal_result(
            order=order, meta=meta, sid=sid, protocol_path=protocol_path,
            transcript_path=transcript_path, telegram_status="sent")
        if portal.get("status") == "queued":
            remember_portal_version(meta, meta_path, int(portal["version"]))
        elif portal.get("status") in _PORTAL_NOTES:
            portal["note"] = _PORTAL_NOTES[portal["status"]]
    try:
        from lib import notify  # noqa: PLC0415

        notify.push_via_notarius(_owner_notice(series, date, portal, degraded_note))
    except Exception as e:  # noqa: BLE001
        logger.warning("[portal] строка владельцу про карточку не ушла (%s)", type(e).__name__)
    return portal


def redeliver_after_force(
    *, meta: dict, meta_path: Path, protocol_path: Path, sid: str, replaced: list,
) -> dict:
    """Повтор с --force по уже доставленной встрече: `deliver_protocol` пропустил
    отправку (идемпотентность по `delivered`), а текст новый — шлём владельцу
    ревизией (`redeliver_revised_protocol`: «🔁 Что изменилось» + PDF, отметка в
    meta). Прежний текст — копия, снятая перед --force. Тот же текст → `skipped`
    (у владельца ровно он)."""
    name = Path(protocol_path).name
    old_text = next((_read_text(Path(p)) for p in replaced or [] if Path(p).name == name), "")
    try:
        from lib import llm_postprocess  # noqa: PLC0415

        res = llm_postprocess.redeliver_revised_protocol(
            dict(meta), old_text, _read_text(protocol_path),
            meta_json_path=Path(meta_path), meeting_sid=sid)
    except Exception as e:  # noqa: BLE001
        logger.warning("[deliver] новая версия владельцу не ушла (%s)", type(e).__name__)
        return {"status": "error", "error": type(e).__name__}
    if res.get("status") in ("no-change", "skipped"):
        return {"status": "skipped", "reason": str(res.get("status"))}
    return res


def meeting_times(order: dict, *, audio_s: Optional[float], transcript_path) -> dict:
    """`startTs`/`endTs`/`durationS` встречи для meta доставки: начало — из наряда (с
    поясом), длительность — длина записи, иначе по последней метке времени
    расшифровки, иначе длительность наряда. Без них карточка «бот не пришёл» считает
    длительность до момента приёма (дни) — и перевыпуск по правке их повторяет."""
    start = order_start(order)
    duration = int(round(audio_s)) if audio_s and audio_s > 0 else None
    duration = duration or transcript_span_s(transcript_path)
    if not duration:
        try:
            duration = int(order.get("duration_minutes") or 0) * 60 or None
        except (TypeError, ValueError):
            duration = None
    duration = min(duration, 86400) if duration else None
    out: dict = {}
    if start is not None:
        out["startTs"] = start.isoformat()
        if duration:
            out["endTs"] = (start + timedelta(seconds=duration)).isoformat()
    if duration:
        out["durationS"] = duration
    return out


_TS_RE = re.compile(r"^\s*[>\-*•]*\s*[\[(](\d{1,2}):(\d{2})(?::(\d{2}))?[\])]")


def transcript_span_s(transcript_path) -> Optional[int]:
    """Последняя метка времени расшифровки (`[ЧЧ:ММ:СС]`, `(ММ:СС)`) в секундах.
    Текст реплик не читается в журнал — только числа."""
    best = 0
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, TypeError):
        return None
    for line in lines:
        m = _TS_RE.match(line)
        if not m:
            continue
        a, b, c = int(m.group(1)), int(m.group(2)), m.group(3)
        best = max(best, a * 3600 + b * 60 + int(c) if c is not None else a * 60 + b)
    return best or None


def prepare_delivery_meta(meta: dict, previous: Optional[dict], order: Optional[dict], *,
                          audio_s: Optional[float], transcript_path, present: list) -> dict:
    """Meta ДОСТАВКИ портальной встречи (`recovered/<sid>.meta.json`) — то, что потом
    повторит перевыпуск по правке: время встречи из наряда, «кто был» (`--participants`
    — люди, бывшие на встрече со слов владельца) и счёт версий портала прошлого прогона
    того же наряда (повтор с --force — новая версия, а не дубль)."""
    if not order:
        return meta
    meta.update(meeting_times(order, audio_s=audio_s, transcript_path=transcript_path))
    if present:
        meta["participants"] = list(present)
    if not isinstance(previous, dict) or not previous.get("portal_version"):
        return meta
    prev_order = previous.get("order")
    prev_id = str(prev_order.get("id") or "") if isinstance(prev_order, dict) else ""
    # Без штампа наряда — та же встреча, записанная до починки (версии слали руками);
    # чужой наряд сюда не доходит — его отвергла `check_previous_recovery`.
    if not prev_id or prev_id == str(order.get("id")):
        meta["portal_version"] = previous["portal_version"]
    return meta


def preflight(series: str, explicit: Optional[str], *, date: Optional[str] = None,
              requested_mode: str = "personal", mode: str = "personal",
              previous_meta: Optional[dict] = None) -> tuple:
    """Всё, что проверяется ДО распознавания: наряд (`resolve_portal_order`), путь и
    прошлое восстановление той же серии и даты. Возвращает (наряд | None, путь).

    Портальная встреча — только путь А (лично владельцу): путь Б по наряду разослал
    бы PDF людям по маршруту наряда мимо проверки владельца. `auto`, выбравший путь Б
    по серии с группой, переводится на путь А; явный `standard` — отказ."""
    order = resolve_portal_order(series, explicit, date=date)
    if order is None:
        return None, mode
    if mode == "standard":
        if requested_mode != "auto":
            raise PortalOrderError(
                "portal_order_personal_only",
                "портальную встречу восстанавливаю только лично владельцу (--mode personal): "
                "в карточку протокол уйдёт очередью портала",
            )
        logger.info("[portal] встреча портала — путь А вместо группы серии")
        mode = "personal"
    check_previous_recovery(previous_meta, order)
    return order, mode


def summary_facts(series: str, order: Optional[dict]) -> dict:
    """Поля итоговой строки обёртки: `company`, `knowledge_files` (+ `portal_order`)."""
    facts = knowledge_facts({"series": series, **portal_company_fields(order)})
    if order is not None:
        facts["portal_order"] = order.get("id")
    return facts
