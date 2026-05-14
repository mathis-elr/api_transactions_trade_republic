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
from datetime import datetime

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

    # chemin chrome pour l'instance oracle
    #options.binary_location = "/usr/bin/google-chrome"

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

    # 1. On s'abonne aux détails
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
                    if h == "Transaction" and "action" in item.get("detail", {}):
                        sub_payload = item["detail"]["action"].get("payload", {})
                        for sub_sec in sub_payload.get("sections", []):
                            for sub_item in sub_sec.get("data", []):
                                sub_h = sub_item.get("title")
                                sub_t = sub_item.get("detail", {}).get("text")
                                if sub_h and sub_t:
                                    details["synthèse"][sub_h] = sub_t
    return details, message_id

async def fetch_all_transactions(token, extract_details, dernier_id_enregistre=None):
    investissements = []
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

                    id_courant = transaction.get("id")
                    if id_courant == dernier_id_enregistre:
                        return {"Transactions": investissements}

                    event = transaction.get("eventType")
                    if event in ["TRADING_TRADE_EXECUTED", "TRADING_SAVINGSPLAN_EXECUTED", "PEA_SAVINGS_PLAN_PAY_IN", "PEA_DEPOSIT_DEBIT"]:

                        details, message_id = await fetch_transaction_details(websocket, id_courant, token, message_id)

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

                        clean_entry = {
                            "Id": id_courant,
                            "Date": transaction.get("timestamp").replace("+0000", "Z"),
                            "Type": "Vente" if transaction.get("subtitle") == "Ordre de vente" else "Achat" ,  # Achat / Vente
                            "Actif": transaction.get("title"),
                            "ISIN": details.get("isin"),
                            "Prix": prix_u,
                            "Quantite": quantite,
                            "Frais": frais_clean,
                            "Total": abs(float(transaction.get("amount", {}).get("value", 0)))
                        }

                        investissements.append(clean_entry)
            else:
                investissements.extend(data["items"])

            after_cursor = data.get("cursors", {}).get("after")
            if not after_cursor:
                break

    return {"Transactions": investissements}

