"""The connection guide's words on the server (`pisa setup`), in English and Italian (`domain.locale`). Pure:
no I/O.

Each step says what it asks, why it is needed, what changes with a yes or a no, and where to change it later,
as the app's guide does (the PISA plan, "The connection guide"). Nothing is turned on silently.
"""

from pisa.domain import locale

STEPS = ("place", "ai", "talk", "push", "pair")

TEXT: dict[str, dict[str, str]] = {
    "en": {
        "intro": "Pisa's setup on this server. Each step says what it asks, why, what a yes or a no "
        "changes, and where to change it later. Press Enter to keep what is shown.",
        "asks": "It asks",
        "why": "Why",
        "yes": "If you say yes",
        "no": "If you say no",
        "later": "Change it later",
        "skipped": "Skipped.",
        "done": "Setup done. Run `pisa setup` again at any time.",
        "place.title": "1. Your place in the world",
        "place.asks": "The time zone, the language Pisa speaks and your country.",
        "place.why": "Plan times follow the time zone; Pisa speaks and writes in the language; the country "
        "turns on features that exist only there (public transport feeds, taxi numbers, certified email).",
        "place.yes": "A new language is used from Pisa's next sentence; a new zone or country after a "
        "restart.",
        "place.no": "Pisa keeps the values below.",
        "place.later": "The language: in the app (Settings, Language) or here. The time zone and the "
        "country: PISA_TIMEZONE and PISA_COUNTRY in the server's .env file, then restart Pisa.",
        "place.now": "Now: time zone {tz}, language {language}, country {country}.",
        "place.ask_lang": "Language (en, it)",
        "place.ask_tz": "Time zone (an IANA name, e.g. Europe/London)",
        "place.country_set": "Country: {country}, set when Pisa was installed. To change it, run the "
        "installer again with --force (your passwords and keys are kept).",
        "place.country_unset": "No country is set, so no feature that exists only in one country is on. "
        "To set it, run the installer again on the machine Pisa runs on, with --force (your passwords and "
        "keys are kept) and your country: `bash install.sh --force --country IT` with the PISA_REPO and "
        "PISA_VERSION you installed with (from a checkout: `bash deploy/install.sh --force --country IT`; "
        "on Windows: `.\\install.ps1 --force --country IT`).",
        "place.lang_set": "Language set to {language}.",
        "place.env": "Put these lines in the server's .env file, then restart Pisa:",
        "place.bad_tz": "{value!r} is not a time zone Pisa knows (use a name such as Europe/London).",
        "place.bad_country": "{value!r} is not a two-letter country code.",
        "ai.title": "2. AI models",
        "ai.asks": "Which model answers you: a cloud provider with your key, or your own model server.",
        "ai.why": "Pisa needs a model to understand you and answer. Where the model runs is where your "
        "words go.",
        "ai.yes": "A cloud provider receives the words of each request and charges you for them; your "
        "spending caps apply. Your own server keeps them on your machines and costs nothing per word; "
        "features that need tool calling work only if the model can call tools.",
        "ai.no": "No model: Pisa cannot answer until one is set.",
        "ai.later": "In the app (Settings, AI), or `pisa models set` and `pisa secrets set` here.",
        "ai.now": "Now: the conversation uses {model}.",
        "ai.unset": "Now: no model answers the conversation.",
        "ai.ask_kind": "Choose: cloud, own (your model server), or skip",
        "ai.ask_provider": "Provider id (e.g. openai, anthropic, mistral)",
        "ai.ask_key": "API key (not shown)",
        "ai.key_set": "Key stored (encrypted). Test: {result}",
        "ai.test_models": "{n} models are available.",
        "ai.test_ok": "{detail}",
        "ai.test_unverified": "{detail}",
        "ai.test_failed": "{detail}",
        "ai.ask_server_url": "Your server's address, ending in /v1",
        "ai.ask_server_id": "A short id for it (lowercase letters, digits, dashes)",
        "ai.server_added": "Server {id} added.",
        "ai.ask_model": "Model for the conversation (<provider>:<model>)",
        "ai.probe": "Probing {model} (this can take a minute)...",
        "ai.caps": "It can: {can}. It cannot: {cannot}.",
        "ai.needs_tools": "Without tool calling these features do not work: {features}.",
        "ai.model_set": "The conversation now uses {model}. Other features: `pisa models list`.",
        "ai.slots_set": "Each feature now has its model:",
        "ai.slots_missing": "Left unset, because no model you have a key for fits them: {slots}; they stay "
        "off until you choose one (`pisa models set`, or in the app), and leaving them off is fine.",
        "ai.embed_missing": "memory.embed (memory search: a Google or OpenAI key gives one)",
        "talk.title": "3. Voice talks",
        "talk.asks": "How Pisa talks with you aloud: natural (a live voice model hears you and answers) or "
        "economy (the phone's own voice reads Pisa's answers).",
        "talk.why": "Natural talks need a live voice model, Gemini Live with your Google key; without it the "
        "phone speaks for Pisa.",
        "talk.yes": "Natural: your voice and the conversation go to Google's Gemini Live, as the app's AI "
        "disclosure lists, after you allow it there (or with `pisa consent`).",
        "talk.no": "Economy: your words are written by the phone and Pisa's answers read by it; nothing "
        "else changes.",
        "talk.later": "In the app (Settings, Conversation).",
        "talk.health": (
            "Natural voice talks send everything you say, health included, to Gemini Live (Google): they "
            "need your health consent too, in the app or with `pisa consent --health`."
        ),
        "talk.ask_natural": "Use natural voice talks? (yes/no)",
        "talk.answer_yes": "yes",
        "talk.kept_economy": (
            "Voice talks stay on the phone's own voice (economy). Switch later in the app (Settings, "
            "Conversation)."
        ),
        "talk.natural": "Voice talks are natural: Gemini Live (your Google key).",
        "talk.models": "Gemini Live models in the catalog: {models}.",
        "talk.ask_model": "Gemini Live model",
        "talk.economy": "Voice talks use the phone's own voice (economy): add a Google or xAI key for "
        "natural talks.",
        "push.title": "4. Notifications",
        "push.asks": "How notices reach your phone when Pisa is closed: the Pisa relay, direct, or none.",
        "push.why": "Apple delivers pushes to the App Store app only from a server holding the app's key, "
        "which your server cannot hold.",
        "push.yes": "Relay: notices are sealed to your phone's key; the relay sees only an opaque handle, "
        "the time and size of each push, and that your subscription is valid: never text, names, places or "
        "your server's address. Live Activities through the relay carry only a progress number and a time. "
        "Direct: only for your own build of the app, with your own Apple key.",
        "push.no": "None: notices arrive only while the app is open, and in the morning briefing.",
        "push.later": "The phone chooses in the app (Settings, Notifications); unpairing it deletes its "
        "registration. On the server: PISA_PUSH_RELAY_URL, or PISA_APNS_TEAM_ID, PISA_APNS_TOPIC and `pisa "
        "secrets set-apns` for direct.",
        "push.state": "On this server: relay {relay}, direct {direct}.",
        "push.on": "ready",
        "push.off": "not set",
        "push.ask": "Which will your phone use: relay, direct, or none",
        "push.relay_missing": "Set PISA_PUSH_RELAY_URL in the server's .env file (the relay's https "
        "address), then restart Pisa.",
        "push.direct_missing": "Set PISA_APNS_TEAM_ID and PISA_APNS_TOPIC, then store the key with `pisa "
        "secrets set-apns --key-id <id> < AuthKey.p8`.",
        "push.ready": "Ready: choose it in the app (Settings, Notifications).",
        "push.none": "Nothing to set on the server.",
        # the relay off (`domain.experimental`, push_relay: App Store review mode keeps it off): not offered
        "push.asks.direct": "How notices reach your phone when Pisa is closed: direct, or none.",
        "push.yes.direct": "Direct: only for your own build of the app, with your own Apple key.",
        "push.later.direct": "The phone chooses in the app (Settings, Notifications); unpairing it deletes "
        "its registration. On the server: PISA_APNS_TEAM_ID, PISA_APNS_TOPIC and `pisa secrets set-apns`.",
        "push.state.direct": "On this server: direct {direct}.",
        "push.ask.direct": "Which will your phone use: direct, or none",
        "push.relay_off": "The relay is off on this server (App Store review mode).",
        "talk.economy.gemini": "Voice talks use the phone's own voice (economy): add a Google key for "
        "natural talks.",
        "pair.title": "5. Pairing",
        "pair.asks": "To pair your phone now: Pisa shows a QR code to scan (or a code to type).",
        "pair.why": "Pairing gives the phone its own key; it proves the phone is yours on every connection, "
        "with a short-lived session that ends after an hour.",
        "pair.yes": "The phone scans the code; you compare the 6 digits on both sides and confirm here.",
        "pair.no": "Pair later with `pisa pair <name>`.",
        "pair.later": "`pisa pair <name>` here; revoke a device with `pisa devices revoke`, or from the app.",
        "pair.ask": "Pair a device now? Give its name, or press Enter to skip",
    },
    "it": {
        "intro": "La configurazione di Pisa su questo server. Ogni passo dice cosa chiede, perché, cosa "
        "cambia con un sì o un no, e dove cambiarlo poi. Premi Invio per tenere quello che vedi.",
        "asks": "Chiede",
        "why": "Perché",
        "yes": "Se dici sì",
        "no": "Se dici no",
        "later": "Per cambiarlo poi",
        "skipped": "Saltato.",
        "done": "Configurazione finita. Puoi rilanciare `pisa setup` quando vuoi.",
        "place.title": "1. Dove sei nel mondo",
        "place.asks": "Il fuso orario, la lingua in cui Pisa parla e il tuo paese.",
        "place.why": "Gli orari del piano seguono il fuso; Pisa parla e scrive nella lingua; il paese "
        "accende le funzioni che esistono solo lì (trasporti pubblici, numeri dei taxi, PEC).",
        "place.yes": "Una nuova lingua vale dalla prossima frase di Pisa; un nuovo fuso o paese dopo un "
        "riavvio.",
        "place.no": "Pisa tiene i valori qui sotto.",
        "place.later": "La lingua: nell'app (Impostazioni, Lingua) o qui. Il fuso e il paese: PISA_TIMEZONE "
        "e PISA_COUNTRY nel file .env del server, poi riavvia Pisa.",
        "place.now": "Ora: fuso {tz}, lingua {language}, paese {country}.",
        "place.ask_lang": "Lingua (en, it)",
        "place.ask_tz": "Fuso orario (un nome IANA, per esempio Europe/Rome)",
        "place.country_set": "Paese: {country}, scelto quando hai installato Pisa. Per cambiarlo, "
        "rilancia l'installer con --force (password e chiavi restano).",
        "place.country_unset": "Nessun paese impostato, quindi nessuna funzione che esiste solo in un "
        "paese è accesa. Per impostarlo, rilancia l'installer sulla macchina dove gira Pisa, con --force "
        "(password e chiavi restano) e il tuo paese: `bash install.sh --force --country IT` con PISA_REPO "
        "e PISA_VERSION usati per installare (da una copia del repository: `bash deploy/install.sh "
        "--force --country IT`; su Windows: `.\\install.ps1 --force --country IT`).",
        "place.lang_set": "Lingua impostata: {language}.",
        "place.env": "Metti queste righe nel file .env del server, poi riavvia Pisa:",
        "place.bad_tz": "{value!r} non è un fuso orario che Pisa conosce (usa un nome come Europe/Rome).",
        "place.bad_country": "{value!r} non è un codice di paese di due lettere.",
        "ai.title": "2. Modelli di IA",
        "ai.asks": "Quale modello ti risponde: un fornitore cloud con la tua chiave, o un tuo server di "
        "modelli.",
        "ai.why": "A Pisa serve un modello per capirti e risponderti. Dove gira il modello è dove vanno le "
        "tue parole.",
        "ai.yes": "Un fornitore cloud riceve le parole di ogni richiesta e te le fa pagare; valgono i tuoi "
        "tetti di spesa. Un tuo server le tiene sulle tue macchine e non costa nulla a parola; le funzioni "
        "che usano gli strumenti vanno solo se il modello sa chiamarli.",
        "ai.no": "Nessun modello: Pisa non può risponderti finché non ne imposti uno.",
        "ai.later": "Nell'app (Impostazioni, IA), o qui con `pisa models set` e `pisa secrets set`.",
        "ai.now": "Ora: la conversazione usa {model}.",
        "ai.unset": "Ora: nessun modello risponde nella conversazione.",
        "ai.ask_kind": "Scegli: cloud, own (un tuo server di modelli) o skip",
        "ai.ask_provider": "Id del fornitore (per esempio openai, anthropic, mistral)",
        "ai.ask_key": "Chiave API (non viene mostrata)",
        "ai.key_set": "Chiave salvata (cifrata). Prova: {result}",
        "ai.test_models": "sono disponibili {n} modelli.",
        "ai.test_ok": "la chiave funziona.",
        "ai.test_unverified": "questo fornitore non offre un modo gratuito di verificare una chiave: sarà "
        "verificata alla prima chiamata.",
        "ai.test_failed": "non è riuscita ({detail}).",
        "ai.ask_server_url": "L'indirizzo del tuo server, che finisce in /v1",
        "ai.ask_server_id": "Un id breve (lettere minuscole, cifre, trattini)",
        "ai.server_added": "Server {id} aggiunto.",
        "ai.ask_model": "Modello per la conversazione (<fornitore>:<modello>)",
        "ai.probe": "Provo {model} (può volerci un minuto)...",
        "ai.caps": "Sa fare: {can}. Non sa fare: {cannot}.",
        "ai.needs_tools": "Senza la chiamata di strumenti queste funzioni non vanno: {features}.",
        "ai.model_set": "La conversazione ora usa {model}. Le altre funzioni: `pisa models list`.",
        "ai.slots_set": "Ogni funzione ora ha il suo modello:",
        "ai.slots_missing": "Restano da impostare, perché nessun modello per cui hai una chiave va bene: "
        "{slots}; restano spente finché non ne scegli uno (`pisa models set`, o nell'app), e lasciarle "
        "spente va bene.",
        "ai.embed_missing": "memory.embed (la ricerca nella memoria: una chiave Google o OpenAI ne dà uno)",
        "talk.title": "3. Conversazioni a voce",
        "talk.asks": "Come Pisa ti parla a voce: naturale (un modello vocale dal vivo ti ascolta e "
        "risponde) o economy (la voce del telefono legge le risposte di Pisa).",
        "talk.why": "Le conversazioni naturali richiedono un modello vocale dal vivo, Gemini Live con la tua "
        "chiave Google; senza, è il telefono a parlare per Pisa.",
        "talk.yes": "Naturale: la tua voce e la conversazione vanno a Gemini Live di Google, come elenca "
        "l'informativa sull'IA dell'app, dopo che lo permetti lì (o con `pisa consent`).",
        "talk.no": "Economy: il telefono scrive le tue parole e legge le risposte di Pisa; nient'altro "
        "cambia.",
        "talk.later": "Nell'app (Impostazioni, Conversazione).",
        "talk.health": (
            "Le conversazioni vocali naturali inviano tutto quello che dici, salute compresa, a Gemini Live "
            "(Google): serve anche il tuo consenso per la salute, nell'app o con `pisa consent --health`."
        ),
        "talk.ask_natural": "Usare le conversazioni vocali naturali? (sì/no)",
        "talk.answer_yes": "sì",
        "talk.kept_economy": (
            "Le conversazioni a voce restano sulla voce del telefono (economy). Puoi cambiarlo nell'app "
            "(Impostazioni, Conversazione)."
        ),
        "talk.natural": "Le conversazioni a voce sono naturali: Gemini Live (la tua chiave Google).",
        "talk.models": "Modelli Gemini Live nel catalogo: {models}.",
        "talk.ask_model": "Modello Gemini Live",
        "talk.economy": "Le conversazioni a voce usano la voce del telefono (economy): aggiungi una chiave "
        "Google o xAI per le conversazioni naturali.",
        "push.title": "4. Notifiche",
        "push.asks": "Come le notifiche arrivano al telefono quando Pisa è chiusa: il relay di Pisa, "
        "diretto o nessuno.",
        "push.why": "Apple consegna le notifiche all'app dell'App Store solo da un server che ha la chiave "
        "dell'app, e il tuo server non può averla.",
        "push.yes": "Relay: le notifiche sono sigillate con la chiave del telefono; il relay vede solo un "
        "identificativo opaco, ora e dimensione di ogni notifica e che l'abbonamento è valido: mai testi, "
        "nomi, luoghi o l'indirizzo del tuo server. Le Live Activity attraverso il relay portano solo un "
        "numero di avanzamento e un orario. Diretto: solo per una tua build dell'app, con una tua chiave "
        "Apple.",
        "push.no": "Nessuno: le notifiche arrivano solo con l'app aperta, e nel briefing del mattino.",
        "push.later": "Il telefono sceglie nell'app (Impostazioni, Notifiche); scollegarlo cancella la sua "
        "registrazione. Sul server: PISA_PUSH_RELAY_URL, oppure PISA_APNS_TEAM_ID, PISA_APNS_TOPIC e `pisa "
        "secrets set-apns` per il diretto.",
        "push.state": "Su questo server: relay {relay}, diretto {direct}.",
        "push.on": "pronto",
        "push.off": "non impostato",
        "push.ask": "Quale userà il telefono: relay, direct o none",
        "push.relay_missing": "Imposta PISA_PUSH_RELAY_URL nel file .env del server (l'indirizzo https del "
        "relay), poi riavvia Pisa.",
        "push.direct_missing": "Imposta PISA_APNS_TEAM_ID e PISA_APNS_TOPIC, poi salva la chiave con `pisa "
        "secrets set-apns --key-id <id> < AuthKey.p8`.",
        "push.ready": "Pronto: sceglilo nell'app (Impostazioni, Notifiche).",
        "push.none": "Niente da impostare sul server.",
        "push.asks.direct": "Come le notifiche arrivano al telefono quando Pisa è chiusa: diretto o nessuno.",
        "push.yes.direct": "Diretto: solo per una tua build dell'app, con una tua chiave Apple.",
        "push.later.direct": "Il telefono sceglie nell'app (Impostazioni, Notifiche); scollegarlo cancella "
        "la sua registrazione. Sul server: PISA_APNS_TEAM_ID, PISA_APNS_TOPIC e `pisa secrets set-apns`.",
        "push.state.direct": "Su questo server: diretto {direct}.",
        "push.ask.direct": "Quale userà il telefono: direct o none",
        "push.relay_off": "Il relay è spento su questo server (modalità di revisione App Store).",
        "talk.economy.gemini": "Le conversazioni a voce usano la voce del telefono (economy): aggiungi una "
        "chiave Google per le conversazioni naturali.",
        "pair.title": "5. Collegamento",
        "pair.asks": "Collegare ora il telefono: Pisa mostra un codice QR da inquadrare (o un codice da "
        "digitare).",
        "pair.why": "Il collegamento dà al telefono una sua chiave; prova che il telefono è tuo a ogni "
        "connessione, con una sessione breve che finisce dopo un'ora.",
        "pair.yes": "Il telefono inquadra il codice; confronti le 6 cifre sui due lati e confermi qui.",
        "pair.no": "Collegalo poi con `pisa pair <nome>`.",
        "pair.later": "`pisa pair <nome>` qui; revoca un dispositivo con `pisa devices revoke`, o dall'app.",
        "pair.ask": "Collegare un dispositivo ora? Scrivi il suo nome, o premi Invio per saltare",
    },
}
PAIR_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "scan": "Scan this code with the Pisa app (valid until {until}):",
        "typed": "No camera? Type this code in the app: {code}",
        "waiting": "Waiting for the phone...",
        "request": "Pairing request from {name!r} ({platform}).",
        "compare": "Does it show {sas}? Confirm only if the 6 digits are the same",
        "confirmed": "Paired: {name} is device {device_id}.",
        "refused": "Refused: nothing was paired.",
        "mismatch": "The digits differ: refused. Someone else may have tried to pair; scan a new code.",
        "expired": "The code expired without a request. Run `pisa pair` again.",
        "signer_not_added": "{name} needs no --signer: every paired client signs device ops, approvals and "
        "erasing with its own device key (key id device-{device_id}).",
        "later": "Confirm it later with `pisa pair --confirm <the 6 digits>`.",
        "none_waiting": "No pairing request with these digits is waiting.",
        "need_name": "error: give the device's name: pisa pair <name>",
        "no_pin": "QR pairing needs the server's addresses and TLS certificate: set PISA_ADDRESSES and "
        "PISA_TLS_CERT (see .env.example). On a developer's machine, without TLS: "
        "PISA_INSECURE_LOOPBACK_PAIRING=true and PISA_ADDRESSES=http://127.0.0.1:8000 "
        "(docs-site/pairing.md).",
        "insecure": "WARNING: insecure loopback mode (PISA_INSECURE_LOOPBACK_PAIRING): no TLS, plain http, "
        "this machine only. For development; never for a phone or another machine.",
        "review_scan": "App Store review code (review mode): valid until {until}, for up to {devices} "
        "device(s), each paired without the 6-digit confirmation. Scan it with the Pisa app:",
        "review_notes": "For the App Review Notes: the server {server} and the 26-character code above. End "
        "it with `pisa pair --review --revoke`.",
        "review_off": "error: `pisa pair --review` works only in App Store review mode: set "
        "PISA_REVIEW_MODE=1 on a server with no one's real data (docs-site/app-review.md).",
        "review_revoked": "Review code ended ({codes} closed); {devices} device(s) paired with it revoked.",
        "revoke_needs_review": "error: --revoke ends the review code: pisa pair --review --revoke",
    },
    "it": {
        "scan": "Inquadra questo codice con l'app Pisa (valido fino alle {until}):",
        "typed": "Niente fotocamera? Digita questo codice nell'app: {code}",
        "waiting": "Aspetto il telefono...",
        "request": "Richiesta di collegamento da {name!r} ({platform}).",
        "compare": "Mostra {sas}? Conferma solo se le 6 cifre sono uguali",
        "confirmed": "Collegato: {name} è il dispositivo {device_id}.",
        "refused": "Rifiutato: nessun collegamento.",
        "mismatch": "Le cifre sono diverse: rifiutato. Qualcun altro potrebbe aver provato a collegarsi; "
        "inquadra un nuovo codice.",
        "expired": "Il codice è scaduto senza richieste. Rilancia `pisa pair`.",
        "signer_not_added": "A {name} non serve --signer: ogni client collegato firma le operazioni sui "
        "dispositivi, le approvazioni e le cancellazioni con la propria chiave (key id device-{device_id}).",
        "later": "Confermalo poi con `pisa pair --confirm <le 6 cifre>`.",
        "none_waiting": "Nessuna richiesta di collegamento con queste cifre è in attesa.",
        "need_name": "errore: scrivi il nome del dispositivo: pisa pair <nome>",
        "no_pin": "Il collegamento con QR richiede gli indirizzi e il certificato TLS del server: imposta "
        "PISA_ADDRESSES e PISA_TLS_CERT (vedi .env.example). Su una macchina di sviluppo, senza TLS: "
        "PISA_INSECURE_LOOPBACK_PAIRING=true e PISA_ADDRESSES=http://127.0.0.1:8000 (docs-site/pairing.md).",
        "insecure": "ATTENZIONE: modalità loopback non sicura (PISA_INSECURE_LOOPBACK_PAIRING): niente TLS, "
        "http in chiaro, solo questa macchina. Per lo sviluppo; mai per un telefono o un'altra macchina.",
        "review_scan": "Codice per la revisione App Store (modalità revisione): valido fino al {until}, "
        "per al massimo {devices} dispositivi, collegati senza la conferma delle 6 cifre. Inquadralo con "
        "l'app Pisa:",
        "review_notes": "Per le note di App Review: il server {server} e il codice di 26 caratteri qui "
        "sopra. "
        "Chiudilo con `pisa pair --review --revoke`.",
        "review_off": "errore: `pisa pair --review` funziona solo in modalità revisione App Store: imposta "
        "PISA_REVIEW_MODE=1 su un server senza dati veri di nessuno (docs-site/app-review.md).",
        "review_revoked": "Codice di revisione chiuso ({codes} chiusi); {devices} dispositivi collegati con "
        "esso revocati.",
        "revoke_needs_review": "errore: --revoke chiude il codice di revisione: pisa pair --review --revoke",
    },
}


