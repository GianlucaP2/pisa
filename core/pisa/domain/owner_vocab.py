"""The anonymiser's allow-list (the user loop): the only words of theirs that may stay
in a scenario candidate. Everything else becomes a placeholder (`domain.owner_loop.anonymise`).

Three parts, all fixed in code:
- **function words** of Italian and English (articles, prepositions, pronouns, conjunctions, auxiliaries,
  common adverbs, yes/no, fillers);
- **Pisa's command and day words**: the verbs they give Pisa and the nouns of a day Pisa manages (an event, a
  task, a dinner, a meeting), none of them a person, a place, a company, a body or a family;
- **the replay suite's own vocabulary**: the lower-case words of `tests/replay/scenarios.py`'s owner turns,
  minus every word the suite also writes capitalised (its names and places), the weekdays and months (they
  become [DAY] / [DATE]), the number words ([N]), the words of its e-mail domains, and `PRIVATE`.

`PRIVATE` (health, family, relationships, faith, body) is never kept, whatever list a word is also in: it
becomes [PRIVATE]. A test checks that no allowed word is private, a weekday, a month or a number word.
"""

from pisa.domain.lexicon.weekdays import EN_WEEKDAYS, IT_WEEKDAYS, IT_WEEKDAYS_PLAIN

_FUNCTION = """
a ad al alla alle allo agli ai all anche ancora ma e ed o oppure od se che chi cui come dove quando quanto
quanta quanti quante perché perche poi già gia non no sì si ok okay va bene sì certo forse solo pure più piu
meno molto poco tanto troppo tutto tutti tutte tutta ogni qualche qualcosa qualcuno niente nulla nessuno
nessuna altro altra altri altre stesso stessa questo questa questi queste quello quella quelli quelle quel
quei quegli il lo la i gli le l un uno una un' del dello della dei degli delle dal dallo dalla dai dagli
dalle nel nello nella nei negli nelle sul sullo sulla sui sugli sulle col coi di da in con su per tra fra
verso dopo prima durante entro senza contro sopra sotto dentro fuori vicino lontano accanto davanti dietro
io tu lui lei noi voi loro me te mi ti ci vi si ne lo gli le mio mia miei mie tuo tua tuoi tue suo sua suoi
sue nostro nostra nostri nostre vostro vostra loro
sono sei è e' siamo siete era erano sarà sarebbe stato stata stati state essere ho hai ha abbiamo avete
hanno avevo aveva avrò avere fa fai faccio facciamo fatto fatta fare vado vai va andiamo andare andato
andata vengo vieni viene venire posso puoi può puo possiamo potere potresti potrei devo devi deve dobbiamo
dovere dovrei voglio vuoi vuole vorrei volere so sai sa sapere dico dici dice detto dire stai sta stare
c' ce dov com qual quale quali cos cosa cose ecco allora dunque quindi però pero invece anzi comunque
magari almeno ormai subito presto tardi adesso ora oggi domani ieri stasera stamattina stanotte
dopodomani sempre mai spesso ancora già qui qua lì li là la su giù giu sopra
grazie prego per favore scusa scusami ciao ehi ehm beh boh dai ah oh uh insomma tipo
the a an and or but if then than that this these those there here what which who whom whose when where why
how all any some no not none every each other another same such only also just still already yet very too
so as at by for from in into of off on onto out over under up down with without about after before during
until while to is am are was were be been being have has had do does did done will would shall should can
could may might must i you he she it we they me him her us them my your his its our their mine yours ours
theirs yes ok okay please thanks thank sorry hi hey oh um uh well now today tomorrow tonight yesterday later
soon always never again here there
"""

_COMMANDS = """
sposta spostala spostalo spostali spostale spostare spostato spostata metti mettila mettilo mettere
segna segnalo segnala segnare segnato aggiungi aggiungere aggiunto togli togliere cancella cancellare
cancellato annulla annullare annullato elimina eliminare rimuovi ricordami ricordarmi ricorda ricordare
promemoria chiama chiamami chiamalo chiamala chiamare chiamato chiamata scrivi scrivigli scrivile scrivere
manda mandagli mandale mandala mandalo mandare mandato invia inviare rispondi rispondere rileggi leggi
leggimi leggere mostrami mostra mostrare fammi vedere dimmi dirmi trova trovami cercami cerca cercare
controlla controllare prenota prenotami prenotare compra comprami comprare ordina ordinare paga pagare
apri aprire chiudi chiudere chiudiamo accendi accendila spegni spegnere salva salvala salvalo tienila
tienimelo tieni tenere rinomina prepara preparami preparare organizza organizzami sistemami sistema
pagato pagati pagata vediamoci vediamo porta portami portare
pianifica fermati ferma riprendi continua aspetta aspetto intendevo volevo dicevo sbagliato sbagliata giusto
corretto confermo conferma va fatto fatta capito
move put add remove delete cancel remind call text write send reply read show find search check book buy
order pay open close turn save keep rename prepare plan stop resume wait meant said wrong right confirm
evento eventi appuntamento appuntamenti riunione riunioni call meeting cena pranzo colazione aperitivo caffè
caffe palestra spesa lista task compito compiti cosa cose impegno impegni agenda calendario giornata
giorno giorni settimana settimane mese mesi anno ora ore minuto minuti mezz mezza mezzora momento mattina
mattino pomeriggio sera notte weekend fine inizio prossimo prossima prossimi scorso scorsa ultimo ultima
primo prima secondo dopo presto tardi ritardo orario sveglia email mail messaggio messaggi posta bozza
bozze nota note biglietto biglietti volo voli treno treni viaggio viaggi hotel prenotazione prenotazioni
bolletta bollette fattura fatture pagamento pagamenti scadenza scadenze rimborso rimborsi ricevuta
documenti documento pacco pacchi consegna ritiro casa ufficio lavoro meteo pioggia tempo traffico strada
taxi macchina auto bus metro aeroporto stazione partenza arrivo andata ritorno pagina sito link foto
file vista viste skill telefono schermo notifica notifiche
event events appointment meeting dinner lunch breakfast coffee gym groceries list task tasks thing things
day days week weeks month months year hour hours minute minutes morning afternoon evening night next last
first early late alarm email message messages draft note ticket flight train trip hotel booking bill bills
invoice payment deadline refund receipt document package delivery home office work weather rain traffic car
airport station page site link photo file view phone screen notification
"""

