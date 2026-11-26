"""The Google setup guide: how a user makes their own Google Cloud project and OAuth client for Pisa's Google
connector (Gmail and Calendar), in English or Italian.

Each user owns their project and client: Pisa (the project) never has a shared one, and never receives the
user's keys, tokens or data. The best way is Google Auth Platform's Personal mode (project and client in one
step, no verification, no 7-day expiry, only the creating account can authorize); the fallbacks are an
External app "In production" left unverified (a warning screen), then External "Testing" (authorize again
every 7 days). The client is a Web application whose redirect URI is the user's own server.

The last step is optional and apart from the sign-in: a Google Maps API key for places and travel times
(Places API (New) and Routes API enabled in the same project, a key restricted to those two). It is an API key
pasted in Pisa, never an OAuth scope: Pisa never asks the Google account for Google Cloud access.

`guide` builds the steps for the app and for `pisa google setup`; `redirect_uri` is the callback address both
use. No I/O here."""

import ipaddress
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote, urlsplit

from pisa.domain import locale

CALLBACK_PATH = "/oauth/google/callback"

# Google Auth Platform's Personal-mode onboarding: project and OAuth client in one flow
# (support.google.com/cloud/answer/15544987)
PERSONAL_MODE_URL = "https://console.cloud.google.com/auth/overview/personal"
# Google Auth Platform's Data Access page, where the scopes are added
# (developers.google.com/workspace/guides/configure-oauth-consent)
DATA_ACCESS_URL = "https://console.cloud.google.com/auth/scopes"
# Google Auth Platform's Clients page, for the fallback ways (support.google.com/cloud/answer/15549257)
CLIENTS_URL = "https://console.cloud.google.com/auth/clients"
API_LIBRARY_URL = "https://console.cloud.google.com/apis/library"
GMAIL_API = "gmail.googleapis.com"
CALENDAR_API = "calendar-json.googleapis.com"
# Google Maps for places and travel times (optional): an API key, never a scope of the sign-in
PLACES_API = "places.googleapis.com"  # Places API (New)
ROUTES_API = "routes.googleapis.com"
CREDENTIALS_URL = "https://console.cloud.google.com/apis/credentials"
BILLING_URL = "https://console.cloud.google.com/billing"
USER_DATA_POLICY_URL = "https://developers.google.com/terms/api-services-user-data-policy"


def redirect_uri(addresses: Sequence[str]) -> str | None:
    """The OAuth callback on this server: the first https entry of PISA_ADDRESSES that Google accepts as a
    redirect host (a name, not a raw IP address nor localhost), as scheme://host[:port] plus the callback
    path. None when there is none."""
    for address in addresses:
        try:
            parts = urlsplit(address.strip())
            host, port = parts.hostname, parts.port
        except ValueError:
            continue
        if parts.scheme.lower() != "https" or not host or host == "localhost" or _is_ip(host):
            continue
        netloc = f"{host}:{port}" if port is not None and port != 443 else host
        return f"https://{netloc}{CALLBACK_PATH}"
    return None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _with_project(url: str, project: str | None) -> str:
    return f"{url}?project={quote(project, safe='')}" if project else url