def text(key: str, lang: str, **values: object) -> str:
    return locale.pick(TEXT, lang)[key].format(**values)


DOCTOR_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "ok": "Nothing to fix.",
        "plaintext_pairing_tokens": "PISA_SECRETS_KEY is not set, so pairing codes are stored in plaintext "
        "in the database for their 10 minutes (and provider keys cannot be stored). Set PISA_SECRETS_KEY "
        "(see .env.example) and restart Pisa.",
        "pairing_off": "No client can pair: PISA_ADDRESSES and PISA_TLS_CERT are not both set, or the "
        "certificate is unreadable. Set them (docs-site/pairing.md), or on a developer's machine use "
        "PISA_INSECURE_LOOPBACK_PAIRING=true with PISA_ADDRESSES=http://127.0.0.1:8000.",
        "talker_off": "Voice talks are in economy: no talker to check.",
        "talker_no_key": "Natural talks use {talker} ({model}), but no {provider} key is set: add it "
        "(`pisa secrets set {provider}`).",
        "talker_ok": "{talker} ({model}) opened and closed a session: natural talks can start.",
        "talker_failed": "{talker} ({model}) refused the session: {kind} (code {code}): {reason}. Natural "
        "talks fall back to economy until this is fixed (the key's access, the model: PISA_TALKER_MODEL_*).",
        "insecure_loopback_pairing": "PISA_INSECURE_LOOPBACK_PAIRING is on: clients pair and talk over plain "
        "http, and only this machine is answered. Fine for development; turn it off and set up TLS "
        "(PISA_TLS_CERT) before any phone or other machine uses this server.",
    },
    "it": {
        "ok": "Niente da sistemare.",
        "plaintext_pairing_tokens": "PISA_SECRETS_KEY non è impostata, quindi i codici di collegamento sono "
        "salvati in chiaro nel database per i loro 10 minuti (e le chiavi dei fornitori non si possono "
        "salvare). Imposta PISA_SECRETS_KEY (vedi .env.example) e riavvia Pisa.",
        "pairing_off": "Nessun client può collegarsi: PISA_ADDRESSES e PISA_TLS_CERT non sono entrambe "
        "impostate, o il certificato non si legge. Impostale (docs-site/pairing.md), o su una macchina di "
        "sviluppo usa PISA_INSECURE_LOOPBACK_PAIRING=true con PISA_ADDRESSES=http://127.0.0.1:8000.",
        "talker_off": "Le conversazioni a voce sono in economy: nessun talker da provare.",
        "talker_no_key": "Le conversazioni naturali usano {talker} ({model}), ma non c'è una chiave "
        "{provider}: aggiungila (`pisa secrets set {provider}`).",
        "talker_ok": "{talker} ({model}) ha aperto e chiuso una sessione: le conversazioni naturali possono "
        "partire.",
        "talker_failed": "{talker} ({model}) ha rifiutato la sessione: {kind} (codice {code}): {reason}. Le "
        "conversazioni naturali tornano in economy finché non è risolto (l'accesso della chiave, il modello: "
        "PISA_TALKER_MODEL_*).",
        "insecure_loopback_pairing": "PISA_INSECURE_LOOPBACK_PAIRING è attiva: i client si collegano e "
        "parlano in http in chiaro, e risponde solo a questa macchina. Va bene per lo sviluppo; spegnila e "
        "imposta TLS (PISA_TLS_CERT) prima che un telefono o un'altra macchina usi questo server.",
    },
}


