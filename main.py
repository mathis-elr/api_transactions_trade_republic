import os
import json
import websockets
import requests
import hashlib
import uuid
import base64

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

def generate_device_info():
    """Génère dynamiquement un Device Info cohérent au format Base64"""
    device_id = hashlib.sha512(uuid.uuid4().bytes).hexdigest()
    device_info = {
        "stableDeviceId": device_id,
    }
    return base64.b64encode(json.dumps(device_info).encode()).decode()


def get_waf_token_via_api():
    # Remplace par ta vraie clé API ScrapingBee (ou ZenRows)
    SCRAPINGBEE_API_KEY = os.getenv("SCRAPINGBEE_API_KEY")
    TARGET_URL = "https://app.traderepublic.com/"

    # Appel à l'API distante au lieu de lancer un navigateur local
    api_url = f"https://app.scrapingbee.com/api/v1/?api_key={SCRAPINGBEE_API_KEY}&url={TARGET_URL}&render_js=true"

    try:
        response = requests.get(api_url)
        # ScrapingBee renvoie les cookies dans les headers ou dans le JSON
        cookies = response.cookies
        waf_token = cookies.get("aws-waf-token")

        if waf_token:
            print("✅ Token WAF récupéré via API !")
            return waf_token
        else:
            print("❌ Token non trouvé dans la réponse API.")
            return ""

    except Exception as e:
        print(f"❌ Erreur API ScrapingBee: {e}")
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
                            "Type": 0 if transaction.get("subtitle") == "Ordre de vente" else 1,
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

