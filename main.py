import os
import json
import asyncio
import configparser
import websockets
import requests
import pandas as pd
import hashlib
import uuid
import base64
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

def headers_to_dict(response):
    """
    Transforme les en-têtes de réponse HTTP en dictionnaire structuré.

    :param response: Objet de réponse HTTP.
    :return: Dictionnaire contenant les en-têtes structurés.
    """
    extracted_headers = {}
    for header, header_value in response.headers.items():
        parsed_dict = {}
        entries = header_value.split(", ")
        for entry in entries:
            key_value = entry.split(";")[0]
            if "=" in key_value:
                key, value = key_value.split("=", 1)
                parsed_dict[key.strip()] = value.strip()
        extracted_headers[header] = parsed_dict if parsed_dict else header_value
    return extracted_headers

def flatten_and_clean_json(all_data, sep="."):
    """
    Aplatit des données JSON imbriquées et préserve l'ordre des colonnes.

    :param all_data: Liste de dictionnaires JSON à aplatir.
    :param sep: Séparateur utilisé pour les clés aplaties.
    :return: Liste de dictionnaires aplatis et nettoyés.
    """
    all_keys = []  # Utilisé pour conserver l'ordre des colonnes
    flattened_data = []

    def flatten(nested_json, parent_key=""):
        """Aplatit récursivement un JSON imbriqué."""
        flat_dict = {}
        for key, value in nested_json.items():
            new_key = f"{parent_key}{sep}{key}" if parent_key else key
            if isinstance(value, dict):
                flat_dict.update(flatten(value, new_key))
            else:
                flat_dict[new_key] = value

            if new_key not in all_keys:
                all_keys.append(new_key)

        return flat_dict

    for item in all_data:
        flat_item = flatten(item)
        flattened_data.append(flat_item)

    complete_data = [
        {key: item.get(key, None) for key in all_keys} for item in flattened_data
    ]
    return complete_data

def transform_data_types(df):
    """
    Transforme les types de données d'un DataFrame Pandas :
    - Convertit les colonnes de type timestamp en format date français.
    - Formate les montants en valeurs numériques avec séparateur français.

    :param df: DataFrame contenant les données.
    :return: DataFrame transformé.
    """
    timestamp_columns = ["timestamp"]
    for col in timestamp_columns:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%d/%m/%Y")

    amount_columns = [
        "amount.value",
        "amount.fractionDigits",
        "subAmount.value",
        "subAmount.fractionDigits",
    ]
    for col in amount_columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
            df[col] = df[col].apply(
                lambda x: str(x).replace(".", ",") if pd.notna(x) else x
            )
    return df

def generate_device_info():
    """Génère dynamiquement un Device Info cohérent au format Base64"""
    device_id = hashlib.sha512(uuid.uuid4().bytes).hexdigest()
    device_info = {
        "stableDeviceId": device_id,
    }
    return base64.b64encode(json.dumps(device_info).encode()).decode()