CONSENT_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "intro": "Pisa would send your words to these AI models, for {name}:",
        "processor": "{provider} ({kind}), model {model}, for {slot}: receives {data}",
        "none": "No AI model is set yet: nothing would be sent.",
        "kind.cloud": "a cloud provider",
        "kind.self_hosted": "your own model server",
        "kind.on_server": "on this server",
        "health_no": (
            "Health stays out (allow it with --health). Natural voice talks need it: the voice AI hears "
            "everything you say."
        ),
        "health_yes": "Health too (--health).",
        "ask": "Allow it?",
        "given": "Allowed for {name}.",
        "not_given": "Not allowed: nothing changed.",
        "give_yes": "Run it on a terminal to answer, or add --yes once you have read the list.",
        "which": "Several devices are paired. Which one is allowing it?",
        "ask_which": "Its number or id:",
        "give_device": "Give its id with --device.",
        "unknown_device": "No active device {device}: see `pisa devices list`.",
        "no_device": "No device is paired yet: pair one first (`pisa pair <name>`).",
    },
    "it": {
        "intro": "Pisa manderebbe le tue parole a questi modelli di IA, per {name}:",
        "processor": "{provider} ({kind}), modello {model}, per {slot}: riceve {data}",
        "none": "Nessun modello di IA è impostato: non verrebbe mandato niente.",
        "kind.cloud": "un fornitore cloud",
        "kind.self_hosted": "un tuo server di modelli",
        "kind.on_server": "su questo server",
        "health_no": (
            "La salute resta fuori (permettila con --health). Le conversazioni vocali naturali ne hanno "
            "bisogno: l'IA vocale sente tutto quello che dici."
        ),
        "health_yes": "Anche la salute (--health).",
        "ask": "Lo permetti?",
        "given": "Permesso per {name}.",
        "not_given": "Non permesso: non è cambiato niente.",
        "give_yes": "Lancialo in un terminale per rispondere, o aggiungi --yes dopo aver letto l'elenco.",
        "which": "Ci sono più dispositivi collegati. Per quale lo permetti?",
        "ask_which": "Il suo numero o id:",
        "give_device": "Indica il suo id con --device.",
        "unknown_device": "Nessun dispositivo attivo {device}: vedi `pisa devices list`.",
        "no_device": "Nessun dispositivo è ancora collegato: collegane uno (`pisa pair <nome>`).",
    },
}
# the disclosure's data categories (`$defs/AIDisclosure`), as a person reads them
CONSENT_CATEGORIES: dict[str, dict[str, str]] = {
    "en": {
        "conversation": "your conversation", "mail": "your email", "calendar": "your calendar",
        "places": "places", "location": "where you are", "people": "people you know", "health": "health",
        "finance": "money", "documents": "your documents", "voice": "your voice (audio)",
    },
    "it": {
        "conversation": "la conversazione", "mail": "la tua email", "calendar": "il calendario",
        "places": "i luoghi", "location": "dove sei", "people": "le persone che conosci",
        "health": "la salute",
        "finance": "i soldi", "documents": "i tuoi documenti", "voice": "la tua voce (audio)",
    },
}  # fmt: skip


