# Light Bulb Replacement Wizard

En lokal Home Assistant-integrasjon som lar en ny pære overta den gamle pærens
navn og entity-ID. Den nye pæren må først være lagt til Home Assistant gjennom
for eksempel ZHA, Zigbee2MQTT, Hue eller Matter.

Krever **Home Assistant 2026.10 eller nyere**. Testet med Core **2026.10.2**.
Eldre versjoner støttes ikke. ZHA-adapteren er utviklet mot ZHA **2.3.1** og
Zigpy **2.3.0**, som følger denne HA-versjonen.

## Hva skjer ved et bytte?

Eksempel: gammel pære er `light.stue`, ny pære er `light.ny_paere`.

1. Gammel pære får entity-ID `light.stue_old` og visningsnavn `Stue_OLD`.
2. Ny pære overtar `light.stue` og visningsnavnet `Stue`.
3. Den nye entiteten overtar effektivt rom, aliaser og egendefinert ikon.
   Gamle entitets-/enhetsetiketter legges til den nye entitetens etiketter.
4. Gamle entitetens egne etiketter og aliaser fjernes. Entiteten deaktiveres,
   men slettes ikke. Den fysiske pæren fjernes ikke fra nettverket.

HA entity-ID-er må være små bokstaver, derfor `_old` i ID-en og `_OLD` i navnet.
Pærenes **unique-ID, device-ID, register-UUID og opprinnelige integrasjoner
beholdes**. Dette lar den nye pæren fortsatt styres av riktig maskinvareintegrasjon.

Grupper, scener, automasjoner, skript og dashboards med eksplisitte referanser
til `light.stue` bruker dermed den nye pæren. Konfigurasjonen deres skrives ikke
om. Tilstandsendringer sendes under byttet og kan utløse automasjoner.

## Installasjon og bruk

1. Kopier mappen `custom_components/light_bulb_replacement` til
   `/config/custom_components/light_bulb_replacement` på HA-installasjonen.
   Repositoriet er også strukturert for å legges til som en egendefinert
   integrasjon i HACS; det er ikke publisert i HACS-standardkatalogen.
2. Start Home Assistant på nytt.
3. Velg **Innstillinger → Enheter og tjenester → Legg til integrasjon →
   Light Bulb Replacement Wizard**.
4. Velg **Konfigurer → Bytt pære**, og velg gammel og ny `light`-entitet.
   For ZHA-pærer kan du også velge **Finn og kopier ZHA-bindinger og Zigbee-grupper**.
5. Les forhåndsvisningen. Ta en HA-sikkerhetskopi og stopp berørte automasjoner
   før du bekrefter. Rett opp eventuelle blokkerende referanser først.
6. Prøv en eksisterende gruppe, scene og automasjon. Aktiver automasjonene igjen
   når du har kontrollert resultatet.

Den gamle pæren kan være utilgjengelig. Den nye må være aktivert og tilgjengelig.
Lysgrupper kan ikke velges som pærer. Ved opptatt `_old`-ID stopper veiviseren
i stedet for å overskrive eller velge et uventet suffiks.

## Referanser og begrensninger

| Referanse / egenskap | Håndtering |
| --- | --- |
| Gammel entity-ID i HA-grupper, scener, automasjoner, skript og dashboards | Beholdes; peker nå til den nye pæren. |
| Device-ID i lastede automasjoner og skript | Oppdages; byttet blokkeres. Endre til entity-ID, eller bygg om maskinvareavhengige utløsere først. |
| Gammel register-UUID i konfigurasjonsoppføringenes data/valg | Oppdages; byttet blokkeres. Endre referansen i den aktuelle hjelperens/integrasjonens UI. |
| Rom og etiketter | Kopieres til entiteten. Delte enheter og deres andre entiteter flyttes ikke. Arver ny enhet rom mens gammel pære mangler rom, må dette avklares først. |
| Farger, dimming, effekter og fargetemperatur | Fargemoduser og støttede funksjonsflagg sammenlignes når gamle pærens tilstand finnes. Scenenes verdier konverteres ikke; kontroller også Kelvin-område manuelt. |
| ZHA-bindinger og Zigbee-grupper | Kan kartlegges og kopieres med valgfri ZHA-støtte; se nedenfor. |
| Bindinger/grupper via Zigbee2MQTT eller Hue | Må settes opp i den opprinnelige integrasjonen. |
| Historikk og statistikk | Migreres ikke. Navnebyttet lover ikke sammenhengende historikk for ny maskinvare. |
| Assist/Alexa/Google-eksponering og integrasjonsspesifikke innstillinger | Må kontrolleres manuelt; de følger ikke nødvendigvis entity-ID-en. |
| Referanser til ny pæres tidligere entity-ID | Skrives ikke om. Disse blir ugyldige og må ryddes manuelt. |
| Inaktive/ikke lastede automasjoner, dynamiske maler og eksterne systemer | Kan ikke fullstendig kartlegges. Må kontrolleres manuelt. |

Dette er en første versjon for entity-ID-baserte oppsett. Den garanterer ikke
automatisk migrering av alle typer maskinvare- og tredjepartsreferanser.
Den bytter én `light`-entitet om gangen; andre entiteter på samme enhet, som
strømforbrukssensorer eller flere lyskanaler, behandles ikke.

## Kopiere ZHA-bindinger og Zigbee-grupper