def get_waf_token_with_selenium():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    # Force l'utilisation d'un User-Agent réaliste (très important pour le WAF sur Linux)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    # Indique à Selenium où se trouve le binaire Chrome installé par dpkg
    options.binary_location = "/usr/bin/google-chrome"

    try:
        # On laisse Selenium Manager trouver le driver tout seul,
        # mais on lui passe les options configurées pour Linux
        driver = webdriver.Chrome(options=options)

        # --- RESTE DU CODE (Masquage bot) ---
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                })
            """
        })

        print("🌐 Navigation vers Trade Republic pour le token WAF...")
        driver.get("https://app.traderepublic.com/")
        time.sleep(7)  # Un peu plus de temps sur serveur car c'est souvent plus lent

        waf_token = None
        for cookie in driver.get_cookies():
            if "aws-waf-token" in cookie.get("name", ""):
                waf_token = cookie["value"]
                break

        driver.quit()
        return waf_token

    except Exception as e:
        print(f"❌ Erreur Selenium sur Oracle : {e}")
        return ""

async def connect_to_websocket():
    """
    Fonction asynchrone pour établir une connexion WebSocket à l'API de TradeRepublic.

    :return: L'objet WebSocket connecté à l'API de TradeRepublic.
    """
    websocket = await websockets.connect("wss://api.traderepublic.com")
    locale_config = {
        "locale": "fr",
        "platformId": "webtrading",
        "platformVersion": "safari - 18.3.0",
        "clientId": "app.traderepublic.com",
        "clientVersion": "3.151.3",
    }
    await websocket.send(f"connect 31 {json.dumps(locale_config)}")
    await websocket.recv()

    print("✅ Connexion à la WebSocket réussie!\n⏳ Récupération des données...")
    return websocket


async def fetch_transaction_details(websocket, transaction_id, token, message_id):
    payload = {"type": "timelineDetailV2", "id": transaction_id, "token": token}
    message_id += 1
    await websocket.send(f"sub {message_id} {json.dumps(payload)}")
    response = await websocket.recv()
    await websocket.send(f"unsub {message_id}")
    await websocket.recv()

    start_index = response.find("{")
    end_index = response.rfind("}")
    data = json.loads(response[start_index: end_index + 1] if start_index != -1 else "{}")

    details = {"isin": None, "synthèse": {}}

    for section in data.get("sections", []):
        if section.get("type") == "header" and "action" in section:
            details["isin"] = section["action"].get("payload")

        if section.get("title") == "Synthèse":
            for item in section.get("data", []):
                h = item.get("title")
                t = item.get("detail", {}).get("text")
                if h and t:
                    details["synthèse"][h] = t

                    # On descend pour Actions / Prix du titre
                    if h == "Transaction" and "action" in item.get("detail", {}):
                        # On utilise .get() partout pour éviter les "KeyError"
                        sub_payload = item["detail"]["action"].get("payload", {})
                        for sub_sec in sub_payload.get("sections", []):
                            for sub_item in sub_sec.get("data", []):
                                sub_h = sub_item.get("title")
                                # CORRECTION ICI : on sécurise l'accès au texte
                                sub_t = sub_item.get("detail", {}).get("text")

                                if sub_h and sub_t:
                                    details["synthèse"][sub_h] = sub_t

    return details, message_id

async def fetch_all_transactions(token, extract_details):
    """
    Fonction principale qui récupère toutes les transactions via WebSocket et les sauvegarde dans un fichier.

    Cette fonction se connecte à l'API WebSocket de TradeRepublic pour récupérer les informations
    relatives aux transactions de l'utilisateur, soit sous forme de JSON, soit sous forme de CSV.
    Si l'option `details` est activée, elle récupère les détails des transactions supplémentaires.

    Le processus implique l'abonnement à un flux de transactions, la gestion de la pagination,
    la collecte des données et leur sauvegarde dans un fichier à la fin.

    :param token: Token de session pour l'authentification. Il est nécessaire pour valider les requêtes de l'API.
    :param details: Booléen déterminant si des détails supplémentaires sur chaque transaction doivent être récupérés.
                    Si `True`, chaque transaction sera enrichie de données supplémentaires ; sinon, seules les transactions de base seront récupérées.
    :return: Elle sauvegarde les données récupérées dans un fichier (soit JSON, soit CSV) dans le dossier spécifié.
    """
    investissements = []
    flux_bancaires = []
    message_id = 0

    async with await connect_to_websocket() as websocket:
        after_cursor = None
        while True:
            payload = {"type": "timelineTransactions", "token": token}
            if after_cursor:
                payload["after"] = after_cursor

            message_id += 1
            await websocket.send(f"sub {message_id} {json.dumps(payload)}")
            response = await websocket.recv()
            await websocket.send(f"unsub {message_id}")
            await websocket.recv()
            start_index = response.find("{")
            end_index = response.rfind("}")
            response = (
                response[start_index : end_index + 1]
                if start_index != -1 and end_index != -1
                else "{}"
            )
            data = json.loads(response)

            if not data.get("items"):
                break

            if extract_details:
                for transaction in data.get("items", []):

                    event = transaction.get("eventType")
                    if event in ["TRADING_TRADE_EXECUTED", "TRADING_SAVINGSPLAN_EXECUTED", "PEA_SAVINGS_PLAN_PAY_IN", "PEA_DEPOSIT_DEBIT"]:

                        transaction_id = transaction.get("id")
                        details, message_id = await fetch_transaction_details(websocket, transaction_id, token, message_id)

                        # Vérification : Si pas de ligne "Transaction" dans la synthèse, on ignore
                        synth = details.get("synthèse", {})
                        if "Transaction" not in synth:
                            continue

                        try:
                            # On récupère la ligne : "0,000419 × 59 539,96 €"
                            parts = synth["Transaction"].split("×")

                            # Nettoyage de la Quantité
                            raw_qty = parts[0].strip().replace(",", ".")
                            # On supprime TOUS les espaces (normaux et insécables)
                            quantite = float("".join(raw_qty.split()))

                            # Nettoyage du Prix Unitaire
                            raw_prix = parts[1].strip().replace("€", "").replace(",", ".")
                            # On supprime TOUS les espaces et les caractères bizarres
                            prix_u = float("".join(raw_prix.split()))

                        except Exception as e:
                            print(f"⚠️ Erreur de parsing sur {transaction.get('title')}: {e}")
                            quantite, prix_u = None, None

                        frais_raw = synth.get("Frais", "0")
                        frais_clean = 0.0
                        if isinstance(frais_raw, str):
                            if "gratuit" in frais_raw.lower() or frais_raw.strip() == "" or frais_raw.strip() == "0":
                                frais_clean = 0.0
                            else:
                                try:
                                    # Nettoyage pour transformer "1,00 €" en 1.0
                                    frais_clean = float(
                                        frais_raw.replace("€", "").replace(",", ".").replace("\xa0", "").replace(" ",
                                                                                                                 "").strip())
                                except:
                                    frais_clean = 0.0

                        # CONSTRUCTION DU JSON ESSENTIEL
                        clean_entry = {
                            "Id": transaction.get("id"),
                            "Date": transaction.get("timestamp").parse("+0000", "Z"),
                            "Type": "Achat" if transaction.get("subtitle") == "Ordre d'achat" else "Vente" ,  # Achat / Vente
                            "Actif": transaction.get("title"),
                            "ISIN": details.get("isin"),
                            "Prix": prix_u,
                            "Quantite": quantite,
                            "Frais": frais_clean,
                            "Total": abs(float(transaction.get("amount", {}).get("value", 0)))
                        }

                        investissements.append(clean_entry)

                    elif event in ["BANK_TRANSACTION_INCOMING", "BANK_TRANSACTION_OUTGOING"]:
                        valeur_raw = transaction.get("amount", {}).get("value", 0)
                        flux_bancaires.append({
                            "Id": transaction.get("id"),
                            "Date": transaction.get("timestamp").replace("+0000", "Z"),
                            "Type": "Entrant" if valeur_raw > 0 else "Sortant",
                            "Expediteur": transaction.get("title"),
                            "Montant": abs(float(valeur_raw))
                        })
            else:
                investissements.extend(data["items"])

            after_cursor = data.get("cursors", {}).get("after")
            if not after_cursor:
                break

    return {
        "transactions": investissements,
        "fluxBancaires": flux_bancaires
    }


async def profile_cash(token):
    """
    Récupère les informations de profil de l'utilisateur via WebSocket.

    :param token: Le token de session utilisé pour l'authentification.
    :return: Un dictionnaire contenant les informations du profil utilisateur.
    """
    async with await connect_to_websocket() as websocket:
        payload = {"type": "availableCash", "token": token}
        await websocket.send(f"sub 1 {json.dumps(payload)}")
        response = await websocket.recv()

        start_index = response.find("[")
        end_index = response.rfind("]")
        response_data = json.loads(
            response[start_index : end_index + 1]
            if start_index != -1 and end_index != -1
            else "[]"
        )

        if output_format.lower() == "json":
            output_path = os.path.join(
                output_folder, "trade_republic_profile_cash.json"
            )
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(response_data, f, indent=4, ensure_ascii=False)
            print("✅ Données sauvegardées dans 'trade_republic_profile_cash.json'")
        else:
            flattened_data = flatten_and_clean_json(response_data)
            if flattened_data:
                df = pd.DataFrame(flattened_data)
                output_path = os.path.join(
                    output_folder, "trade_republic_profile_cash.csv"
                )
                df.to_csv(output_path, index=False, sep=";", encoding="utf-8-sig")
                print("✅ Données sauvegardées dans 'trade_republic_profile_cash.csv'")

if __name__ == "__main__":
    # Chargement de la configuration
    config = configparser.ConfigParser()
    config.read("config.ini")

    # Variables de configuration
    try:
        phone_number = config.get("secret", "phone_number")
        pin = config.get("secret", "pin")
    except (configparser.NoSectionError, configparser.NoOptionError):
        print("❌ Erreur : Veuillez vérifier que 'phone_number' et 'pin' sont bien renseignés dans config.ini")
        exit()
    
    # Paramètres WAF et Device (optionnels dans le config.ini, générés automatiquement sinon)
    waf_token = config.get("secret", "waf_token", fallback="")
    device_info = config.get("secret", "device_info", fallback="")
    
    output_format = config.get("general", "output_format", fallback="csv")
    output_folder = config.get("general", "output_folder", fallback="out")
    extract_details = config.getboolean("general", "extract_details", fallback=False)
    os.makedirs(output_folder, exist_ok=True)

    if output_format.lower() not in ["json", "csv"]:
        print(f"❌ Le format '{output_format}' est inconnu. Veuillez saisir 'json' ou 'csv'.")
        exit()

    # Si les tokens ne sont pas écrits en dur dans config.ini, on les génère automatiquement
    if not device_info:
        device_info = generate_device_info()
    
    if not waf_token:
        waf_token = get_waf_token_with_selenium()

    headers = {
        "Accept": "*/*",
        "Accept-Language": "fr",
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "Pragma": "no-cache",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "x-aws-waf-token": waf_token,
        "x-tr-app-version": "13.40.5",
        "x-tr-device-info": device_info,
        "x-tr-platform": "web"
    }

    print("📡 Tentative de connexion à l'API Trade Republic...")
    login_response = requests.post(
        "https://api.traderepublic.com/api/v1/auth/web/login",
        json={"phoneNumber": phone_number, "pin": pin},
        headers=headers
    )

    if login_response.status_code != 200:
        print(f"❌ Erreur lors de la connexion à l'API Trade Republic (Code HTTP {login_response.status_code}).")
        print("Le pare-feu Amazon (WAF) a probablement bloqué la requête ou le jeton a expiré.")
        print(f"Détails bruts : {login_response.text}")
        exit()

    try:
        login_data = login_response.json()
    except ValueError:
        print("❌ L'API n'a pas renvoyé de JSON valide.")
        exit()

    process_id = login_data.get("processId")
    countdown = login_data.get("countdownInSeconds")
    
    if not process_id:
        print("❌ Échec de l'initialisation de la connexion. Vérifiez vos identifiants.")
        exit()

    code = input(f"❓ Entrez le code 2FA reçu ({countdown} secondes restantes) ou tapez 'SMS': ")

    if code == "SMS":
        requests.post(
            f"https://api.traderepublic.com/api/v1/auth/web/login/{process_id}/resend",
            headers=headers
        )
        code = input("❓ Entrez le code 2FA reçu par SMS: ")

    verify_response = requests.post(
        f"https://api.traderepublic.com/api/v1/auth/web/login/{process_id}/{code}",
        headers=headers
    )
    
    if verify_response.status_code != 200:
        print(f"❌ Échec de la vérification de l'appareil (Code HTTP {verify_response.status_code}).")
        print(f"Détails bruts : {verify_response.text}")
        exit()

    print("✅ Appareil vérifié avec succès!")

    response_headers = headers_to_dict(verify_response)
    session_token = response_headers.get("Set-Cookie", {}).get("tr_session")
    
    if not session_token:
        print("❌ Token de connexion introuvable. Trade Republic a peut-être changé sa méthode d'authentification.")
        exit()

    print("✅ Token de session récupéré avec succès!")

    asyncio.run(fetch_all_transactions(session_token, extract_details))
    asyncio.run(profile_cash(session_token))