def consent_text(key: str, lang: str, **values: object) -> str:
    return locale.pick(CONSENT_TEXT, lang)[key].format(**values)


def consent_category(category: str, lang: str) -> str:
    return locale.pick(CONSENT_CATEGORIES, lang).get(category, category)


def doctor_text(key: str, lang: str, **values: object) -> str:
    text = locale.pick(DOCTOR_TEXT, lang)[key]
    return text.format(**values) if values else text


def pair_text(key: str, lang: str, **values: object) -> str:
    return locale.pick(PAIR_TEXT, lang)[key].format(**values)


def explain(step: str, lang: str, variant: str = "") -> list[str]:
    """A step's title, then what it asks, why, what a yes and a no change, and where to change it later.
    `variant`: a part's own words for it where it has them (`push.yes.direct`: the relay not offered)."""
    words = locale.pick(TEXT, lang)

    def part_of(part: str) -> str:
        key = f"{step}.{part}"
        return text(f"{key}.{variant}" if variant and f"{key}.{variant}" in words else key, lang)

    return [text(f"{step}.title", lang)] + [
        f"  {text(part, lang)}: {part_of(part)}" for part in ("asks", "why", "yes", "no", "later")
    ]


def sas_shown(sas: str) -> str:
    """The SAS as both sides show it: two groups of three digits."""
    return f"{sas[:3]} {sas[3:]}"


def token_shown(token: str) -> str:
    """The typed fallback in groups of five, easier to read and type (dashes are ignored when typed)."""
    return "-".join(token[i : i + 5] for i in range(0, len(token), 5))