# The lower-case words of the replay suite's owner turns, filtered as the
# module docstring says. A new scenario word joins here by hand, after the same filter.
_SUITE = """
about accendila actually add adesso aeroporto affitto aggiungi ai al allerte ancora andata anno annulla anzi
aperto appuntamento arrivato arrivo aspetta at aveva bagnoschiuma bandi bar bello benissimo bollette
buonanotte buono bus c café calendario call camicie cancella cancellare carta casa ce cena centro chain che
chiama chiamami chiamare chiamarla chiamo chiavi chiedermelo chiudiamo ci clienti coi com come commercialista
compiti compleanno comprami comprare con consegnato controlla convegno conviene corsa cosa cose costruiscimi
costume cover d dalle dei del della delle dello detto devo di dice did dinner dirmi documenti domani dov dove
dovrei e email entrare fai falli fammi fare fatte fatto fatture ferma find fotovoltaico garanzia giorni gli
glutine grazie guida ha hai ho i identità ieri il imprese in intendevo intorno io it l la lampadine lasciato
last latte lavanderia lavatrice le leggi lo mai mail mangiare mangiato mappa massima me mese meteo metro metti
mettila mezzanotte mi mia miei minutes minuti momento month mostrami near negli nella nelle next no non not
numero nuovo offerta oggi ogni ok ora ore out outlets pacco pagare pagina palestra pane partire passa per
perdo piccole pizza più pm poi pomeriggio posso posta potessi pranzo preferiti prenotami prepara preparo
presi problema problemi programma prossimi prossimo puoi pure qualche qualcosa quali quando quanti quella
quello quest questa qui quiet raccomandata regalo remind resto ricordami ricordarmi rileggi rimborsi rinomina
rispondi ristoranti ritardo ritirare riunione s salvala say sbagliato scadenza scadono scorsa scorso scrivi
se segna segnalo segnare segreteria senso senza sera settimana sistemami sito skill so solo sono sospeso
spegni spesa spetta sposta spostala spostalo spostamenti spostare spostato stagione stampo stasera stato su
sul sushi sì tabella taglio take tardi tavolo tempo tetto the ti tienila tienimelo to tomorrow train trip
trovami tu tutto ufficio ultima un una uova uscire vai vedere viaggi vicina vita voli volo vorrei what when
with you è
"""

# Health, family, relationships, faith and the body (special-category data): never kept, always [PRIVATE]
PRIVATE = frozenset(
    """
    moglie marito sposa sposo figlio figlia figli figlie bambino bambina bambini bimbo bimba bimbi neonato
    madre padre mamma papà papa babbo genitori fratello sorella fratelli sorelle nonno nonna nonni zio zia
    zii cugino cugina nipote suocero suocera cognato cognata fidanzato fidanzata fidanzati ragazzo ragazza
    compagno compagna amante ex divorzio divorziare separazione separati matrimonio sposarsi nozze
    incinta gravidanza parto aborto chemio chemioterapia radioterapia tumore cancro malattia malato malata
    ospedale ricovero pronto soccorso clinica medico medica dottore dottoressa psicologa psicologo
    psichiatra psicoterapia terapia terapeuta analisi esame esami visita ecografia dentista ginecologo
    ginecologa cardiologo oncologo urologo dermatologo farmacia farmaco farmaci pillola pillole diabete
    depressione ansia attacco panico dieta peso sesso funerale lutto morto morta morte chiesa messa moschea
    sinagoga preghiera
    wife husband spouse son daughter child children kid kids baby mother father mom mum dad parents brother
    sister grandma grandpa grandmother grandfather uncle aunt cousin girlfriend boyfriend partner divorce
    wedding pregnant pregnancy chemo chemotherapy cancer tumor hospital clinic doctor therapist therapy
    psychologist psychiatrist diagnosis surgery pharmacy medication pills diabetes depression anxiety
    funeral church mosque synagogue prayer
    """.split()
)

WEEKDAYS = frozenset(IT_WEEKDAYS + IT_WEEKDAYS_PLAIN + EN_WEEKDAYS)

ALLOWED = frozenset((_FUNCTION + _COMMANDS + _SUITE).split()) - PRIVATE - WEEKDAYS
