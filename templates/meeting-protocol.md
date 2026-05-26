# {{ meeting_title }}

**Дата:** {{ date }}
**Длительность:** {{ duration_human }}
**Участники:** {{ participants_list }}
**Источник:** {{ meeting_url }}
**Запись:** {{ audio_path }}

---

## Транскрипт

> Если в репликах встречается **«Спикер ?»** — это короткий фрагмент, который
> диарезация не привязала ни к одному кластеру. Игнорируй или сверь с записью.

{{ transcript_body }}

---

## Ключевые моменты

_На этой версии протокола блок пустой — заполняется на будущих итерациях LLM-постпроцессингом или вручную._

---

<details>
<summary>Технические данные генерации</summary>

- ASR-модель: {{ asr_model }}
- Диарезация: {{ diarization_model }}
- Маппинг имён — источники: {{ name_mapping_sources }}
- Генератор: notary/finalize-meeting.py @ {{ generated_at }}
- sessionUid: {{ session_uid }}

</details>
