# Dockerfile
# Verwenden Sie ein schlankes, modernes Python-Basis-Image
FROM python:3.11-slim

# Legen Sie das Arbeitsverzeichnis fest
WORKDIR /app

# Erstellen Sie einen Nicht-Root-Benutzer und eine Gruppe
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin -c "App User" appuser

# Erstellen Sie Verzeichnisse für Logs und Projektdaten (als Mount-Punkte)
# und weisen Sie dem App-Benutzer die Eigentümerschaft zu
RUN mkdir -p /app/logs /app/project \
    && chown -R appuser:appuser /app

# Kopieren Sie die requirements.txt-Datei zuerst, um das Caching der Docker-Layer zu nutzen
COPY requirements.txt .

# Installieren Sie die Python-Abhängigkeiten
RUN pip install --no-cache-dir -r requirements.txt

# Kopieren Sie alle Ihre Python-Anwendungsskripte
COPY *.py .

# Setzen Sie Berechtigungen für die Skripte und weisen Sie den Besitz zu
RUN chmod +x *.py \
    && chown appuser:appuser *.py

# Wechseln Sie zum Nicht-Root-Benutzer
USER appuser

# Der Startbefehl (CMD) wird in docker-compose.yml für jeden Dienst separat festgelegt