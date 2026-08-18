"""notary/lib — модули post-processing встреч (Ф3+ проекта meeting-notary)."""

# ─────────────────────────────────────────────────────────────────────────────
# ПАКЕТ `notary` ДЛЯ ПРОЦЕССА ФИНАЛИЗАЦИИ (ремонт 18.08.2026)
#
# ЧТО СЛОМАЛОСЬ. Один и тот же код запускают из ДВУХ разных корней:
#   • службы (collector, gateway, listener, scheduler) — `PYTHONPATH=scripts`,
#     корень `notary`, модули зовутся `notary.lib.orders`;
#   • финализация (`finalize-meeting.py`) — команда вида
#     `cd /srv/meeting-notary/vexa/scripts/notary && python finalize-meeting.py`,
#     то есть корень `lib`, модули зовутся `lib.orders`, а пакета `notary`
#     в `sys.path` НЕТ ВООБЩЕ.
# Внутренние импорты `lib/*.py` написаны в первой форме — `from notary.lib
# import orders`, `from notary.cli.registry import ...`, их около полусотни, —
# и в процессе финализации ВСЕ они падали с
# `ModuleNotFoundError: No module named 'notary'`.
#
# ЦЕНА. Не «строчка в логе». `delivery_route.order_for_meeting` трактует сбой
# чтения реестра как «чья это встреча — НЕ ВЫЯСНЕНО» и обязан остановить
# доставку; но вызывающий (`llm_postprocess.deliver_protocol`) ловил тот же сбой
# ещё на импорте, получал `route = None` и уходил в legacy-фолбэк «нет привязки
# → личка владельца». Итог 18.08.2026: протокол встречи «Еженедельная
# координация» уехал владельцу в ЛИЧКУ вместо рабочей группы, а порталу при этом
# отрапортовали `telegram_delivery: sent, mode: group` (`mode` собирает
# collector — у него корень `notary` и импорты живые, он видит НАМЕРЕНИЕ наряда,
# а не факт отправки).
#
# ЧТО ДЕЛАЕМ. Когда пакет загружен как top-level `lib` (значит, мы в процессе
# финализации), поднимаем синтетический пакет `notary`, у которого:
#   • `__path__` смотрит в настоящую папку `scripts/notary` — поэтому
#     `notary.cli`, `notary.auto_vocab`, `notary.meetings_listener` грузятся
#     штатно, без копий кода;
#   • `notary.lib.*` перехватывается нашим finder'ом и отдаёт УЖЕ ЗАГРУЖЕННЫЕ
#     объекты `lib.*` — одно состояние, никаких двойников с раздвоенными кэшами
#     (а `__path__` без перехвата загрузил бы вторые экземпляры).
#
# ПОЧЕМУ НЕ ПРАВКА ПОЛУСОТНИ МЕСТ. Она чинит только те строки, до которых
# дотянулись; следующий такой импорт снова уронит доставку молча — а молчание
# здесь стоит протокола не в том чате.
#
# ПОЧЕМУ БЕЗОПАСНО ДЛЯ СЛУЖБ. Установка идёт только если настоящий `notary`
# НЕ импортируется (проверяем до установки). У служб он импортируется — и весь
# этот блок не делает ничего; там же этот файл вообще исполняется под именем
# `notary.lib` и выходит по первой проверке.
# ─────────────────────────────────────────────────────────────────────────────


def _install_notary_alias() -> None:
    import sys

    # Корень `notary` (службы) — этот файл и есть `notary.lib`, чинить нечего.
    if __name__ != "lib":
        return

    import importlib
    import importlib.util
    import os
    import types
    from importlib.abc import Loader, MetaPathFinder
    from importlib.machinery import ModuleSpec

    if any(type(f).__name__ == "_NotaryLibAliasFinder" for f in sys.meta_path):
        return
    try:
        # Настоящий пакет доступен (например, тесты с PYTHONPATH=scripts) —
        # ничего не подменяем: пусть работает штатный импорт.
        if importlib.util.find_spec("notary") is not None:
            return
    except (ImportError, ValueError):
        pass

    notary_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    this_module = sys.modules[__name__]

    class _NotaryLibAliasFinder(MetaPathFinder, Loader):
        """`notary` → папка scripts/notary; `notary.lib[.X]` → уже живые `lib[.X]`."""

        _PREFIX = "notary.lib."

        def __init__(self) -> None:
            # Импортная машинерия (`_init_module_attrs`) перепишет модулю
            # `__name__`/`__spec__` на псевдонимные — запоминаем настоящие и
            # возвращаем в `exec_module` (он вызывается уже после неё). Иначе
            # `lib.orders` начал бы называть себя `notary.lib.orders`, и в
            # логах/трейсбеках всплыло бы второе, несуществующее имя.
            self._restore: dict = {}

        def find_spec(self, fullname, path=None, target=None):
            if fullname == "notary":
                spec = ModuleSpec("notary", self, is_package=True)
                spec.submodule_search_locations = [notary_dir]
                return spec
            if fullname == "notary.lib" or fullname.startswith(self._PREFIX):
                return ModuleSpec(fullname, self)
            return None

        def create_module(self, spec):
            if spec.name == "notary":
                shim = types.ModuleType("notary")
                shim.__path__ = [notary_dir]
                return shim
            if spec.name == "notary.lib":
                module = this_module
            else:
                module = importlib.import_module("lib." + spec.name[len(self._PREFIX):])
            self._restore[id(module)] = (
                module.__name__,
                getattr(module, "__spec__", None),
                getattr(module, "__loader__", None),
            )
            return module

        def exec_module(self, module):
            # Модуль уже исполнен под своим настоящим именем — повторять нечего,
            # остаётся вернуть ему это имя.
            saved = self._restore.pop(id(module), None)
            if saved is not None:
                module.__name__, module.__spec__, module.__loader__ = saved

    # В НАЧАЛО `sys.meta_path`, а не в конец: `notary.lib` иначе нашёл бы
    # штатный PathFinder по `notary.__path__` и загрузил ВТОРЫЕ экземпляры
    # тех же модулей. Перехват сюда не пускает; всё остальное (`notary.cli`
    # и соседи) finder пропускает мимо себя — их грузит PathFinder как обычно.
    sys.meta_path.insert(0, _NotaryLibAliasFinder())


_install_notary_alias()
