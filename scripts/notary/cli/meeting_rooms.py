#!/Users/ilarybalka/Projects/meeting-notary/.venv-cli/bin/python
"""meeting-rooms — CLI для справочника постоянных Telemost-переговорок.

Команды: list, add, remove
Реестр: ~/Projects/me/встречи/rooms.yaml
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import click

from notary.cli.registry import (  # noqa: E402
    ROOMS_FILE,
    find_room,
    load_rooms,
    load_watched,
    release_rooms_lock,
    save_rooms,
    validate_room_record,
)


@click.group(help="Справочник постоянных Telemost-переговорок.")
def cli() -> None:
    pass


@cli.command("list", help="Показать все переговорки.")
def cmd_list() -> None:
    data = load_rooms()
    rooms = data.get("rooms", [])
    if not rooms:
        click.echo(f"(пусто) реестр: {ROOMS_FILE}")
        return
    click.echo(f"Переговорки ({len(rooms)}) — {ROOMS_FILE}:\n")
    for r in rooms:
        desc = r.get("description", "")
        click.echo(f"  @{r['name']:<10}  {r['url']}")
        if desc:
            click.echo(f"  {'':<12}  {desc}")
    click.echo("")


@cli.command("add", help="Добавить переговорку интерактивно.")
@click.option("--name", help="Короткое имя (kebab-case латиницей).")
@click.option("--url", help="Полный URL Telemost.")
@click.option("--description", default="", help="Описание (опционально).")
def cmd_add(name: str | None, url: str | None, description: str) -> None:
    data = load_rooms(lock=True)
    saved = False
    try:
        if not name:
            name = click.prompt("Короткое имя (латиницей, например t11)").strip().lower()
        if find_room(name, data):
            raise click.ClickException(f"@{name} уже есть в rooms.yaml")
        if not url:
            url = click.prompt("URL Telemost (https://telemost.yandex.ru/j/...)").strip()
        if not description:
            description = click.prompt("Описание (можно пустое)", default="", show_default=False).strip()

        record = {"name": name, "url": url}
        if description:
            record["description"] = description

        errors = validate_room_record(record)
        if errors:
            for e in errors:
                click.echo(f"  ✗ {e}", err=True)
            raise click.ClickException("Запись не прошла валидацию")

        data["rooms"].append(record)
        save_rooms(data)
        saved = True
        click.echo(f"✓ Добавлено: @{name} → {url}")
    finally:
        if not saved:
            release_rooms_lock()


@cli.command("remove", help="Удалить переговорку по имени.")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Не спрашивать подтверждения.")
def cmd_remove(name: str, yes: bool) -> None:
    data = load_rooms(lock=True)
    saved = False
    try:
        room = find_room(name, data)
        if not room:
            raise click.ClickException(f"@{name} не найдена")

        watched = load_watched()
        ref = f"@{name}"
        using = [w.get("id", "?") for w in watched.get("watched", []) if w.get("room") == ref]
        click.echo(f"Удалю: @{name} → {room.get('url', '')}")
        if using:
            click.echo(f"⚠  Эта комната используется в {len(using)} встречах: {', '.join(using)}")
            click.echo("   После удаления `meeting-watch run` для них упадёт «комната не найдена».")
        if not yes and not click.confirm("Продолжить?", default=False):
            raise click.ClickException("Отменено")
        data["rooms"] = [r for r in data["rooms"] if r.get("name") != name]
        save_rooms(data)
        saved = True
        click.echo(f"✓ Удалено: @{name}")
    finally:
        if not saved:
            release_rooms_lock()


if __name__ == "__main__":
    cli()
