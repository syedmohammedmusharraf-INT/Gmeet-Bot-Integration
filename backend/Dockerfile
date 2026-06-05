FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV DISPLAY=:99
ENV PLAYWRIGHT_BROWSERS_PATH=0
ENV PLAYWRIGHT_CHANNEL=chrome

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    x11-utils \
    x11vnc \
    novnc \
    websockify \
    pulseaudio \
    pulseaudio-utils \
    ffmpeg \
    libopus0 \
    dbus \
    libportaudio2 \
    portaudio19-dev \
    libasound2 \
    libasound2-plugins \
    alsa-utils \
    libnss3 \
    libnspr4 \
    libgbm1 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libxss1 \
    libxtst6 \
    libdrm2 \
    libxkbcommon0 \
    libpango-1.0-0 \
    libcairo2 \
    libfontconfig1 \
    libxshmfence1 \
    libgtk-3-0 \
    libgtk-4-1 \
    libgdk-pixbuf-2.0-0 \
    fonts-liberation \
    xdg-utils \
    wget \
    curl \
    ca-certificates \
    gnupg \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

RUN update-ca-certificates

RUN printf 'pcm.!default {\n    type pulse\n}\nctl.!default {\n    type pulse\n}\n' > /etc/asound.conf

RUN wget -q --no-check-certificate https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y -f ./google-chrome-stable_current_amd64.deb \
    && rm google-chrome-stable_current_amd64.deb \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN sed -i 's/\r$//' *.sh 2>/dev/null; chmod +x start.sh

CMD ["./start.sh"]
