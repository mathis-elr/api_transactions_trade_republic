# 1. On part d'un Linux léger avec Python 3.11 préinstallé
FROM python:3.11-slim

# 2. On installe Chromium (la version open-source de Chrome) et son driver
# Chromium est natif sur Debian, ça s'installe sans aucune erreur de dépendance
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

# 3. On prépare le dossier de travail
WORKDIR /app

# 4. On copie tes fichiers et on installe tes librairies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 5. On utilise Gunicorn (serveur web de production) au lieu de l'outil de debug Flask
CMD ["sh", "-c", "gunicorn --timeout 120 -b 0.0.0.0:${PORT:-5000} api_transaction:app"]