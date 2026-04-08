import asyncio
import os
import requests
import configparser
from flask import Flask, request, abort, jsonify

# Import des fonctions de ton main.py
from main import (
    generate_device_info,
    get_waf_token_with_selenium,
    headers_to_dict,
    fetch_all_transactions
)

app = Flask(__name__)

API_KEY = "entrez_une_clee_api_secrète"

# Un seul endroit pour tout stocker entre les requêtes
state = {
    "process_id": None,
    "headers": {},
    "session_token": None,
    "extract_details": False
}


def check_auth():
    key = request.headers.get('X-API-KEY')
    if key != API_KEY:
        abort(401)


def connexion(phone, pin, headers_to_use):
    print(f"📡 Tentative de connexion pour le numéro : {phone}")
    return requests.post(
        "https://api.traderepublic.com/api/v1/auth/web/login",
        json={"phoneNumber": phone, "pin": pin},
        headers=headers_to_use
    )


def run_configuration_logic():
    config = configparser.ConfigParser()
    config.read("config.ini")

    try:
        phone_number = config.get("secret", "phone_number")
        pin = config.get("secret", "pin")
    except (configparser.NoSectionError, configparser.NoOptionError):
        return None, "Erreur : phone_number ou pin manquants dans config.ini"

    state["extract_details"] = config.getboolean("general", "extract_details", fallback=False)

    # Récupération du WAF
    waf_token = config.get("secret", "waf_token", fallback="")
    if not waf_token:
        waf_token = get_waf_token_with_selenium()

    # Construction des headers dans le state
    state["headers"] = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "x-aws-waf-token": waf_token,
        "x-tr-app-version": "13.40.5",
        "x-tr-device-info": generate_device_info(),
        "x-tr-platform": "web"
    }

    # Tentative de login
    response = connexion(phone_number, pin, state["headers"])

    if response.status_code != 200:
        print(f"Erreur TR ({response.status_code}): {response.text}")
        return None, f"Erreur TR ({response.status_code}): {response.text}"

    return response.json(), None


@app.route('/auth/request-sms', methods=['POST'])
def demande_code_sms():
    login_data, error = run_configuration_logic()

    if error:
        return jsonify({"error": error}), 400

    state["process_id"] = login_data.get("processId")

    return jsonify({
        "status": "sms_sent",
        "countdown": login_data.get("countdownInSeconds")
    })


@app.route('/auth/confirm-sms', methods=['POST'])
def reception_code_sms():
    if not state["process_id"]:
        return jsonify({"error": "Aucune session en cours. Appelez request-sms d'abord."}), 400

    data = request.json
    # On s'assure que le code est bien une string
    code_sms = str(data.get('code')).strip()

    print(f"📩 Validation du code SMS : {code_sms}")

    # Utilisation du process_id et des headers stockés dans state
    resp = requests.post(
        f"https://api.traderepublic.com/api/v1/auth/web/login/{state['process_id']}/{code_sms}",
        headers=state["headers"]
    )

    if resp.status_code != 200:
        print(f"❌ Échec confirmation : {resp.text}")
        return jsonify({"error": "Code invalide ou session expirée", "details": resp.text}), 400

    # Extraction du token de session final
    res_headers = headers_to_dict(resp)
    state["session_token"] = res_headers.get("Set-Cookie", {}).get("tr_session")

    print("✅ Authentification réussie, session stockée.")
    return jsonify({"status": "success", "session_ready": True})


@app.route('/transactions', methods=['GET'])
def get_transactions():
    if not state["session_token"]:
        return jsonify({"error": "Non authentifié. Appelez confirm-sms d'abord."}), 401

    try:
        # Exécution du scraper
        all_data = asyncio.run(fetch_all_transactions(state["session_token"], state["extract_details"]))
        return jsonify(all_data)
    except Exception as e:
        return jsonify({"error": f"Erreur lors de l'extraction : {str(e)}"}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)