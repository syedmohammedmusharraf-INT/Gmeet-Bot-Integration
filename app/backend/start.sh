#!/bin/bash

echo "===================================================="
echo "  INT Avatar Interview Bot - Container Starting"
echo "===================================================="

echo "[1/6] Starting Xvfb virtual display..."
rm -f /tmp/.X99-lock 2>/dev/null || true
Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +render -noreset &
export DISPLAY=:99
sleep 2
echo "Xvfb started"

echo "[2/6] Starting noVNC on port 6080..."
x11vnc -display :99 -nopw -listen localhost -xkb -forever -shared -bg -o /tmp/x11vnc.log
websockify --web /usr/share/novnc --wrap-mode=ignore 0.0.0.0:6080 localhost:5900 > /tmp/novnc.log 2>&1 &
sleep 2
echo "noVNC ready - http://localhost:6080/vnc.html"

echo "[3/6] Starting PulseAudio..."
pulseaudio --kill 2>/dev/null || killall pulseaudio 2>/dev/null || true
sleep 1
mkdir -p /var/run/pulse
chmod 755 /var/run/pulse
pulseaudio \
    --system --daemonize=no -n \
    --load="module-native-protocol-unix socket=/var/run/pulse/native auth-anonymous=1" \
    --load="module-always-sink" \
    --exit-idle-time=-1 --log-level=error \
    >> /tmp/pulse.log 2>&1 &

export PULSE_SERVER=unix:/var/run/pulse/native
PULSE_UP=0
for i in $(seq 1 10); do
    sleep 1
    if pactl info > /dev/null 2>&1; then PULSE_UP=1; echo "PulseAudio running (after ${i}s)"; break; fi
    echo "   Waiting for PulseAudio... (${i}s)"
done
if [ "$PULSE_UP" -eq 0 ]; then echo "FATAL: PulseAudio failed"; cat /tmp/pulse.log; exit 1; fi

echo "[4/6] Creating virtual audio cables..."
pactl load-module module-null-sink sink_name=VirtualMic sink_properties=device.description=VirtualMic
pactl load-module module-null-sink sink_name=VirtualSpeaker sink_properties=device.description=VirtualSpeaker
pactl load-module module-virtual-source source_name=VirtualMicSource master=VirtualMic.monitor source_properties=device.description=VirtualMicSource
pactl set-default-sink VirtualSpeaker
pactl set-default-source VirtualMicSource
echo "Virtual cables created"
echo "Sinks:";   pactl list sinks short
echo "Sources:"; pactl list sources short

echo "[5/6] Pre-granting Chrome mic permissions..."
CHROME_PROFILE=/tmp/chrome-profile
mkdir -p "$CHROME_PROFILE/Default"
cat > "$CHROME_PROFILE/Default/Preferences" << 'CHROME_PREFS'
{
  "profile": {
    "content_settings": {
      "exceptions": {
        "media_stream_mic": {
          "https://meet.google.com,*": {
            "expiration": "0",
            "last_modified": "13000000000000000",
            "model": 0,
            "setting": 1
          },
          "https://meet.google.com:443,*": {
            "expiration": "0",
            "last_modified": "13000000000000000",
            "model": 0,
            "setting": 1
          }
        }
      },
      "pref_version": 1
    },
    "default_content_setting_values": {
      "media_stream_mic": 1
    }
  },
  "browser": { "has_seen_welcome_page": true }
}
CHROME_PREFS
echo "Chrome profile written to $CHROME_PROFILE"

echo ""
echo "===================================================="
echo "  All systems ready. Starting FastAPI on port 8000..."
echo "  noVNC at: http://localhost:4014/vnc.html"
echo "  API  at:  http://localhost:4000"
echo "===================================================="

export PULSE_SERVER=unix:/var/run/pulse/native
export PULSE_SINK=VirtualMic
export PULSE_SOURCE=VirtualMicSource
export CHROME_PROFILE_DIR=/tmp/chrome-profile
export DISPLAY=:99

exec uvicorn main:app --host 0.0.0.0 --port 8000 --log-level info