Velg ZHA-kopiering etter at begge pærer er lagt til samme ZHA-nettverk.
Skanningen leser bindingstabellene til alle enheter ZHA kjenner, inkludert
fjernkontroller og koordinatoren. Det er nødvendig fordi en binding
**fjernkontroll → gammel pære** ligger på fjernkontrollen, ikke på pæren.
Alle sider av hver tabell leses, med inntil tre samtidige avlesninger og en
tidsgrense per enhet. Dette kan ta flere minutter på et stort nettverk.

Veiviseren viser direkte bindinger som peker til eller kommer fra den valgte
lyskanalen, Zigbee-gruppene den tilhører, samt enheter som ikke kunne leses.
Den bruker lysentitetenes faktiske ZHA-endepunkter, også når gammel og ny pære
har forskjellige endepunktnumre. Entiteter og gruppemedlemskap for andre
lyskanaler på samme enhet erstattes ikke.
Bindinger med manglende klynger, ulik Zigbee-profil eller produsentspesifikke
klynger blokkerer kopiering; funksjoner gjettes ikke.

Vekk batterifjernkontroller før du skanner, og hold dem våkne gjennom kopieringen.
En pære som er helt død kan ikke levere sin egen bindingstabell. Veiviseren kan
likevel finne innkommende bindinger på andre enheter og hente kjente
gruppemedlemskap fra ZHA sin mellomlagring. Slike resultater merkes som
ufullstendige. Standardvalget er å stoppe til du enten skanner på nytt eller
uttrykkelig godtar at bare kjente oppføringer kopieres. Et tomt resultat med
feil betyr **ikke** at pæren var uten bindinger.

Ved bekreftelse opprettes bare manglende bindinger og gruppemedlemskap på den nye
pæren. Bindinger fra fjernkontroller til en Zigbee-gruppe trenger ingen endring;
den nye pæren legges til samme gruppe. Protokollsvar kontrolleres, og alle
forventede oppføringer leses tilbake før entity-ID-ene byttes.

Gamle bindinger og medlemskap beholdes for å gjøre tilbakeføring mulig. Begge
pærer kan derfor reagere på direkte fjernkontroller eller gruppemeldinger,
selv om gammel HA-entitet er deaktivert. Koble fra den gamle pæren etter at du
har kontrollert ny pære. Vent med å slette den fra ZHA til du ikke lenger trenger
angremuligheten. Ved full bindingstabell kan du måtte rydde gamle oppføringer
manuelt før kopiering.

ZHA-operasjonene lagres i samme gjenopprettingsjournal som navnebyttet. Ved feil
forsøkes tilbakeføring. Angre fjerner bare bindinger og medlemskap veiviseren
la til; eksisterende oppføringer og opprinnelige bindinger beholdes. Tapte svar
eller utilgjengelige enheter kan kreve ny gjenoppretting med enhetene våkne.
Et annet Zigbee-nettverk eller en annen koordinator blokkerer radioendringer.

Støtten bruker ZHA sin eksisterende gateway og Zigpy-kommandoer. Ingen egen
tilkobling til Zigbee-adapteren opprettes. Innebygde Zigbee-scener,
attributtrapportering, produsentspesifikke innstillinger og HA-automasjoners
device-ID-er kopieres ikke. Dette må kontrolleres separat. Testene simulerer
radioen; fysisk ZHA-utstyr er foreløpig ikke testet.

## Angre og gjenoppretting

**Konfigurer → Angre / gjenopprett siste bytte** gjenoppretter de opprinnelige
ID-ene og innstillingene for begge entiteter, inkludert tidligere aktivering.
Kun siste bytte beholdes. Et nytt bytte erstatter forrige angrejournal.
Senere endringer i disse navnene, rommene, ikonene, aliasene eller etikettene
overskrives ved gjenoppretting.

Integrasjonen lagrer journalen gjennom Home Assistants Storage-API før første
endring. Feil under byttet utløser tilbakeføring. Etter et avbrudd blokkeres nye
bytter til det uferdige byttet er gjenopprettet. Gjenoppretting bruker
register-UUID-er for å finne riktig maskinvare og nekter å overta ID-er som nå
tilhører andre entiteter. Journalen beholdes ved avlasting av integrasjonen.
Den skriver aldri direkte til HA sine registerfiler eller YAML-filer.

Dette er ikke en full HA-transaksjon: historikk, allerede utløste automasjoner,
endringer i eksterne systemer og strømbrudd under lagring kan ikke angres av
veiviseren. HA-sikkerhetskopien er fortsatt nødvendig.

## Utvikling

Bruk eksisterende checkout i skymiljøet. Hver skyoppgave er allerede isolert;
det trengs ikke et ekstra Git-worktree.

Med Python 3.14.2+ og `uv`:

```sh
uv sync --frozen
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Testene starter ekte Home Assistant-komponenter med to simulerte lyspærer.
De prøver registerbytte, fysiske identiteter, `light.turn_on`, eksisterende
grupper/scener/automasjoner, brukerflyten, feiltilbakeføring og journalgjenoppretting.
ZHA-testene bruker ekte Zigpy-endepunkter og protokolltyper med simulert radio,
inkludert inn-/utgående bindinger, gruppemedlemskap, tapte svar og avbrudd.
Ingen ekte HA-installasjon eller fysiske pærer er endret av testene.