_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "https.title": "Give your server an https address",
        "https.body": (
            "Google sends you back to your own server after you sign in, and it accepts only an https "
            "address with a name (not a raw IP address). Add one to PISA_ADDRESSES: your tailnet name "
            "(https://<name>.<tailnet>.ts.net) or your own domain, then open this guide again. The phone "
            "must reach that address when you connect."
        ),
        "client.title": "Create your OAuth client in Personal mode",
        "client.body": (
            "Open Google Auth Platform's Personal mode page, signed in with the Google account whose mail "
            "and calendar Pisa will read. It creates a project for you (or pick one of yours). Choose Web "
            "application, add the redirect URI below as an authorized redirect URI, accept the terms and "
            "continue. Copy the Client ID and the Client secret at once: Google shows the secret only once."
            " In Personal mode only this account can authorize the client, there is no verification and no "
            "7-day expiry. If Personal mode is not offered, or refuses the scopes or the redirect URI: "
            "create a Web application client from the Clients page "
            "(https://console.cloud.google.com/auth/clients) with an External audience, and publish the app"
            " In production without verification (Google may ask for a homepage and a privacy policy URL on"
            " the Branding page). As a last resort, leave it in Testing with your account as a test user."
        ),
        "client.blocked": (
            "This step can't be finished yet: the redirect URI needs your server's https address (step 1)."
        ),
        "gmail_api.title": "Enable the Gmail API",
        "gmail_api.body": (
            "Open the Gmail API in your project's API Library and select Enable (skip it if it is already "
            "on)."
        ),
        "calendar_api.title": "Enable the Google Calendar API",
        "calendar_api.body": (
            "Open the Google Calendar API in your project's API Library and select Enable (skip it if it is"
            " already on)."
        ),
        "scopes.title": "Add the scopes",
        "scopes.body": (
            "On the Data Access page, select Add or remove scopes, paste these scopes and save. Pisa needs "
            "the reading ones (Gmail read-only, Calendar read-only, the calendars Pisa creates); without "
            "changing your events and writing drafts, Pisa only reads."
        ),
        "paste.title": "Paste the Client ID and Client secret into Pisa",
        "paste.body": (
            "In the PISA app, open Settings, Connections, Google, and paste the Client ID and the Client "
            "secret. They go only to your own server, which keeps them encrypted."
        ),
        "connect.title": "Connect Google",
        "connect.body": (
            "Tap Connect Google, sign in with the same Google account and accept the consent screen. In "
            "Personal mode that's all. With an app In production but unverified, Google first shows a "
            "warning that it hasn't verified the app: it is your own app, so continue. In Testing, Google "
            "ends the authorization after 7 days and you connect again each week. Finish the Google sign-in "
            "within 10 minutes, or tap Connect Google again. If Google answers redirect_uri_mismatch, tap "
            '"error details" (or run pisa google setup on the server) to see the exact redirect URI: the '
            "OAuth client must be a Web application, with that URI pasted exactly as an authorized "
            "redirect URI; then wait about 5 minutes for Google to apply it."
        ),
        "maps_key.title": "Optional: a Google Maps API key for places and travel times",
        "maps_key.body": (
            "Only if you want Google Maps for place search, place details and travel times (paid above "
            "Google's free monthly allowance; Apple Maps and OpenStreetMap need no Google key). In the same "
            "project, turn on billing ({billing}), enable the Places API (New) ({places}) and the Routes API "
            "({routes}), then on the Credentials page create an API key and restrict it to those two APIs. "
            "It is an API key, not a permission of your Google sign-in: Pisa never asks your account for "
            "access to Google Cloud. Paste the key in the PISA app (Settings, Connections, Google Maps) and "
            "give your OK to its monthly cap there: Pisa calls Google Maps only after it."
        ),
        "legal.yours": (
            "Your Google project and OAuth client are yours: you are their developer under Google's API "
            "Services User Data Policy (https://developers.google.com/terms/api-services-user-data-policy)."
        ),
        "legal.never": (
            "PISA (the project) never receives your keys, tokens or data: they stay on your server, "
            "encrypted."
        ),
        "legal.terms": "Google's terms apply to your project.",
        "legal.name": "Don't use the PISA name or logo as the app name on your consent screen.",
    },
    "it": {
        "https.title": "Dai al tuo server un indirizzo https",
        "https.body": (
            "Dopo l'accesso Google ti rimanda al tuo server, e accetta solo un indirizzo https con un nome "
            "(non un indirizzo IP). Aggiungine uno a PISA_ADDRESSES: il nome nella tua tailnet "
            "(https://<nome>.<tailnet>.ts.net) o un tuo dominio, poi riapri questa guida. Quando colleghi "
            "Google, il telefono deve raggiungere quell'indirizzo."
        ),
        "client.title": "Crea il tuo client OAuth in modalità personale",
        "client.body": (
            "Apri la pagina della modalità personale (Personal mode) di Google Auth Platform, con l'account"
            " Google di cui Pisa leggerà posta e calendario. Crea un progetto per te (o scegline uno tuo). "
            "Scegli Applicazione web (Web application), aggiungi l'URI di reindirizzamento qui sotto tra "
            "gli URI di reindirizzamento autorizzati, accetta i termini e continua. Copia subito l'ID "
            "client e il client secret: Google mostra il secret una volta sola. In modalità personale solo "
            "questo account può autorizzare il client, non c'è verifica e non scade dopo 7 giorni. Se la "
            "modalità personale non è disponibile, o rifiuta gli ambiti o l'URI di reindirizzamento: crea "
            "un client Applicazione web dalla pagina Client (https://console.cloud.google.com/auth/clients)"
            " con pubblico Esterno, e pubblica l'app In produzione senza verifica (Google può chiedere "
            "l'indirizzo di una home page e di una informativa sulla privacy nella pagina Branding). Come "
            "ultima scelta, lasciala In test con il tuo account tra gli utenti di test."
        ),
        "client.blocked": (
            "Per ora non puoi completare questo passo: l'URI di reindirizzamento richiede l'indirizzo https"
            " del tuo server (passo 1)."
        ),
        "gmail_api.title": "Attiva la Gmail API",
        "gmail_api.body": (
            "Apri la Gmail API nella Libreria API del tuo progetto e seleziona Abilita (salta se è già "
            "attiva)."
        ),
        "calendar_api.title": "Attiva la Google Calendar API",
        "calendar_api.body": (
            "Apri la Google Calendar API nella Libreria API del tuo progetto e seleziona Abilita (salta se "
            "è già attiva)."
        ),
        "scopes.title": "Aggiungi gli ambiti",
        "scopes.body": (
            "Nella pagina Accesso ai dati (Data Access), seleziona Aggiungi o rimuovi ambiti, incolla "
            "questi ambiti e salva. A Pisa servono quelli di lettura (Gmail in sola lettura, Calendar in "
            "sola lettura, i calendari creati da Pisa); senza quelli per modificare i tuoi eventi e "
            "scrivere bozze, Pisa legge soltanto."
        ),
        "paste.title": "Incolla l'ID client e il client secret in Pisa",
        "paste.body": (
            "Nell'app PISA, apri Impostazioni, Collegamenti, Google, e incolla l'ID client e il client "
            "secret. Vanno solo al tuo server, che li conserva cifrati."
        ),
        "connect.title": "Collega Google",
        "connect.body": (
            "Tocca Collega Google, accedi con lo stesso account Google e accetta la schermata di consenso. "
            "In modalità personale è tutto. Con un'app In produzione ma non verificata, Google mostra prima"
            " un avviso che non ha verificato l'app: è la tua app, quindi continua. In test, Google chiude "
            "l'autorizzazione dopo 7 giorni e la ricolleghi ogni settimana. Completa l'accesso a Google "
            "entro 10 minuti, o tocca di nuovo Collega Google. Se Google risponde redirect_uri_mismatch, "
            "tocca \"dettagli dell'errore\" (o esegui pisa google setup sul server) per vedere l'URI di "
            "reindirizzamento esatto: il client OAuth deve essere un'Applicazione web (Web application), con "
            "quell'URI incollato esattamente tra gli URI di reindirizzamento autorizzati; poi attendi circa "
            "5 minuti che Google lo applichi."
        ),
        "maps_key.title": "Facoltativo: una chiave API di Google Maps per luoghi e tempi di viaggio",
        "maps_key.body": (
            "Solo se vuoi Google Maps per la ricerca dei luoghi, i dettagli dei posti e i tempi di viaggio "
            "(a pagamento oltre la quota mensile gratuita di Google; Apple Maps e OpenStreetMap non chiedono "
            "chiavi Google). Nello stesso progetto attiva la fatturazione ({billing}), abilita Places API "
            "(New) ({places}) e Routes API ({routes}), poi nella pagina Credenziali crea una chiave API e "
            "limitala a queste due API. È una chiave API, non un permesso del tuo accesso a Google: Pisa non "
            "chiede mai al tuo account l'accesso a Google Cloud. Incolla la chiave nell'app PISA "
            "(Impostazioni, Collegamenti, Google Maps) e dai lì il tuo OK al tetto mensile: Pisa chiama "
            "Google Maps solo dopo."
        ),
        "legal.yours": (
            "Il tuo progetto Google e il tuo client OAuth sono tuoi: ne sei lo sviluppatore secondo le "
            "norme di Google sui dati degli utenti dei servizi API "
            "(https://developers.google.com/terms/api-services-user-data-policy)."
        ),
        "legal.never": (
            "PISA (il progetto) non riceve mai le tue chiavi, i tuoi token o i tuoi dati: restano sul tuo "
            "server, cifrati."
        ),
        "legal.terms": "Al tuo progetto si applicano i termini di Google.",
        "legal.name": (
            "Non usare il nome o il logo di PISA come nome dell'app nella tua schermata di consenso."
        ),
    },
}

