"""Everything a Pisa process needs at runtime, built once from Settings (and replaceable in tests)."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, cast

import httpx
from psycopg_pool import AsyncConnectionPool

from pisa.brain.conversation import ToolFamilies
from pisa.brain.models.gateway import ModelGateway
from pisa.brain.models.providers import ModelFactory, build_model
from pisa.connectors.learn_fetch import LearnFetcher
from pisa.connectors.open_meteo import OpenMeteo
from pisa.connectors.public_feeds import PublicFeeds
from pisa.connectors.speech import speech_providers
from pisa.connectors.talker_gemini import GeminiLive
from pisa.connectors.talker_xai import GrokVoice
from pisa.connectors.ucp import UcpClient
from pisa.connectors.web_reader import SignedReader
from pisa.db.pool import open_pool
from pisa.domain import model_catalog
from pisa.domain.owner import Owner
from pisa.infra import hard_limit
from pisa.infra.keys import KeyStore
from pisa.logging import get_logger
from pisa.ports.later import Later
from pisa.ports.push import LiveDevices, LiveSlot
from pisa.services.accountant import AccountantService
from pisa.services.actions import ActionsService
from pisa.services.actuals import ActualsService
from pisa.services.ai import AiService
from pisa.services.approvals import ApprovalsService
from pisa.services.assistant import AssistantDataService
from pisa.services.attend import AttendService
from pisa.services.autonomy import AutonomyService
from pisa.services.autopilot import AutopilotService, ServiceUndoers
from pisa.services.briefing import BriefingService
from pisa.services.connectors import ConnectorsService
from pisa.services.conversation import ConversationService
from pisa.services.conversation_deps import ConversationDeps
from pisa.services.curious import CuriousService
from pisa.services.desk import DeskService
from pisa.services.done import DoneService
from pisa.services.dossier import DossierService
from pisa.services.episodes import EpisodeService
from pisa.services.erase import EraseService
from pisa.services.errands import ErrandsService
from pisa.services.explore import ExploreService
from pisa.services.follow import FollowService
from pisa.services.google import GoogleService
from pisa.services.google_sync import sync_calendar, sync_gmail
from pisa.services.guardian_tick import GuardianService
from pisa.services.handoffs import HandoffService
from pisa.services.ideas import IdeasService
from pisa.services.identity import IdentityService
from pisa.services.intake import IntakeService
from pisa.services.intentions import IntentionsService
from pisa.services.learn import LearnService, LearnTurnData
from pisa.services.learn_ask import GatewayLearnModels, LearnAsk
from pisa.services.learn_search import LearnSearch
from pisa.services.learning import LearningService
from pisa.services.lexicon import LexiconService
from pisa.services.live_activity import LiveActivityService
from pisa.services.memory import MemoryService
from pisa.services.money_back import MoneyBackService
from pisa.services.night import NightService, night_web
from pisa.services.night_step import reader
from pisa.services.note import NoteService
from pisa.services.owner import provide
from pisa.services.owner_loop import OwnerLoop
from pisa.services.people import PeopleService
from pisa.services.phone_keys import PhoneKeys
from pisa.services.place_assistant import PlaceAssistantData
from pisa.services.place_notes import PlaceNotesService
from pisa.services.place_providers import PlaceProviderService
from pisa.services.places import PlacesService
from pisa.services.plan_options import PlanOptionsService
from pisa.services.planner import PlannerService
from pisa.services.portrait import OFFER_PATH, PortraitService
from pisa.services.profile import ProfileService, profile_secret
from pisa.services.public_data import PublicDataService, transit_minutes
from pisa.services.push import PushService
from pisa.services.reader import ReaderService
from pisa.services.ready import ReadyService
from pisa.services.reflex import PisaReads, ReflexService
from pisa.services.reminders import RemindersService
from pisa.services.residency import BriefingLines, ResidencyService, TwoCountriesData
from pisa.services.rituals import DayRehearsal, RitualsService, place_ideas, put_rituals, rituals_of
from pisa.services.settings import SettingsService
from pisa.services.skill_starters import StarterService
from pisa.services.skills import Embed, SkillsService, gateway_embed
from pisa.services.stanotte import StanotteService
from pisa.services.surprise import SurpriseService
from pisa.services.talk import TalkService
from pisa.services.tasks import TasksService
from pisa.services.tasks_bulk import TasksBulkService
from pisa.services.trips import TripsService, VaultDocuments
from pisa.services.turn_hint import TurnHintService
from pisa.services.turns import TurnsService
from pisa.services.unasked import Unasked
from pisa.services.vault import VaultService
from pisa.services.views import ViewReads, ViewService
from pisa.services.voice import VoiceService
from pisa.services.voice1 import KnockService
from pisa.services.voice2 import Voice2Service
from pisa.services.voice3 import NightVoice
from pisa.services.voice4 import CeoVoice
from pisa.services.voice6 import Prurito
from pisa.services.voice_id import Models, VoiceIdService
from pisa.services.walks import WalksService
from pisa.services.weather import WeatherService
from pisa.services.world import WorldService
from pisa.services.zone import ZoneService
from pisa.services.zone import load as load_zone
from pisa.settings import Settings

# Outbound HTTP binds its source to IPv4 (the Maps key is IPv4-restricted); nothing listens on it.
IPV4_ANY = "0.0.0.0"  # noqa: S104

_log = get_logger("pisa.runtime")


@dataclass
class Runtime:
    settings: Settings
    pool: AsyncConnectionPool
    http: httpx.AsyncClient
    sync_http: httpx.Client
    apns_http: httpx.AsyncClient
    keys: KeyStore
    gateway: ModelGateway
    conversations: ConversationService
    ai: AiService
    google: GoogleService
    tasks: TasksService
    tasks_bulk: TasksBulkService
    intake: IntakeService
    settings_service: SettingsService
    erase: EraseService  # their deletion by family (the API only)
    learn: LearnService
    learn_ask: LearnAsk  # answers from them
    push: PushService
    briefings: BriefingService
    reminders: RemindersService
    attend: AttendService
    place_providers: PlaceProviderService
    places: PlacesService
    planner: PlannerService
    plan_options: PlanOptionsService
    follow: FollowService
    done: DoneService
    voice: VoiceService
    talk: TalkService
    voice_id: VoiceIdService  # their voice, the gate in front of the talker
    weather: WeatherService
    live_activity: LiveActivityService
    approvals: ApprovalsService
    actions: ActionsService
    profile: ProfileService
    views: ViewService
    people: PeopleService
    rituals: RitualsService
    place_notes: PlaceNotesService
    learning: LearningService
    lexicon: LexiconService
    turn_hints: TurnHintService
    reflex: ReflexService
    autopilot: AutopilotService  # the ledger, stop everything, the vault, autonomy
    clock: Callable[[], datetime]
    owner: Owner  # the user's context: language, country, zone (one per runtime)
    zone: ZoneService
    ready: ReadyService  # Pisa's ready answers for the talk's likely questions
    handoffs: HandoffService  # what Pisa prepares for the user's tap, and the one follow-up
    trips: TripsService  # their trips from confirmation emails, landing, the checklist
    residency: ResidencyService  # the residency counter (a count, never advice)
    money_back: MoneyBackService  # money that comes back, promises, supplies
    public: PublicDataService  # transit, weather alerts, the ZTL, pharmacies (read-only, cached)
    errands: ErrandsService  # one errand combo a day at most
    guardian: GuardianService  # the holds' minute, and the record's readers
    night: NightService  # the night's frame, shadow only
    identity: IdentityService  # Pisa's own key, its signed reads
    world: WorldService  # UCP carts, the ladder
    explore: ExploreService  # the fog of war, the Map of the Life
    ideas: IdeasService  # the idea engine and the ladder, night steps
    curious: CuriousService  # the curiosity engine, night steps
    desk: DeskService  # the CEO's desk, night steps (shadow)
    note: NoteService  # their note of standing instructions
    episodes: EpisodeService  # Pisa's memory of their days (the night's steps, recall)
    memory: MemoryService  # the night's claims from their words (m1_extract)
    portrait: PortraitService  # the portrait in "Su di te" (m2_portrait, m3_offer)
    stanotte: StanotteService  # the morning sheet, its gate, the diary
    connectors: ConnectorsService  # every source a connector
    loops: IntentionsService  # intentions and waiting-for
    dossier: DossierService  # the morning's one dossier
    prurito: Prurito  # il prurito, a miss's cause (the night), the same day's knock
    turns: TurnsService  # contract 1's REST turns' kept sheets
    # the API process's live sockets (its socket registry, `live`); empty in the worker
    live_slot: LiveSlot = field(default_factory=LiveSlot)
    _background: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def live(self) -> LiveDevices | None:
        """The API process's live sockets (set by the API at its start); None in the worker."""
        return self.live_slot.devices

    @live.setter
    def live(self, devices: LiveDevices | None) -> None:
        self.live_slot.devices = devices

    @classmethod
    async def start(
        cls,
        settings: Settings,
        *,
        model_factory: ModelFactory = build_model,
        http_transport: httpx.AsyncBaseTransport | None = None,
        apns_transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        pool_size: int = 10,
        voice_models: Callable[[], Models] | None = None,
    ) -> "Runtime":
        """Every service built once, each after what it needs, each with its collaborators in its
        constructor. Two cycles are closed by ports, never by an attribute set afterwards: the approvals reach
        the API's live sockets through `live_slot`, and the approvals' plan options and Google's first sync
        are `Later`s filled right after what they name is built."""
        # the worker's one runtime runs its groups side by side, each step holding a
        # connection for its lock: it asks for more.
        pool = await open_pool(settings.database_url, max_size=pool_size)
        # connections kept a minute (httpx keeps 5 s): the next reply's voice, the next model call
        # and the next map search reuse them instead of a new TLS handshake each turn.
        # IPv4 only: the api runs on the host's network, where the host's IPv6 would go out
        # too, and the Google Maps key allows the server's IPv4 address only.
        limits = httpx.Limits(keepalive_expiry=60.0)
        http = httpx.AsyncClient(
            transport=http_transport or httpx.AsyncHTTPTransport(local_address=IPV4_ANY, limits=limits),
            follow_redirects=False,
            limits=limits,
        )
        # For google-auth's synchronous token refresh; tests pass one transport that serves both.
        sync_transport = http_transport if isinstance(http_transport, httpx.BaseTransport) else None
        sync_http = httpx.Client(
            transport=sync_transport or httpx.HTTPTransport(local_address=IPV4_ANY), follow_redirects=False
        )
        # APNs speaks HTTP/2 only; tests inject a transport.
        apns_http = httpx.AsyncClient(http2=True, transport=apns_transport or http_transport)
        # fetches of addresses Pisa was given (pages, papers) go through `connectors.outbound`, each hop
        # pinned to the address it checked; tests route them to their transport
        outbound_transport = None if http_transport is None else (lambda _ip: http_transport)
        keys = KeyStore(settings.secrets_key)
        voice_id = VoiceIdService(
            pool, keys, voice_models or (lambda: load_voice_models(settings)), gate_on=settings.voice_gate
        )
        model_catalog.set_override(settings.model_catalog)  # the operator's model catalog, if any
        model_catalog.set_talker_models(settings.talker_models)  # and its talker models, if any
        owner = provide(settings, clock)  # the user's context, passed to every service that reads it
        await load_zone(pool, owner)  # where the user is (the phone's zone), loaded now
        hard = hard_limit.limits(settings)  # the hard limit, handed to every service that reserves
        speech = speech_providers(http)  # the speech adapters, shared by the voice and the AI keys
        gateway = ModelGateway(
            pool,
            keys,
            owner=owner,
            ollama_base_url=settings.ollama_base_url,
            model_factory=model_factory,
            clock=clock,
            http=http,
            hard_limits=hard,
        )
        # the `memory.embed` slot's embedder: the skills, the idea engine and Pisa Learn
        embed = gateway_embed(pool, keys, clock, owner, hard)
        this: Later[Runtime] = Later("the runtime")  # Google's first sync runs on the runtime, built last
        google = GoogleService(
            pool, keys, http, sync_http, owner=owner, clock=clock,
            on_connected=lambda: this().sync_google_in_background(),  # the first sync starts right away
        )  # fmt: skip
        tasks = TasksService(pool, owner=owner, clock=clock)
        settings_service = SettingsService(pool, owner=owner, clock=clock, hard_limits=hard)
        place_providers = PlaceProviderService(
            pool,
            keys,
            http,
            team_id=settings.apns_team_id,
            owner=owner,
            clock=clock,
            hard_limits=hard,
        )
        # Pisa's own identity (never the user's phone keys), its signed reads, UCP carts; published, Pisa
        # reads pages itself, signed (Tavily the fallback)
        identity = IdentityService(pool, keys, agent_url=settings.agent_url, clock=clock)
        signed = (
            SignedReader(identity, transport=outbound_transport) if identity.agent_url is not None else None
        )
        places = PlacesService(
            pool, place_providers, gateway, owner=owner, clock=clock,
            reader=signed.read if signed is not None else None,
            counted_reader=signed.read_counted if signed is not None else None,
        )  # fmt: skip
        meteo = OpenMeteo(http)
        weather = WeatherService(pool, meteo, places, owner=owner, clock=clock)
        place_data = PlaceAssistantData(
            pool, places, owner=owner, clock=clock, weather=weather,
            reader=ReaderService(gateway, clock),  # the tool-less reader of a page
        )  # fmt: skip
        # parts others read before the autopilot is built: the approvals read the autonomy level,
        # the trips a document's country, the views the week's report
        phone_keys = PhoneKeys(pool, owner=owner, clock=clock)
        autonomy = AutonomyService(pool, phone_keys, owner=owner, clock=clock)
        vault = VaultService(pool, owner=owner, clock=clock)
        owner_loop = OwnerLoop(pool, owner=owner, clock=clock)
        # two countries; the departure checklist reads the autopilot's vault (a document's country)
        trips = TripsService(
            pool, owner=owner, clock=clock, places=places, documents=VaultDocuments(vault.documents),
        )  # fmt: skip
        residency = ResidencyService(pool, trips, owner=owner, clock=clock)
        ready = ReadyService(pool, owner=owner, clock=clock)
        # the phone's zone: a new zone, Pisa's ready answers rebuilt; each report heard by the trips and the
        # residency count
        zone = ZoneService(pool, owner, on_change=ready.changed, observers=(trips.on_context, residency.note))
        planner = PlannerService(pool, tasks, places, owner=owner, clock=clock, weather=weather, trips=trips)
        profile = ProfileService(
            pool,
            owner=owner,
            clock=clock,
            secret=profile_secret(settings.secrets_key.get_secret_value() if settings.secrets_key else None),
            places=places,
        )
        night = NightService(pool, owner=owner, clock=clock, web=night_web(places))
        # the sheet is the night's last step; sent only with the gate open, by the morning briefing's notice
        # and the Sunday poster. Code opens the gate through the rituals' setting.
        stanotte = StanotteService(
            pool, owner=owner, clock=clock, switch=lambda patch: put_rituals(pool, owner, patch, code=True)
        )

        # the night spoken (the story, the drawer, `stanotte`, spars, bets, il quarto d'ora); bets
        # resolved by the night from Open-Meteo's public page (its past day); il quarto d'ora only inside
        # the knock
        async def observe(lat: float, lon: float, day: date) -> tuple[float, str]:
            return await meteo.observed_rain(lat, lon, day, owner.tz)

        voice3 = NightVoice(
            pool, owner=owner, clock=clock, stanotte=stanotte, weather=weather, observe=observe
        )
        # the Ghost Run's day readers: the rituals', the CEO's prova generale's, the dossier's
        rehearsal = DayRehearsal(pool, owner=owner, places=places)
        # the CEO spoken (il consiglio, la prova generale, "sciame su questo"); the first two only
        # as the knock's parts on Sunday; a swarm starts only on a day their phone signed
        voice4 = CeoVoice(
            pool, owner=owner, clock=clock, night=voice3, rehearsal=rehearsal, names=night.names
        )
        # il prurito (a gap in the talk: one row, one line, answered with what is left of the
        # turn's research budget); its answer and a miss's reason come back only as the knock's parts, the
        # same day
        from pisa.brain.voice6 import gateway_think as prurito_think

        async def look(query: str) -> list[dict[str, str]]:
            return await place_data.web_search(
                query, include_domains=None, exclude_domains=None, recent_days=None, language=None
            )

        prurito = Prurito(
            pool, owner=owner, clock=clock, think=prurito_think(gateway), look=look, names=night.names
        )
        # every source a connector, switched in Settings; its three readers found by search_tools and
        # read by the night (shadow), never a line of any turn's prompt
        sources = ConnectorsService(pool, keys, http, owner=owner, clock=clock)
        await sources.ensure()
        # the knock and il tragitto (attend's senses, its rails, its stop; "dimmi" opens them), with their
        # parts. One finite morning: the knock carries one dossier (its parts, the radar, their loops, the
        # night's drafts, "domani, provato"); their yes hears at most three, then "È tutto"; the phone the
        # rest. The dossier is the knock's own, made with the knock's parts.
        voice1 = KnockService(
            pool, owner=owner, clock=clock, weather=weather, stanotte=stanotte,
            quarto=voice3.quarto_part, ceo=[voice4.consiglio_part, voice4.prova_part], later=[prurito.part],
            dossier=lambda parts: DossierService(
                pool, owner=owner, clock=clock, sources=sources, rehearsal=rehearsal, parts=parts
            ),
        )  # fmt: skip
        dossier = cast(DossierService, voice1.dossier)
        place_notes = PlaceNotesService(pool, owner=owner, clock=clock)
        # never drop an intention. Its cues ride the reminders (a task), the place notes (the
        # phone's regions) and attend (an event, a "still waiting?"); its tools found by search_tools, never
        # a line of any turn's prompt. One morning, the loops' nudges wait for the dossier.
        loops = IntentionsService(
            pool, tasks, place_notes, owner=owner, clock=clock, morning_hold=voice1.carries
        )
        # the senses propose, attend decides (every minute, after them): the knock and il tragitto (the trip
        # first), the same day's knock again, the loops
        attend = AttendService(
            pool, gateway, owner=owner, clock=clock, profile=profile, weather=weather,
            senses=[voice1.tragitto, voice1.knock, voice1.again, loops.sense],
        )  # fmt: skip
        push = PushService(
            pool, keys, apns_http, team_id=settings.apns_team_id, topic=settings.apns_topic,
            relay_url=settings.push_relay_url,
        )  # fmt: skip
        live = LiveSlot()  # the approvals reach the API's live sockets, there once the API starts
        unasked = Unasked()  # what Pisa said to each device unasked (a yes answers the newest question)
        approvals = ApprovalsService(pool, push, owner=owner, clock=clock, live=live.get, unasked=unasked)

        # an approved calendar change: Pisa's copy of the calendar and the day's plan follow at once
        async def calendar_changed() -> None:
            await follow_calendar(pool, google, places, planner, ready, owner, clock)

        # the approvals apply an option of Pisa's after the user's yes, and put Pisa's plan back when
        # that change is undone; the options write through the approvals (built right after)
        options: Later[PlanOptionsService] = Later("the plan's options")
        actions = ActionsService(
            pool, google, approvals, owner=owner, clock=clock, after_calendar_change=calendar_changed,
            apply_option=lambda options_id, option_id, approved: options().apply(
                options_id, option_id, "voice", approved
            ),
            undo_option=lambda ref: options().undo_for_approval(ref),
            autonomy=autonomy.verdict,  # the approvals read the level
        )  # fmt: skip
        plan_options = PlanOptionsService(
            pool, planner, places, owner=owner, clock=clock,
            write_through=actions.write_through, undo_through=actions.undo_through,
        )  # fmt: skip
        options.fill(plan_options)
        follow = FollowService(
            pool, planner, plan_options, tasks, places, owner=owner, clock=clock,
            attend=attend, offers={OFFER_PATH: profile.offer_taken},  # "Conosciamoci": the first talk
        )  # fmt: skip
        done = DoneService(pool, tasks, planner, owner=owner, clock=clock, attend=attend)
        lexicon = LexiconService(pool, owner=owner, clock=clock)
        people = PeopleService(pool, owner=owner, clock=clock)
        # the public feeds (read-only, no key): the brain's tools, views and skills, the briefing
        public = PublicDataService(
            pool, PublicFeeds(http), owner=owner, clock=clock, travel=transit_minutes(places),
            city_name=places.city_at,
        )  # fmt: skip

        async def ritual_settings() -> dict[str, Any]:  # the morning's one thing
            async with pool.connection() as conn:
                return await rituals_of(conn)

        briefings = BriefingService(
            pool, gateway, tasks, owner=owner, clock=clock, places=places, planner=planner,
            weather=weather, people=people, two_countries=BriefingLines(trips, residency),
            rituals=ritual_settings, public=public.briefing_lines,
            surprise=SurpriseService(owner=owner, clock=clock),  # Pisa's morning thought, the first briefing
            stanotte=stanotte.morning,
        )  # fmt: skip
        tasks_bulk = TasksBulkService(pool, tasks)
        assistant_data = AssistantDataService(
            pool, tasks, settings_service, briefings.ask_text, planner, plan_options, follow, done, tasks_bulk
        )
        # Pisa's own views (recipes of read-only reads and sheet blocks); the user loop: "mostrami il report"
        # opens the week's talk report (Pisa's built-in view)
        view_reads = ViewReads(assistant_data, place_data, owner=owner, clock=clock, public=public)
        views = ViewService(
            pool, view_reads, clock=clock, builtin=lambda: _report_sheet(owner_loop, owner, pool)
        )
        # Pisa's reflex on the user's partial words (natural and economy) and the shared state
        reflex = ReflexService(
            gateway,
            PisaReads(assistant_data, actions, place_data, owner=owner, clock=clock),
            owner=owner,
            clock=clock,
        )
        # perceive then ask, Pisa's own states, the character file, its opinions (by code, in a turn);
        # the Sunday card counts the cue-questions
        voice2 = Voice2Service(pool, owner=owner, clock=clock)
        await voice2.load_traits()  # the approved traits, held in memory

        # a bill email's PDF, from Gmail (gmail.readonly), read locally
        def pdfs(email_id: str, part_id: str) -> Awaitable[bytes | None]:
            return google.gmail().attachment(email_id, part_id)

        # the autopilot: the ledger, stop everything, the vault and the bills, earned autonomy, the accountant
        # package (one draft, sent only from the card with Face ID); the ledger's Sunday line: what Pisa
        # took back of their profile, restorable
        autopilot = AutopilotService(
            pool, ServiceUndoers(approvals, done, tasks_bulk, tasks), views, owner=owner, clock=clock,
            keys=phone_keys, autonomy=autonomy, vault=vault, owner_loop=owner_loop, pdfs=pdfs,
            accountant=AccountantService(
                pool, actions, residency, vault, owner=owner, clock=clock, pdfs=pdfs
            ),
            profile=profile, voice2=voice2, voice6=prurito,
        )  # fmt: skip
        # the end-of-walk recap, the fog's glint, the Map of the Life (the rituals' tick)
        explore = ExploreService(pool, owner=owner, places=places, clock=clock)
        # the rituals, Pisa's questions that take one yes; Chiudiamo reads the ledger
        rituals = RitualsService(
            pool, owner=owner, unasked=unasked, clock=clock, tasks=tasks, actions=actions,
            approvals=approvals, done=done, places=places, weather=weather,
            ideas=place_ideas(pool, places, clock), profile=profile, rehearsal=rehearsal,
            ledger=autopilot.ledger, walks=WalksService(pool, owner=owner, clock=clock), explore=explore,
            stanotte=stanotte.sunday, voice1=voice1,
        )  # fmt: skip
        handoffs = HandoffService(pool, owner=owner, clock=clock, attend=attend, intentions=loops)
        money_back = MoneyBackService(pool, owner=owner, clock=clock, handoffs=handoffs, intentions=loops)
        # Pisa's memory of their days; the night's steps (its stop, its budget), `recall` deferred
        from pisa.brain.episodes import gateway_digest

        episodes = EpisodeService(pool, owner=owner, clock=clock, digest=gateway_digest(gateway))
        # the night learns their tastes and habits from what they said (its €0.05, the night's
        # budget), candidates under
        from pisa.brain.memory import gateway_estimate, gateway_extract

        memory = MemoryService(
            pool, profile, owner=owner, clock=clock, extract=gateway_extract(gateway),
            estimate=gateway_estimate(gateway),
        )  # fmt: skip
        from pisa.brain.portrait import gateway_estimate as portrait_estimate
        from pisa.brain.portrait import gateway_write

        portrait = PortraitService(
            pool, profile, owner=owner, clock=clock, write=gateway_write(gateway),
            estimate=portrait_estimate(gateway),
        )  # fmt: skip
        # Pisa Learn's papers slice: the sources (background ingestion), retrieval and cited answers
        learn_models = GatewayLearnModels(gateway, pool)
        learn = LearnService(
            pool,
            LearnFetcher(contact_email=settings.learn_contact_email, transport=outbound_transport),
            embed=embed,
            clock=clock,
        )
        learn_ask = LearnAsk(
            pool, LearnSearch(pool, learn_models, embed), learn_models, owner=owner, clock=clock
        )
        world = WorldService(pool, UcpClient(http, identity), clock=clock)
        # their note of standing instructions, read whole every turn; their yes to a
        # held rewrite reaches it through the profile's one pending write
        note = NoteService(pool, clock=clock, profile=profile)
        conversations = ConversationService(
            pool,
            gateway,
            owner=owner,
            clock=clock,
            deps=ConversationDeps(
                tools=ToolFamilies(
                    data=assistant_data,
                    places=place_data,
                    actions=actions,
                    profile=profile,
                    views=views,
                    trips=TwoCountriesData(trips, residency),
                    people=people,
                    rituals=(rituals, place_notes),
                    autopilot=autopilot,
                    learn=LearnTurnData(learn, learn_ask),
                    handoffs=handoffs,
                    money=money_back,
                    # the public tools and the research's first skills, kept through the path
                    public=(public, StarterService(pool, autopilot.skills, owner=owner, clock=clock)),
                    world=world,
                    explore=explore,
                    night=voice3,
                    memory=episodes,
                    sources=sources,
                    loops=loops,
                    owner_note=note,
                ),
                profile=profile,
                lexicon=lexicon,
                unasked=unasked,
                attend=attend,
                # the skills, by embeddings when `memory.embed` has a model, else by trigrams
                skills=SkillsService(pool, embed),
                reflex=reflex,
                knock=voice1,
                perceive=voice2,
                night=voice3,
                ceo=voice4,
                prurito=prurito,
                episodes=episodes,
                note=note,
                connectors=sources,
            ),
        )
        # the night's engines: their calls through the gateway on the night's budget, their searches through
        # the frame's `search`, their pages through Pisa's own signed reader (`places.reader`)
        ideas = ideas_service(pool, night, owner, clock, places, gateway, embed)
        curious = curious_service(pool, night, owner, clock, places, gateway)
        desk = desk_service(pool, night, owner, clock, places, gateway, ideas)
        actuals = ActualsService(pool, owner=owner, clock=clock)
        # the night's engines, declared once: the night runs their steps in the order of `night.STEPS`
        night.declare(
            episodes,
            memory,
            portrait,
            ideas,
            curious,
            desk,
            prurito,
            stanotte,
            voice3,
            voice4,
            sources,
            actuals,
            dossier,
        )
        runtime = cls(
            settings=settings,
            pool=pool,
            http=http,
            sync_http=sync_http,
            apns_http=apns_http,
            keys=keys,
            gateway=gateway,
            conversations=conversations,
            views=views,
            people=people,
            rituals=rituals,
            place_notes=place_notes,
            ai=AiService(
                pool,
                keys,
                http,
                speech=speech,
                owner=owner,
                ollama_base_url=settings.ollama_base_url,
                clock=clock,
                hard_limits=hard,
            ),
            google=google,
            tasks=tasks,
            tasks_bulk=tasks_bulk,
            intake=IntakeService(pool, gateway, owner=owner, clock=clock, attend=attend),
            settings_service=settings_service,
            erase=EraseService(pool, owner=owner, clock=clock),
            learn=learn,
            learn_ask=learn_ask,
            push=push,
            briefings=briefings,
            reminders=RemindersService(pool, owner=owner, clock=clock, attend=attend),
            attend=attend,
            place_providers=place_providers,
            places=places,
            planner=planner,
            plan_options=plan_options,
            follow=follow,
            done=done,
            voice=VoiceService(
                pool,
                keys,
                speech,
                owner=owner,
                clock=clock,
                hard_limits=hard,
            ),
            talk=TalkService(
                pool,
                keys,
                {"xai": GrokVoice(), "gemini": GeminiLive()},
                owner=owner,
                clock=clock,
                hard_limits=hard,
                reflex=reflex,
                ready=ready,
                voice=voice_id,
                asked_aloud=conversations.asked_aloud,  # a device's asked cards read out now
                ollama_base_url=settings.ollama_base_url,
            ),
            voice_id=voice_id,
            weather=weather,
            live_activity=LiveActivityService(pool, push, planner, follow, owner=owner, clock=clock),
            approvals=approvals,
            actions=actions,
            profile=profile,
            learning=LearningService(pool, profile, owner=owner, clock=clock),
            lexicon=lexicon,
            turn_hints=TurnHintService(),
            reflex=reflex,
            autopilot=autopilot,
            clock=clock,
            owner=owner,
            zone=zone,
            ready=ready,
            handoffs=handoffs,
            trips=trips,
            residency=residency,
            money_back=money_back,
            public=public,
            errands=ErrandsService(pool, owner=owner, clock=clock),
            # the holds' minute pushes held approvals again
            guardian=GuardianService(pool, owner=owner, clock=clock, repush=approvals.redeliver),
            night=night,
            identity=identity,
            world=world,
            explore=explore,
            ideas=ideas,
            curious=curious,
            desk=desk,
            note=note,
            episodes=episodes,
            memory=memory,
            portrait=portrait,
            stanotte=stanotte,
            connectors=sources,
            loops=loops,
            dossier=dossier,
            prurito=prurito,
            turns=TurnsService(pool),  # GET /v1/sheets/{id} of a contract 1 REST turn
            live_slot=live,
        )
        this.fill(runtime)
        return runtime

    async def sync_gmail_and_intake(self) -> None:
        """Gmail, then intake of the new messages (the worker runs this every 2 minutes)."""
        result = await sync_gmail(self.pool, self.google, now=self.clock())
        if result.ran:
            if not await self.autopilot.stopped():  # the mail syncs; the tasks it makes wait
                await self.intake.run()
                try:  # bills and receipts (a failure never fails the sync)
                    await self.autopilot.money.scan()
                except Exception:
                    _log.exception("money_scan_failed")
            await self.resolve_places()
            try:  # the new confirmation emails into trips (data); never fails the sync
                await self.trips.refresh()
            except Exception:
                _log.exception("trips_refresh_failed")
            if not await self.autopilot.stopped():  # after the trips (a flight's notice needs its leg)
                try:
                    await self.money_back.scan()
                except Exception:
                    _log.exception("money_back_scan_failed")

    async def sync_calendar(self) -> None:
        """The worker runs this every 5 minutes."""
        await sync_calendar(self.pool, self.google, timezone=self.owner.tz, now=self.clock())
        await self.resolve_places()

    async def after_calendar_change(self) -> None:
        """An approved calendar change: Pisa's copy of the calendar and the day's plan follow at once."""
        await follow_calendar(
            self.pool, self.google, self.places, self.planner, self.ready, self.owner, self.clock
        )

    async def resolve_places(self) -> None:
        """New or changed event and task locations → places. A failure never fails the sync."""
        await resolve_places(self.places)

    async def sync_google(self) -> None:
        """One full round, right after the user connects Google."""
        await self.sync_gmail_and_intake()
        await self.sync_calendar()

    def sync_google_in_background(self) -> None:
        task = asyncio.create_task(self.sync_google())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def close(self) -> None:
        await self.talk.close_all()  # each conversation's rows and ledger close first
        await self.ready.wait()
        self.turn_hints.close()
        await self.ai.wait_background()
        await self.autopilot.settled()  # a correction being kept
        await self.erase.close()  # their deletion is cancelled, left for the resume
        await self.learn.close()  # an ingestion is cancelled, left for the resume
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        await self.http.aclose()
        self.sync_http.close()
        await self.apns_http.aclose()
        await self.pool.close(timeout=5)


