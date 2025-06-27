from textual_serve.server import Server
from dotenv import load_dotenv
import socket
import os

def get_local_ip():
    """
    Ermittelt die lokale IP-Adresse des Rechners.
    """
    s = None
    try:
        # Erstellt ein UDP-Socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Verbindet sich mit einer öffentlichen Adresse (keine Daten werden gesendet)
        # Google's öffentlicher DNS-Server wird hier verwendet
        s.connect(("8.8.8.8", 80))
        # Ruft die IP-Adresse des Sockets ab
        ip = s.getsockname()[0]
    except Exception as e:
        print(f"Fehler beim Ermitteln der IP-Adresse: {e}")
        # Fallback auf gethostname(), falls die Verbindung fehlschlägt
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except socket.gaierror:
            ip = "127.0.0.1"
    finally:
        if s:
            s.close()
    return ip

# Lädt Umgebungsvariablen aus der .env-Datei
load_dotenv()

# Versucht, die IP-Adresse aus der .env-Datei zu lesen
# os.getenv() gibt None zurück, wenn die Variable nicht gesetzt ist
webserver_ip = os.getenv("WEBSERVER_IP")

# Prüft, ob eine IP in der .env-Datei konfiguriert wurde
if webserver_ip:
    print(f"IP-Adresse aus .env-Datei geladen: {webserver_ip}")
    lokale_ip = webserver_ip
else:
    # Wenn nicht, wird die IP automatisch ermittelt
    lokale_ip = get_local_ip()
    print(f"IP-Adresse automatisch ermittelt: {lokale_ip}. Falls eine andere IP genutzt werden soll, setzen Sie WEBSERVER_IP in der .env-Datei.")

# Der Server wird mit der ermittelten oder konfigurierten IP gestartet
print("-" * 30)
server = Server("python -m knx-lens")
server.host = lokale_ip
# Stellt sicher, dass die Portnummer im String enthalten ist
server.public_url = f"http://{lokale_ip}:8000" 
server.serve()
print("-" * 30)
