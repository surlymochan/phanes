import type { PartialTranslation } from "../../message";
import type { messages as source } from "../en/sitePage";

export const messages = {
  "sitePage.error.title": "Deze pagina kan niet worden geladen",
  "sitePage.error.body":
    "Controleer je verbinding en laad de pagina opnieuw. Laadt hij nog steeds niet, ga dan terug naar de startpagina.",
  "sitePage.error.reload": "Pagina opnieuw laden",
  "sitePage.error.home": "Terug naar start",
  "sitePage.join.title": "Word lid van een Volto-server",
  "sitePage.join.description": "Open een privé-uitnodiging voor een server in Volto.",
  "sitePage.join.eyebrow": "Privé-uitnodiging",
  "sitePage.join.intro":
    "Open deze uitnodiging in Volto. De app controleert de server en vraagt je om te bevestigen voordat er verbinding wordt gemaakt.",
  "sitePage.join.invalid":
    "Deze uitnodigingslink is ongeldig of onvolledig. Vraag de eigenaar van de server om een nieuwe uitnodiging.",
  "sitePage.join.open": "Volto openen",
  "sitePage.join.browser": "Openen in browser",
  "sitePage.join.download": "Volto downloaden",
  "sitePage.join.notOpened":
    "Is Volto niet geopend? Installeer het met {download}, of start Volto één keer zodat het uitnodigingslinks kan openen. Je kunt ook {browser} gebruiken.",
  "sitePage.join.mobile":
    "Is Volto geïnstalleerd? Tik dan op {open}. Je kunt deze link ook plakken bij Server toevoegen in de app.",
  "sitePage.join.singleUse":
    "Een eenmalige link werkt één keer en verloopt daarna. Een permanente link werkt totdat de eigenaar hem intrekt. Volto toont de vervaldatum voordat je lid wordt.",
  "sitePage.join.email": "Kwam de uitnodiging per e-mail? Log dan in bij Volto met dat e-mailadres.",
  "sitePage.join.share": "Deze link geeft toegang tot een server. Deel hem alleen met mensen die je wilt uitnodigen.",
  "sitePage.connect.title": "Terug naar Volto",
  "sitePage.connect.failedTitle": "De verbinding is niet voltooid",
  "sitePage.connect.openApp": "Volto openen",
  "sitePage.connect.tryAgainApp": "Opnieuw proberen in Volto",
  "sitePage.connect.tryAgainBrowser": "Opnieuw proberen in browser",
  "sitePage.connect.doneInWebClient":
    "Je Volto-tabblad rondt de verbinding met {name} af. Je kunt dit tabblad sluiten.",
  "sitePage.connect.doneInApp": "Volto rondt de verbinding met {name} af op je computer.",
  "sitePage.connect.failure.cancelled": "De verbinding met {name} is geannuleerd. Er is niets gewijzigd.",
  "sitePage.connect.failure.expired": "Het inloggen bij {name} is verlopen. Begin de verbinding opnieuw.",
  "sitePage.connect.failure.exchange": "{name} heeft de verbinding niet bevestigd. Probeer het opnieuw.",
  "sitePage.connect.failure.enterprise": "Verbind één Slack-werkruimte, niet een hele organisatie.",
  "sitePage.connect.failure.takenSlack":
    "Een andere Volto-server beantwoordt deze Slack-werkruimte al. Verbreek daar de verbinding en probeer het opnieuw.",
  "sitePage.connect.failure.takenDiscord":
    "Een andere Volto-server beantwoordt deze Discord-server al. Verbreek daar de verbinding en probeer het opnieuw.",
  "sitePage.connect.failure.unavailable": "Volto kan nu geen verbinding maken met {name}. Probeer het later opnieuw.",
  "sitePage.connect.failure.tabClosed":
    "Het Volto-tabblad dat de verbinding met {name} startte, reageert niet. Open Volto in je browser en verbind {name} opnieuw.",
  "sitePage.connect.failure.failed": "{name} heeft de verbinding niet afgerond. Probeer het opnieuw.",
  "sitePage.connect.restartHint": "Open in Volto {marketplace}, dan {apps}, dan {name}, en kies {connect}.",
  "sitePage.connect.notOpened":
    "Start Volto en probeer het opnieuw. Staat Volto niet op deze computer? Download het dan en verbind {name} vanuit de app.",
  "sitePage.openApp.title": "Is er niets geopend?",
  "sitePage.openApp.dismiss": "Niet nu",
  "sitePage.billing.pageTitle": "Volto-facturering",
  "sitePage.billing.updatedTitle": "Facturering bijgewerkt",
  "sitePage.billing.updatedBody": "Ga terug naar Volto. Je kunt deze pagina sluiten.",
  "sitePage.billing.cancelledTitle": "Afrekenen geannuleerd",
  "sitePage.billing.cancelledBody": "Er is niets in rekening gebracht. Ga terug naar Volto.",
  "sitePage.report.pageTitle": "Een gehoste site melden — Volto",
  "sitePage.report.title": "Een gehoste site melden",
  "sitePage.report.intro": "Stuur een melding over schadelijke of onrechtmatige inhoud op een openbot.site-adres.",
  "sitePage.report.hostnameLabel": "Siteadres",
  "sitePage.report.hostnameMissing": "Vul het siteadres in",
  "sitePage.report.reasonLabel": "Reden",
  "sitePage.report.reason.phishing": "Phishing",
  "sitePage.report.reason.malware": "Malware",
  "sitePage.report.reason.abuse": "Misbruik",
  "sitePage.report.reason.copyright": "Auteursrecht",
  "sitePage.report.reason.other": "Overig",
  "sitePage.report.detailsLabel": "Details",
  "sitePage.report.optional": "Optioneel",
  "sitePage.report.detailsPlaceholder": "Wat is er mis met deze site?",
  "sitePage.report.send": "Melding sturen",
  "sitePage.report.sending": "Versturen…",
  "sitePage.report.tooMany": "Te veel meldingen. Probeer het later opnieuw.",
  "sitePage.report.sendFailed": "Kan de melding niet versturen. Probeer het opnieuw.",
  "sitePage.report.writeTo": "Je kunt ook schrijven naar {email}.",
  "sitePage.report.sentTitle": "Melding verstuurd",
  "sitePage.report.sentBody": "Bedankt. We hebben je melding ontvangen.",
  "sitePage.report.home": "Naar de startpagina",
} as const satisfies PartialTranslation<typeof source>;