LEGAL_KEYS = ("legal.yours", "legal.never", "legal.terms", "legal.name")


def _step(
    text: dict[str, str], step_id: str, url: str | None, copy: str | None, body: str | None = None
) -> dict[str, Any]:
    return {
        "id": step_id,
        "title": text[f"{step_id}.title"],
        "body": body if body is not None else text[f"{step_id}.body"],
        "url": url,
        "copy": copy,
    }


def guide(
    redirect_uri: str | None, scopes: Sequence[str], lang: str, project: str | None = None
) -> dict[str, Any]:
    """The setup guide: the redirect URI, the scopes, the steps in order (each with a stable id, a title, a
    body, the page to open or None, and the text to copy or None) and the legal notes, in `lang`.

    Without a redirect URI (no https address on the server) the first step is "https", and the client step
    says it can't be finished yet. `project`, when known, is added to the API Library, Data Access and
    Credentials links. The last step (`maps_key`, optional) is the Google Maps API key: its page is the
    Credentials page, its text to copy the two APIs the key is restricted to."""
    text = locale.pick(_TEXT, lang)
    scopes = list(scopes)
    steps: list[dict[str, Any]] = []
    client_body = text["client.body"]
    if redirect_uri is None:
        steps.append(_step(text, "https", None, None))
        client_body = text["client.blocked"] + " " + client_body
    steps += [
        _step(text, "client", PERSONAL_MODE_URL, redirect_uri, client_body),
        _step(text, "gmail_api", _with_project(f"{API_LIBRARY_URL}/{GMAIL_API}", project), None),
        _step(text, "calendar_api", _with_project(f"{API_LIBRARY_URL}/{CALENDAR_API}", project), None),
        _step(text, "scopes", _with_project(DATA_ACCESS_URL, project), "\n".join(scopes)),
        _step(text, "paste", None, None),
        _step(text, "connect", None, None),
        _step(
            text,
            "maps_key",
            _with_project(CREDENTIALS_URL, project),
            "\n".join((PLACES_API, ROUTES_API)),
            text["maps_key.body"].format(
                billing=BILLING_URL,
                places=_with_project(f"{API_LIBRARY_URL}/{PLACES_API}", project),
                routes=_with_project(f"{API_LIBRARY_URL}/{ROUTES_API}", project),
            ),
        ),
    ]
    return {
        "redirect_uri": redirect_uri,
        "scopes": scopes,
        "steps": steps,
        "legal": [text[key] for key in LEGAL_KEYS],
    }
