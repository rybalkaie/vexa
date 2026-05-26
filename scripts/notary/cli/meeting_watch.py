#!/Users/ilarybalka/Projects/meeting-notary/.venv-cli/bin/python
"""meeting-watch — CLI для реестра отслеживаемых встреч бота-нотариуса.

Команды: list, add, remove, disable, enable, pause, resume, run
Реестр: ~/Projects/me/встречи/watched.yaml
Pause-флаг: ~/Projects/me/встречи/.pause-until
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import click

from notary.cli.registry import (  # noqa: E402
    PAUSE_FILE,
    WATCHED_FILE,
    find_watched,
    load_rooms,
    load_watched,
    pause_until,
    release_watched_lock,
    resolve_room_to_url,
    save_watched,
    validate_watched_record,
)

DUBAI_TZ = "Asia/Dubai"
DURATION_RE = re.compile(r"^(\d+)([dhm])$", re.IGNORECASE)
BOT_IMAGE = "vexa-bot:notarius-telemost"
DOCKER_NETWORK = "vexa_vexa"
TRANSCRIPTION_URL = "http://172.17.0.1:8083/v1/audio/transcriptions"
TRANSCRIPTS_VOLUME = "~/meeting-notary/_tmp/transcripts:/transcripts"
SSH_HOST = "meeting-notary"


def parse_duration(s: str) -> timedelta:
    m = DURATION_RE.match(s.strip())
    if not m:
        raise click.ClickException(f"Не понял длительность {s!r} (примеры: 7d, 4h, 90m)")
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit == "d":
        return timedelta(days=n)
    if unit == "h":
        return timedelta(hours=n)
    return timedelta(minutes=n)


def fmt_record_short(rec: dict, rooms: dict) -> str:
    enabled = rec.get("enabled", True)
    ignored = rec.get("ignored", False)
    flag = "✓" if enabled else "✗"
    if ignored:
        flag += " 🚫"
    tp = rec.get("type", "?")
    when = ""
    if tp == "google-calendar":
        evt = rec.get('recurring_event_id') or rec.get('event_id') or '?'
        evt_tail = evt if len(evt) <= 24 else "…" + evt[-23:]
        when = f"cal={rec.get('calendar_id', '?')[:30]} id={evt_tail}"
    elif tp == "manual":
        when = f"cron={rec.get('cron', '?')} tz={rec.get('tz', '?')}"
    elif tp == "one-off":
        when = f"dt={rec.get('datetime', '?')}"
    room = rec.get("room", "(из календаря)") or "(из календаря)"
    return f"{flag} [{tp:14}] {rec.get('id', '?'):28} room={room:14} {when}"


@click.group(help="Реестр отслеживаемых встреч для бота-нотариуса.")
def cli() -> None:
    pass


@cli.command("list", help="Показать все встречи в реестре.")
@click.option("--all", "show_all", is_flag=True, help="Включая disabled/ignored.")
def cmd_list(show_all: bool) -> None:
    data = load_watched()
    rooms = load_rooms()
    watched = data.get("watched", [])
    if not watched:
        click.echo(f"(пусто) реестр: {WATCHED_FILE}")
    else:
        click.echo(f"Встречи ({len(watched)}) — {WATCHED_FILE}:\n")
        for w in watched:
            if not show_all and (not w.get("enabled", True) or w.get("ignored", False)):
                continue
            click.echo("  " + fmt_record_short(w, rooms))
        click.echo("")
    pu = pause_until()
    if pu:
        click.echo(f"⏸  Пауза активна до: {pu}  (файл {PAUSE_FILE})")
    else:
        click.echo("▶  Пауза не активна.")


@cli.command("add", help="Добавить встречу. Параметры можно передать флагами или диалогом.")
@click.option("--id", "meeting_id", help="Внутренний id (kebab-case).")
@click.option("--series", help="Имя серии для папки протоколов (kebab-case).")
@click.option("--type", "type_", type=click.Choice(["google-calendar", "manual", "one-off"]))
@click.option("--calendar-id", help="(google-calendar) calendar_id.")
@click.option("--recurring-event-id", help="(google-calendar) recurring_event_id.")
@click.option("--event-id", help="(google-calendar) event_id для разовых событий из календаря.")
@click.option("--cron", help="(manual) cron-выражение, 5 полей.")
@click.option("--tz", default=DUBAI_TZ, show_default=True, help="(manual) IANA timezone.")
@click.option("--datetime", "dt", help="(one-off) ISO 8601 datetime с офсетом.")
@click.option("--duration", "duration_min", type=int, help="(manual/one-off) длительность встречи в минутах.")
@click.option("--room", help="@<name> или прямая ссылка; пусто = из календаря.")
@click.option("--keep-audio", is_flag=True, default=False, help="Сохранять WAV после транскрипции.")
@click.option("--ignored", is_flag=True, default=False, help="Никогда не предлагать в дайджесте.")
@click.option("--participants", multiple=True, help="Имена участников (можно повторять флаг).")
def cmd_add(
    meeting_id: str | None,
    series: str | None,
    type_: str | None,
    calendar_id: str | None,
    recurring_event_id: str | None,
    event_id: str | None,
    cron: str | None,
    tz: str,
    dt: str | None,
    duration_min: int | None,
    room: str | None,
    keep_audio: bool,
    ignored: bool,
    participants: tuple[str, ...],
) -> None:
    data = load_watched(lock=True)
    rooms = load_rooms()

    if not meeting_id:
        meeting_id = click.prompt("id (kebab-case)").strip().lower()
    if find_watched(meeting_id, data):
        raise click.ClickException(f"id '{meeting_id}' уже есть в watched.yaml")

    if not series:
        series = click.prompt("series (имя папки протоколов, kebab-case)", default=meeting_id).strip().lower()
    if not type_:
        type_ = click.prompt(
            "type", type=click.Choice(["google-calendar", "manual", "one-off"])
        )

    record: dict = {"id": meeting_id, "series": series, "type": type_}

    if type_ == "google-calendar":
        if not calendar_id:
            click.echo("calendar_id — попроси Claude найти серию через Google Calendar MCP и подсказать id.")
            calendar_id = click.prompt("calendar_id (например 'primary' или email@... или group ID)").strip()
        record["calendar_id"] = calendar_id
        if event_id:
            record["event_id"] = event_id
        else:
            if not recurring_event_id:
                recurring_event_id = click.prompt("recurring_event_id (id мастер-события серии)").strip()
            record["recurring_event_id"] = recurring_event_id
    elif type_ == "manual":
        if not cron:
            cron = click.prompt("cron (5 полей, например '0 11 * * 3' — среда 11:00)").strip()
        record["cron"] = cron
        record["tz"] = tz
        if duration_min:
            record["duration_minutes"] = duration_min
    else:  # one-off
        if not dt:
            dt = click.prompt("datetime (ISO 8601 с офсетом, напр. 2026-05-27T11:00:00+03:00)").strip()
        record["datetime"] = dt
        record["duration_minutes"] = duration_min or 60

    if room is None:
        room = click.prompt("room (@<name> / прямая ссылка / пусто = из календаря)", default="", show_default=False).strip()
    if room:
        record["room"] = room

    record["enabled"] = True
    record["ignored"] = ignored
    record["keep_audio"] = keep_audio
    record["notify"] = []
    if participants:
        record["expected_participants"] = list(participants)

    errors = validate_watched_record(record, rooms)
    if errors:
        for e in errors:
            click.echo(f"  ✗ {e}", err=True)
        raise click.ClickException("Запись не прошла валидацию")

    data["watched"].append(record)
    save_watched(data)
    click.echo(f"✓ Добавлено: {meeting_id} ({type_})")


@cli.command("show", help="Показать полную запись одной встречи (YAML).")
@click.argument("meeting_id")
def cmd_show(meeting_id: str) -> None:
    data = load_watched()
    rec = find_watched(meeting_id, data)
    if not rec:
        raise click.ClickException(f"id '{meeting_id}' не найден")
    click.echo(yaml.dump(rec, allow_unicode=True, sort_keys=False, default_flow_style=False).rstrip())


EDITABLE_FIELDS = {
    "room": "str",
    "enabled": "bool",
    "ignored": "bool",
    "keep_audio": "bool",
    "calendar_id": "str",
    "recurring_event_id": "str",
    "event_id": "str",
    "cron": "str",
    "tz": "str",
    "datetime": "str",
    "duration_minutes": "int",
    "series": "str",
    "expected_participants": "list",
}


def _parse_field_value(field: str, raw: str) -> object:
    kind = EDITABLE_FIELDS[field]
    if kind == "bool":
        v = raw.strip().lower()
        if v in ("true", "1", "yes", "y", "да"):
            return True
        if v in ("false", "0", "no", "n", "нет"):
            return False
        raise click.ClickException(f"--value для {field}: ожидается true/false, получено {raw!r}")
    if kind == "int":
        try:
            return int(raw)
        except ValueError:
            raise click.ClickException(f"--value для {field}: ожидается целое, получено {raw!r}")
    if kind == "list":
        return [p.strip() for p in raw.split(",") if p.strip()]
    return raw


@cli.command("edit", help="Изменить одно поле записи. --field room --value @t10")
@click.argument("meeting_id")
@click.option("--field", required=True, type=click.Choice(sorted(EDITABLE_FIELDS.keys())))
@click.option("--value", required=True, help="Новое значение. list — через запятую.")
def cmd_edit(meeting_id: str, field: str, value: str) -> None:
    data = load_watched(lock=True)
    saved = False
    try:
        rec = find_watched(meeting_id, data)
        if not rec:
            raise click.ClickException(f"id '{meeting_id}' не найден")
        old = rec.get(field)
        new = _parse_field_value(field, value)
        rec[field] = new
        rooms = load_rooms()
        errors = validate_watched_record(rec, rooms)
        if errors:
            for e in errors:
                click.echo(f"  ✗ {e}", err=True)
            raise click.ClickException("Изменение нарушает валидацию")
        save_watched(data)
        saved = True
        click.echo(f"✓ {meeting_id}.{field}: {old!r} → {new!r}")
    finally:
        if not saved:
            release_watched_lock()


@cli.command("remove", help="Удалить встречу по id.")
@click.argument("meeting_id")
@click.option("--yes", is_flag=True, help="Не спрашивать подтверждения.")
def cmd_remove(meeting_id: str, yes: bool) -> None:
    data = load_watched(lock=True)
    saved = False
    try:
        rec = find_watched(meeting_id, data)
        if not rec:
            raise click.ClickException(f"id '{meeting_id}' не найден")
        click.echo(f"Удалю: {meeting_id} ({rec.get('type')})")
        if not yes and not click.confirm("Продолжить?", default=False):
            raise click.ClickException("Отменено")
        data["watched"] = [w for w in data["watched"] if w.get("id") != meeting_id]
        save_watched(data)
        saved = True
        click.echo(f"✓ Удалено: {meeting_id}")
    finally:
        if not saved:
            release_watched_lock()


def _toggle_enabled(meeting_id: str, value: bool) -> None:
    data = load_watched(lock=True)
    saved = False
    try:
        rec = find_watched(meeting_id, data)
        if not rec:
            raise click.ClickException(f"id '{meeting_id}' не найден")
        rec["enabled"] = value
        save_watched(data)
        saved = True
        click.echo(f"✓ {meeting_id}: enabled={value}")
    finally:
        if not saved:
            release_watched_lock()


@cli.command("disable", help="Временно выключить встречу (enabled=false).")
@click.argument("meeting_id")
def cmd_disable(meeting_id: str) -> None:
    _toggle_enabled(meeting_id, False)


@cli.command("enable", help="Снова включить встречу (enabled=true).")
@click.argument("meeting_id")
def cmd_enable(meeting_id: str) -> None:
    _toggle_enabled(meeting_id, True)


@cli.command("pause", help="Пауза всего планировщика на длительность (например '7d', '4h', '90m').")
@click.argument("duration")
def cmd_pause(duration: str) -> None:
    td = parse_duration(duration)
    until = datetime.now(timezone.utc) + td
    iso = until.isoformat()
    PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_FILE.write_text(iso + "\n", encoding="utf-8")
    click.echo(f"⏸  Пауза до {iso}")


@cli.command("resume", help="Снять паузу.")
def cmd_resume() -> None:
    if PAUSE_FILE.exists():
        PAUSE_FILE.unlink()
        click.echo("▶  Пауза снята.")
    else:
        click.echo("(пауза не была активна)")


@cli.command("run", help="Сформировать команду ручного запуска бота на VPS для встречи.")
@click.argument("meeting_id")
@click.option("--execute", is_flag=True, help="Не печатать, а выполнить ssh-команду сразу.")
def cmd_run(meeting_id: str, execute: bool) -> None:
    data = load_watched()
    rooms = load_rooms()
    rec = find_watched(meeting_id, data)
    if not rec:
        raise click.ClickException(f"id '{meeting_id}' не найден")
    if not rec.get("enabled", True):
        click.echo("⚠  enabled=false — запуск всё равно соберётся, но обычно ты этого не хотел.", err=True)

    url = resolve_room_to_url(rec.get("room"), rooms)
    if not url:
        raise click.ClickException(
            "В записи нет room. Для ручного запуска нужна явная ссылка — "
            "добавь `room: @<name>` или прямую ссылку Telemost."
        )

    session_uid = f"manual-{meeting_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    bot_config_json = json.dumps({
        "platform": "yandex_telemost",
        "meetingUrl": url,
        "botName": "Бот — протокол встречи",
        "sessionUid": session_uid,
        "language": "ru",
        "task": "transcribe",
    }, ensure_ascii=False)

    docker_cmd = (
        f"docker run --rm "
        f"--network {shlex.quote(DOCKER_NETWORK)} "
        f"-v {shlex.quote(TRANSCRIPTS_VOLUME)} "
        f"-e BOT_CONFIG={shlex.quote(bot_config_json)} "
        f"-e TRANSCRIPTION_SERVICE_URL={shlex.quote(TRANSCRIPTION_URL)} "
        f"{shlex.quote(BOT_IMAGE)}"
    )
    ssh_cmd = ["ssh", SSH_HOST, docker_cmd]

    click.echo(f"# Ручной запуск (Ф5-runner ещё не написан):")
    click.echo(f"# id={meeting_id} room={rec.get('room')} url={url}")
    click.echo(" ".join(shlex.quote(a) for a in ssh_cmd))
    if execute:
        click.echo("\n# Выполняю...")
        rc = subprocess.call(ssh_cmd)
        sys.exit(rc)


if __name__ == "__main__":
    cli()