async def resolve_places(places: PlacesService) -> None:
    """New or changed event and task locations → places. A failure never fails the sync."""
    try:
        await places.resolve_pending()
    except Exception:
        _log.exception("place_resolution_failed")


async def follow_calendar(
    pool: AsyncConnectionPool,
    google: GoogleService,
    places: PlacesService,
    planner: PlannerService,
    ready: ReadyService,
    owner: Owner,
    clock: Callable[[], datetime],
) -> None:
    """An approved calendar change: Pisa's copy of the calendar and the day's plan follow at once (the
    approvals call it; it needs no runtime)."""
    await sync_calendar(pool, google, timezone=owner.tz, now=clock())
    await resolve_places(places)
    await planner.plan(reason="calendar changed")
    ready.changed()  # the ready answers follow too


def ideas_service(
    pool: AsyncConnectionPool,
    night: NightService,
    owner: Owner,
    clock: Callable[[], datetime],
    places: PlacesService,
    gateway: ModelGateway,
    embed: Embed,
) -> IdeasService:
    """The idea engine as night steps (shadow only). Its calls go through the gateway on
    the night's budget, its searches through the frame's `search`, its evidence pages through Pisa's own
    signed reader (`places.reader`, set once Pisa is published); no reader, no link checked, nothing becomes
    `ready`."""
    from pisa.brain.ideas import gateway_think

    async def embed_texts(texts: Any) -> list[list[float]] | None:
        made = await embed(list(texts), "document")
        return made[1] if made else None

    return IdeasService(
        pool,
        night,
        owner=owner,
        clock=clock,
        think=gateway_think(gateway),
        embed=embed_texts,
        read=reader(places),
    )


