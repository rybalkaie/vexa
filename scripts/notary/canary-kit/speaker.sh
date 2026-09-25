#!/bin/sh
# Диктор самопроверки бота (ISS-60, фаза 2, 25.09.2026) — точка входа контейнера.
#
# Запускает самопроверка (scripts/notary/canary.py) в ТОМ ЖЕ образе, что проверяется:
#   docker run ... -v <папка прогона>:/transcripts -v <canary-kit>:/canary:ro \
#       --entrypoint sh <образ бота> /canary/speaker.sh
#
# Экран и звук поднимаются так же, как в entrypoint.sh бота
# (services/vexa-bot/core/entrypoint.sh): Xvfb на :99 и PulseAudio с виртуальным
# микрофоном virtual_mic, который слушает «колонку» tts_sink. Браузер диктора
# берёт микрофон из PulseAudio по умолчанию — ровно как боевой бот.
#
# 🔴 Молчание по умолчанию. tts_sink и virtual_mic заглушены с самого начала:
# диктор физически молчит, пока не получит команду speak, как бы ни вёл себя
# интерфейсный переключатель микрофона Телемоста (в 3.0 он после входа включён).
# Голос включает только speaker.js — на время фразы — и сразу глушит обратно.

set -u

Xvfb :99 -screen 0 1920x1080x24 >/dev/null 2>&1 &
export DISPLAY=:99

pulseaudio --start --log-target=syslog >/dev/null 2>&1 || true
sleep 1

# Тот же набор устройств, что у бота.
pactl load-module module-null-sink sink_name=zoom_sink sink_properties=device.description="ZoomAudioSink" >/dev/null 2>&1 || true
pactl load-module module-null-sink sink_name=tts_sink sink_properties=device.description="TTSAudioSink" >/dev/null 2>&1 || true
pactl load-module module-remap-source master=tts_sink.monitor source_name=virtual_mic source_properties=device.description="VirtualMicrophone" >/dev/null 2>&1 || true
pactl set-default-source virtual_mic >/dev/null 2>&1 || true
# Звук, который играет браузер (голоса звонка), уходит в отдельный «слив», а НЕ в
# tts_sink: иначе чужие голоса вернулись бы в звонок через virtual_mic.
pactl set-default-sink zoom_sink >/dev/null 2>&1 || true
pactl set-sink-mute tts_sink 1 >/dev/null 2>&1 || true
pactl set-source-mute virtual_mic 1 >/dev/null 2>&1 || true

mkdir -p /root
cat > /root/.asoundrc <<'ALSA_EOF'
pcm.!default {
    type pulse
}
ctl.!default {
    type pulse
}
ALSA_EOF

exec node /canary/speaker.js