def curious_service(
    pool: AsyncConnectionPool,
    night: NightService,
    owner: Owner,
    clock: Callable[[], datetime],
    places: PlacesService,
    gateway: ModelGateway,
) -> CuriousService:
    """The curiosity engine as night steps (shadow only), after the ideas' steps. Its calls go through the
    gateway on the night's budget, its searches through the frame's `search`, its pages through Pisa's own
    signed reader (`places.reader`); no reader, no quote checked, nothing answered."""
    from pisa.brain.curious import gateway_think

    return CuriousService(
        pool, night, owner=owner, clock=clock, think=gateway_think(gateway), read=reader(places)
    )


def desk_service(
    pool: AsyncConnectionPool,
    night: NightService,
    owner: Owner,
    clock: Callable[[], datetime],
    places: PlacesService,
    gateway: ModelGateway,
    ideas: IdeasService,
) -> DeskService:
    """The CEO's desk as night steps (shadow). Its calls go through the gateway on the night's budget, its
    searches through the frame's `search` (its own 6 of the 40), its pages through Pisa's own signed reader
    (`places.reader`); no reader, no date checked, nothing shown."""
    from pisa.brain.desk import gateway_think

    return DeskService(
        pool, night, owner=owner, clock=clock, ideas=ideas, think=gateway_think(gateway), read=reader(places)
    )


async def _report_sheet(owner_loop: Any, owner: Owner, pool: AsyncConnectionPool) -> dict[str, Any] | None:
    return await owner_loop.sheet(await owner.lang(pool))


def load_voice_models(settings: Settings) -> Models:
    """The gate's models from the image (`PISA_VOICE_MODELS_DIR`), loaded at the first gated talk."""
    from pisa.connectors import voice_models as vm

    root = settings.voice_models_dir
    return Models(vm.SherpaEmbedder(root, settings.voice_embedder), vm.silero(root), vm.SmartTurn(root))
