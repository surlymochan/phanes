import type { PartialTranslation } from "../../message";
import type { messages as source } from "../en/connector";

export const messages = {
  "connector.github.title": "GitHub",
  "connector.github.description":
    "Alle Agenten auf diesem Computer können deine GitHub-Repositories, Issues und Pull Requests sowie gh und git verwenden.",
  "connector.github.connect": "GitHub verbinden",
  "connector.github.pendingTitle": "Gib diesen Code auf GitHub ein",
  "connector.github.pendingDescription":
    "GitHub wurde in deinem Browser geöffnet. Gib dort den Code ein, um die Verbindung herzustellen.",
  "connector.github.waiting": "Warten auf GitHub",
  "connector.github.copyCode": "Code kopieren",
  "connector.github.codeCopied": "Kopiert",
  "connector.github.openGitHub": "GitHub öffnen",
  "connector.github.cancel": "Abbrechen",
  "connector.github.connectedAs": "Verbunden als @{login}",
  "connector.github.repositoriesTitle": "Repositories",
  "connector.github.repositoriesDescription":
    "Agenten können nur Repositories verwenden, in denen die volto GitHub App installiert ist.",
  "connector.github.chooseRepositories": "Repositories auswählen",
  "connector.github.repositoriesLoading": "Repositories werden von GitHub gelesen",
  "connector.github.repositoriesFailed": "volto konnte die Repositories von GitHub nicht lesen.",
  "connector.github.noRepositories": "Die volto GitHub App ist noch in keinem Repository installiert.",
  "connector.github.moreRepositories": {
    one: "Und {count} weiteres Repository",
    other: "Und {count} weitere Repositories",
  },
  "connector.github.private": "Privat",
  "connector.github.disconnect": "Trennen",
  "connector.github.disconnectTitle": "GitHub trennen",
  "connector.github.disconnectSummary":
    "Alle Agenten verlieren den Zugriff auf GitHub. Deine Chats und Dateien bleiben erhalten.",
  "connector.github.expiredTitle": "Die GitHub-Verbindung ist abgelaufen",
  "connector.github.expiredDescription":
    "Verbinde dich erneut als @{login}, damit Agenten wieder auf GitHub zugreifen können.",
  "connector.github.reconnect": "Neu verbinden",
  "connector.github.actionFailed": "volto konnte die GitHub-Verbindung nicht ändern.",
  "connector.github.statusConnected": "Verbunden",
  "connector.github.statusConnecting": "Wird verbunden",
  "connector.github.statusExpired": "Abgelaufen",
  "connector.github.accountTitle": "Konto",
  "connector.github.filterPlaceholder": {
    one: "{count} Repository filtern",
    other: "{count} Repositories filtern",
  },
  "connector.github.filterLabel": "Repositories filtern",
  "connector.github.noMatch": "Kein Repository entspricht „{query}“.",
  "connector.github.stepSignIn": "Anmelden",
  "connector.github.stepConnected": "Verbunden",
  "connector.github.requestingCode": "volto fordert einen Code von GitHub an.",
  "connector.github.codeLabel": "Code {code}",
  "connector.github.failedTitle": "GitHub wurde nicht verbunden",
  "connector.github.cancelConnecting": "GitHub-Verbindung abbrechen",
  "connector.github.connectedSummary": {
    one: "{count} Repository · alle Agenten auf diesem Computer",
    other: "{count} Repositories · alle Agenten auf diesem Computer",
  },
  "connector.github.done": "Fertig",
  "connector.github.later": "Später",
  "connector.github.disconnectConfirmTitle": "GitHub trennen?",
  "connector.github.disconnectConfirmDescription":
    "Beim Trennen wird die Anmeldung von @{login} von diesem Computer entfernt.",
  "connector.github.disconnectEffectTools": "Alle Agenten verlieren die GitHub-Werkzeuge, gh und git.",
  "connector.github.disconnectEffectRevoke":
    "Danach öffnet sich GitHub. Dort kannst du den Zugriff von volto widerrufen.",
  "connector.github.disconnectEffectKept": "Chats, Dateien und Agentenspeicher bleiben auf diesem Computer erhalten.",
  // The same text for the connection of a joined server. {host} is the server name, such as Studio.
  "connector.github.descriptionHost":
    "Alle Agenten auf {host} können deine GitHub-Repositories, Issues und Pull Requests sowie gh und git verwenden.",
  "connector.github.connectedSummaryHost": {
    one: "{count} Repository · alle Agenten auf {host}",
    other: "{count} Repositories · alle Agenten auf {host}",
  },
  "connector.github.disconnectConfirmDescriptionHost":
    "Beim Trennen wird die Anmeldung von @{login} von {host} entfernt.",
  "connector.github.disconnectEffectKeptHost": "Chats, Dateien und Agentenspeicher bleiben auf {host} erhalten.",
  "connector.github.keepConnected": "Verbunden lassen",
  "connector.github.close": "Schließen",
  "connector.dangerZone": "Gefahrenbereich",
  "connector.statusUnknown": "Unbekannt",
  "connector.readFailed": "volto konnte den Status dieser Verbindung nicht lesen.",
  "connector.slack.title": "Slack",
  "connector.slack.description":
    "Personen erwähnen @volto in Slack oder senden ihm eine Direktnachricht. Der Slack Orchestrator fragt den passenden Agenten und antwortet.",
  "connector.slack.statusConnected": "Verbunden",
  "connector.slack.statusAttention": "Handlungsbedarf",
  "connector.slack.summaryConnected": "{workspace} · Slack Orchestrator antwortet",
  "connector.slack.summaryNoAgent": "{workspace} · Noch kein Agent antwortet",
  "connector.slack.attentionTitle": {
    one: "{count} Arbeitsbereich benötigt Aufmerksamkeit",
    other: "{count} Arbeitsbereiche benötigen Aufmerksamkeit",
  },
  "connector.slack.attentionDescription": "Der Status unten zeigt, was zu tun ist.",
  "connector.slack.connect": "Slack verbinden",
  "connector.slack.addAgent": "Agenten hinzufügen",
  "connector.slack.actionFailed": "Slack hat die Änderung nicht angenommen",
  "connector.slack.workspaceTitle": "Arbeitsbereiche",
  "connector.slack.workspaceDescription": "Personen erwähnen @volto oder senden ihm eine Direktnachricht.",
  "connector.slack.disconnectWorkspace": "Trennen",
  "connector.slack.disconnectAllTitle": "Slack trennen",
  "connector.slack.disconnectAllSummary":
    "Alle Workspaces werden getrennt. Niemand erreicht deine Agenten dann noch über @volto.",
  "connector.slack.disconnectAllSummaryOrchestrator":
    "Alle Workspaces werden getrennt. Der Slack Orchestrator und seine Unterhaltungen werden aus volto gelöscht.",
  "connector.slack.missingScopes":
    "volto hat diese Berechtigungen in Slack nicht: {scopes}. Trenne den Arbeitsbereich und verbinde ihn erneut.",
  "connector.slack.retryAt": "Slack hat volto gebeten zu warten. volto versucht es um {time} erneut.",
  "connector.slack.reconnect": "Neu verbinden",
  "connector.slack.pause": "Pausieren",
  "connector.slack.resume": "Fortsetzen",
  "connector.slack.rowAction": "{action}: {name}",
  "connector.slack.orchestratorTitle": "Slack Orchestrator",
  "connector.slack.orchestratorDescription":
    "Dieser Agent erhält jede Anfrage aus Slack. Kurze Anfragen beantwortet er selbst. Andere Aufgaben gibt er an den passenden Agenten weiter und veröffentlicht die Antwort im Thread.",
  "connector.slack.orchestratorNone": "Noch kein Agent antwortet",
  "connector.slack.orchestratorNoneDescription": "Füge den Slack Orchestrator hinzu, sonst erhält Slack keine Antwort.",
  "connector.slack.inviteNote":
    "volto tritt allen öffentlichen Kanälen und jedem neuen Kanal automatisch bei. Lade ihn in einen privaten Kanal ein: /invite @volto.",
  "connector.slack.stepWorkspace": "Arbeitsbereich",
  "connector.slack.stepAgent": "Agent",
  "connector.slack.connectTitle": "Slack-Arbeitsbereich verbinden",
  "connector.slack.connectDescription":
    "volto installiert eine App namens volto im Arbeitsbereich. Sie tritt allen öffentlichen Kanälen bei.",
  "connector.slack.connectStepBrowser": "Slack öffnet sich in deinem Browser",
  "connector.slack.connectStepAllow": "Wähle den Arbeitsbereich oben rechts und dann „Zulassen“",
  "connector.slack.connectStepReturn": "Dieser Dialog wird fortgesetzt, sobald Slack fertig ist",
  "connector.slack.connectInSlack": "In Slack verbinden",
  "connector.slack.connectWaiting": "Warten auf Slack. Schließe die Installation in deinem Browser ab.",
  "connector.slack.agentStepTitle": "Slack Orchestrator hinzufügen",
  "connector.slack.agentStepDescription":
    "Dieser neue Agent beantwortet alles, was Personen in {workspace} an @volto senden.",
  "connector.slack.orchestratorName": "Slack Orchestrator",
  "connector.slack.orchestratorRole": "Antwortet in Slack und fragt das Team",
  "connector.slack.orchestratorDoesReceive": "Erhält jede Slack-Anfrage zuerst",
  "connector.slack.orchestratorDoesDelegate": "Gibt jede Aufgabe an den am besten geeigneten Agenten weiter",
  "connector.slack.orchestratorDoesAnswer": "Veröffentlicht die Antwort im Slack-Thread",
  "connector.slack.orchestratorModel": "Modell",
  "connector.slack.doneTitle": "volto ist in {workspace}",
  "connector.slack.doneDescription":
    "Erwähne @volto in einem öffentlichen Kanal oder sende ihm eine Direktnachricht.",
  "connector.slack.done": "Fertig",
  "connector.slack.disconnectTitle": "{workspace} trennen?",
  "connector.slack.disconnectDescription":
    "volto antwortet nicht mehr in {workspace} und entfernt sein Slack-Token von diesem Computer.",
  "connector.slack.disconnectDescriptionHost":
    "volto antwortet nicht mehr in {workspace} und entfernt sein Slack-Token von {host}.",
  "connector.slack.disconnectEffect":
    "Personen in {workspace} können deine Agenten nicht mehr über @volto erreichen.",
  "connector.slack.removeEffectKept": "Die Unterhaltungen und der Slack Orchestrator bleiben in volto erhalten.",
  "connector.slack.removeEffectDeleted": "Der Slack Orchestrator und seine Unterhaltungen werden aus volto gelöscht.",
  "connector.slack.keep": "Verbunden lassen",
  "connector.slack.close": "Schließen",
  "connector.slack.addWorkspace": "Arbeitsbereich hinzufügen",
  "connector.slack.limitReached": {
    one: "Du kannst höchstens {count} Arbeitsbereich verbinden.",
    other: "Du kannst höchstens {count} Arbeitsbereiche verbinden.",
  },
  "connector.slack.summaryMany": {
    one: "{count} Arbeitsbereich · Slack Orchestrator antwortet",
    other: "{count} Arbeitsbereiche · Slack Orchestrator antwortet",
  },
  "connector.discord.title": "Discord",
  "connector.discord.description":
    "Personen erwähnen @volto in einem Kanal eines Discord-Servers. Der Discord Orchestrator fragt den passenden Agenten und antwortet.",
  "connector.discord.statusConnected": "Verbunden",
  "connector.discord.statusAttention": "Handlungsbedarf",
  "connector.discord.summaryConnected": "{workspace} · Discord Orchestrator antwortet",
  "connector.discord.summaryNoAgent": "{workspace} · Noch kein Agent antwortet",
  "connector.discord.attentionTitle": {
    one: "{count} Discord-Server benötigt Aufmerksamkeit",
    other: "{count} Discord-Server benötigen Aufmerksamkeit",
  },
  "connector.discord.attentionDescription": "Der Status unten zeigt, was zu tun ist.",
  "connector.discord.connect": "Discord verbinden",
  "connector.discord.addAgent": "Agenten hinzufügen",
  "connector.discord.actionFailed": "Discord hat die Änderung nicht angenommen",
  "connector.discord.workspaceTitle": "Discord-Server",
  "connector.discord.workspaceDescription": "Erwähne @volto in einem Kanal. Antworte volto, um fortzufahren.",
  "connector.discord.disconnectWorkspace": "Trennen",
  "connector.discord.disconnectAllTitle": "Discord trennen",
  "connector.discord.disconnectAllSummary":
    "Alle Server werden getrennt. Niemand erreicht deine Agenten dann noch über @volto.",
  "connector.discord.disconnectAllSummaryOrchestrator":
    "Alle Server werden getrennt. Der Discord Orchestrator und seine Unterhaltungen werden aus volto gelöscht.",
  "connector.discord.missingScopes":
    "volto hat diese Berechtigungen in Discord nicht: {scopes}. Trenne den Discord-Server und verbinde ihn erneut.",
  "connector.discord.retryAt": "Discord hat volto gebeten zu warten. volto versucht es um {time} erneut.",
  "connector.discord.reconnect": "Neu verbinden",
  "connector.discord.pause": "Pausieren",
  "connector.discord.resume": "Fortsetzen",
  "connector.discord.rowAction": "{action}: {name}",
  "connector.discord.orchestratorTitle": "Discord Orchestrator",
  "connector.discord.orchestratorDescription":
    "Dieser Agent erhält jede Anfrage aus Discord. Kurze Anfragen beantwortet er selbst. Andere Aufgaben gibt er an den passenden Agenten weiter und sendet die Antwort im Kanal.",
  "connector.discord.orchestratorNone": "Noch kein Agent antwortet",
  "connector.discord.orchestratorNoneDescription":
    "Füge den Discord Orchestrator hinzu, sonst erhält Discord keine Antwort.",
  "connector.discord.channelsNote": "volto sieht die Kanäle, die seine Rolle sehen darf.",
  "connector.discord.stepWorkspace": "Discord-Server",
  "connector.discord.stepAgent": "Agent",
  "connector.discord.connectTitle": "Discord-Server verbinden",
  "connector.discord.connectDescription": "volto fügt dem Discord-Server einen Bot namens volto hinzu.",
  "connector.discord.connectStepBrowser": "Discord öffnet sich in deinem Browser",
  "connector.discord.connectStepAllow": "Wähle den Server und dann „Autorisieren“",
  "connector.discord.connectStepReturn": "Dieser Dialog wird fortgesetzt, sobald Discord fertig ist",
  "connector.discord.connectInDiscord": "In Discord verbinden",
  "connector.discord.connectWaiting": "Warten auf Discord. Schließe die Autorisierung in deinem Browser ab.",
  "connector.discord.agentStepTitle": "Discord Orchestrator hinzufügen",
  "connector.discord.agentStepDescription":
    "Dieser neue Agent beantwortet alles, was Personen in {workspace} an @volto senden.",
  "connector.discord.orchestratorName": "Discord Orchestrator",
  "connector.discord.orchestratorRole": "Antwortet in Discord und fragt das Team",
  "connector.discord.orchestratorDoesReceive": "Erhält jede Discord-Anfrage zuerst",
  "connector.discord.orchestratorDoesDelegate": "Gibt jede Aufgabe an den am besten geeigneten Agenten weiter",
  "connector.discord.orchestratorDoesAnswer": "Sendet die Antwort im Discord-Kanal",
  "connector.discord.orchestratorModel": "Modell",
  "connector.discord.doneTitle": "volto ist in {workspace}",
  "connector.discord.doneDescription": "Erwähne @volto in einem Kanal. Antworte volto, um fortzufahren.",
  "connector.discord.done": "Fertig",
  "connector.discord.disconnectTitle": "{workspace} trennen?",
  "connector.discord.disconnectDescription":
    "volto antwortet nicht mehr in {workspace} und entfernt die Discord-Verbindung von diesem Computer.",
  "connector.discord.disconnectDescriptionHost":
    "volto antwortet nicht mehr in {workspace} und entfernt die Discord-Verbindung von {host}.",
  "connector.discord.disconnectEffect":
    "Personen in {workspace} können deine Agenten nicht mehr über @volto erreichen.",
  "connector.discord.removeEffectKept": "Die Unterhaltungen und der Discord Orchestrator bleiben in volto erhalten.",
  "connector.discord.removeEffectDeleted":
    "Der Discord Orchestrator und seine Unterhaltungen werden aus volto gelöscht.",
  "connector.discord.keep": "Verbunden lassen",
  "connector.discord.close": "Schließen",
  "connector.discord.addWorkspace": "Discord-Server hinzufügen",
  "connector.discord.limitReached": {
    one: "Du kannst höchstens {count} Discord-Server verbinden.",
    other: "Du kannst höchstens {count} Discord-Server verbinden.",
  },
  "connector.discord.summaryMany": {
    one: "{count} Discord-Server · Discord Orchestrator antwortet",
    other: "{count} Discord-Server · Discord Orchestrator antwortet",
  },
  "connector.discord.accessTitle": "Wer volto nutzen kann",
  "connector.discord.accessDescription": "volto antwortet nur diesen Personen. Erwähnungen von anderen ignoriert es.",
  "connector.discord.accessEveryone": "Alle auf dem Server",
  "connector.discord.accessEveryoneDescription":
    "Jeder, der in einem Kanal schreiben kann, den volto sieht, kann es erwähnen.",
  "connector.discord.accessRoles": "Mitglieder mit {roles}",
  "connector.discord.accessChoose": "Rollen wählen",
  "connector.discord.accessDialogTitle": "Wer kann volto in {workspace} nutzen?",
  "connector.discord.accessDialogDescription": "Ohne ausgewählte Rolle können alle volto erwähnen.",
  "connector.discord.accessLoading": "Rollen werden geladen…",
  "connector.discord.accessLoadFailed": "Discord-Rollen wurden nicht geladen",
  "connector.discord.accessNoRoles": "Dieser Server hat keine Rollen zur Auswahl.",
  "connector.discord.accessRoleDeleted": "{role} (gelöscht)",
  "connector.discord.accessSave": "Speichern",
  "connector.discord.accessHostUpdate": "Aktualisiere volto auf {host}, um Rollen zu wählen.",
  "connector.telegram.title": "Telegram",
  "connector.telegram.description":
    "Füge den volto-Bot zu einer Telegram-Gruppe hinzu oder öffne einen direkten Chat mit ihm. Der Telegram-Orchestrator fragt den passenden Agenten und antwortet.",
  "connector.telegram.statusConnected": "Verbunden",
  "connector.telegram.statusAttention": "Handlungsbedarf",
  "connector.telegram.summaryConnected": {
    one: "{count} Chat · Telegram-Orchestrator antwortet",
    other: "{count} Chats · Telegram-Orchestrator antwortet",
  },
  "connector.telegram.summaryNoAgent": {
    one: "{count} Chat · Noch kein Agent antwortet",
    other: "{count} Chats · Noch kein Agent antwortet",
  },
  "connector.telegram.attentionTitle": {
    one: "{count} Chat erfordert Aufmerksamkeit",
    other: "{count} Chats erfordern Aufmerksamkeit",
  },
  "connector.telegram.attentionDescription": "Der Status unten zeigt, was zu tun ist.",
  "connector.telegram.connect": "Telegram verbinden",
  "connector.telegram.addAgent": "Agenten hinzufügen",
  "connector.telegram.actionFailed": "Telegram hat die Änderung nicht akzeptiert",
  "connector.telegram.chatsTitle": "Chats",
  "connector.telegram.groupDescription": "Personen erwähnen den Bot oder antworten auf seine Nachrichten.",
  "connector.telegram.directDescription": "Jede Nachricht in diesem Chat geht an den Telegram-Orchestrator.",
  "connector.telegram.helpRemoved":
    "Der volto-Bot ist nicht mehr in diesem Chat. Trenne den Chat und füge den Bot dann erneut hinzu.",
  "connector.telegram.helpRelayUnavailable":
    "volto kann auf diesem Computer keine Telegram-Nachrichten empfangen. Melde dich an, gib diesem Computer in den Servereinstellungen einen Namen und lass volto geöffnet.",
  "connector.telegram.helpRelayUnavailableHost":
    "volto auf {host} kann keine Telegram-Nachrichten empfangen. Melde dich auf {host} an, gib dem Server in den Servereinstellungen einen Namen und lass volto geöffnet.",
  "connector.telegram.helpError": "volto kann diesen Chat nicht erreichen. Verbinde ihn erneut oder trenne den Chat.",
  "connector.telegram.retryAt": "Telegram hat volto gebeten zu warten. Neuer Versuch um {time}.",
  "connector.telegram.pause": "Pausieren",
  "connector.telegram.resume": "Fortsetzen",
  "connector.telegram.reconnect": "Neu verbinden",
  "connector.telegram.disconnectChat": "Trennen",
  "connector.telegram.disconnectAllTitle": "Telegram trennen",
  "connector.telegram.disconnectAllSummary":
    "Alle Chats werden getrennt. Niemand erreicht deine Agenten dann noch über den volto-Bot.",
  "connector.telegram.disconnectAllSummaryOrchestrator":
    "Alle Chats werden getrennt. Der Telegram-Orchestrator und seine Unterhaltungen werden aus volto gelöscht.",
  "connector.telegram.rowAction": "{action}: {name}",
  "connector.telegram.linkChat": "Chat verknüpfen",
  "connector.telegram.addToGroup": "Zu Gruppe hinzufügen",
  "connector.telegram.openDirectChat": "Direkten Chat öffnen",
  "connector.telegram.orchestratorTitle": "Telegram-Orchestrator",
  "connector.telegram.orchestratorDescription":
    "Dieser Agent empfängt die Nachrichten für volto aus all deinen Telegram-Chats. Kurze beantwortet er selbst, andere Aufgaben gibt er an den passenden Agenten weiter und postet die Antwort im Chat.",
  "connector.telegram.orchestratorNone": "Noch kein Agent antwortet",
  "connector.telegram.orchestratorNoneDescription":
    "Füge den Telegram-Orchestrator hinzu, sonst bekommt Telegram keine Antwort.",
  "connector.telegram.mentionNote":
    "Erwähne den Bot in einer Gruppe oder antworte auf seine Nachrichten. In einem direkten Chat geht jede Nachricht an den Bot.",
  "connector.telegram.stepChat": "Chat",
  "connector.telegram.stepAgent": "Agent",
  "connector.telegram.connectTitle": "Telegram-Chat verknüpfen",
  "connector.telegram.connectDescription":
    "Füge den volto-Bot zu einer Gruppe hinzu oder öffne einen direkten Chat mit ihm. Ein Bot bedient all deine Chats.",
  "connector.telegram.connectStepBrowser": "Telegram wird in deinem Browser geöffnet",
  "connector.telegram.connectStepPick": "Wähle die Gruppe aus oder tippe im direkten Chat auf „Starten“",
  "connector.telegram.connectStepReturn": "Dieser Dialog geht weiter, sobald der Chat verknüpft ist",
  "connector.telegram.connectWaiting": "Warten auf Telegram. Wähle den Chat in Telegram aus.",
  "connector.telegram.agentStepTitle": "Telegram-Orchestrator hinzufügen",
  "connector.telegram.agentStepDescription":
    "Dieser neue Agent beantwortet die Nachrichten für volto in {chat} und in jedem Chat, den du später verknüpfst.",
  "connector.telegram.orchestratorName": "Telegram-Orchestrator",
  "connector.telegram.orchestratorRole": "Antwortet in Telegram und fragt das Team",
  "connector.telegram.orchestratorDoesReceive": "Empfängt jede Telegram-Nachricht für volto zuerst",
  "connector.telegram.orchestratorDoesDelegate": "Gibt jede Aufgabe an den am besten passenden Agenten weiter",
  "connector.telegram.orchestratorDoesAnswer": "Postet die Antwort im Telegram-Chat",
  "connector.telegram.orchestratorModel": "Modell",
  "connector.telegram.doneTitle": "volto ist in {chat}",
  "connector.telegram.done": "Fertig",
  "connector.telegram.disconnectTitle": "{chat} trennen?",
  "connector.telegram.leaveDescription": "Der volto-Bot verlässt {chat}, und volto antwortet dort nicht mehr.",
  "connector.telegram.disconnectEffect":
    "Personen in {chat} können deine Agenten nicht mehr über den volto-Bot erreichen.",
  "connector.telegram.removeEffectKept": "Die Unterhaltungen und der Telegram-Orchestrator bleiben in volto.",
  "connector.telegram.removeEffectDeleted":
    "Das ist der letzte Chat, daher werden der Telegram-Orchestrator und seine Unterhaltungen aus volto gelöscht.",
  "connector.telegram.keep": "Verbunden lassen",
  "connector.telegram.close": "Schließen",
  "connector.onePassword.title": "1Password",
  "connector.onePassword.description":
    "Teile einen eigenen 1Password-Tresor über ein Dienstkonto mit volto. So können sich Agenten im volto-Browser auf Websites anmelden.",
  "connector.onePassword.howItWorks":
    "Beim Verbinden werden in deinem 1Password-Konto ein Tresor „Shared with volto“ und ein Dienstkonto erstellt, das nur diesen Tresor lesen kann. volto füllt gespeicherte Anmeldedaten in seinem Browser auf diesem Computer aus. Nur Einträge, die du in diesen Tresor verschiebst, werden geteilt. Agenten sehen niemals ein Passwort.",
  "connector.onePassword.connect": "1Password verbinden",
  "connector.onePassword.setupTitle": "Einrichten",
  "connector.onePassword.stepCliTitle": "1Password CLI installieren",
  "connector.onePassword.stepCliChecking": "1Password CLI wird auf diesem Computer gesucht",
  "connector.onePassword.stepCliInstalling": "1Password CLI wird von 1Password heruntergeladen",
  "connector.onePassword.stepCliReady": "Version {version} ist installiert.",
  "connector.onePassword.stepCliMissing":
    "volto lädt sie von 1Password in seinen eigenen Ordner herunter. Dafür ist kein Administratorpasswort erforderlich.",
  "connector.onePassword.stepCliManual":
    "volto kann sie auf diesem Computer nicht installieren. Installiere sie über 1Password und kehre dann zu dieser Seite zurück.",
  "connector.onePassword.installCli": "Installieren",
  "connector.onePassword.stepAppTitle": "CLI-Integration aktivieren",
  "connector.onePassword.stepAppDescription":
    "Öffne in der 1Password-App Einstellungen > Entwickler und aktiviere „In 1Password CLI integrieren“.",
  "connector.onePassword.stepAppReady": "Die 1Password-App erlaubt der CLI, den geteilten Tresor zu erstellen.",
  "connector.onePassword.openApp": "1Password öffnen",
  "connector.onePassword.openAppFailed": "volto konnte 1Password nicht öffnen.",
  "connector.onePassword.checkAgain": "Erneut prüfen",
  "connector.onePassword.stepVaultTitle": "Geteilten Tresor erstellen",
  "connector.onePassword.stepVaultDescription":
    "volto erstellt den Tresor „Shared with volto“ und ein Dienstkonto, das ihn nur lesen kann. 1Password bittet dich um Bestätigung.",
  "connector.onePassword.useTokenDescription":
    "Füge statt dieser Schritte ein Dienstkonto-Token von 1Password.com ein.",
  "connector.onePassword.useTokenAction": "Token verwenden",
  "connector.onePassword.tokenLabel": "Dienstkonto-Token",
  "connector.onePassword.tokenPlaceholder": "ops_…",
  "connector.onePassword.connectWithToken": "Verbinden",
  "connector.onePassword.approveInApp": "Bestätige die Anfrage in der 1Password-App",
  "connector.onePassword.cancel": "Abbrechen",
  "connector.onePassword.chooseAccountTitle": "Konto auswählen",
  "connector.onePassword.chooseAccountDescription":
    "volto erstellt den geteilten Tresor in dem Konto, das du auswählst.",
  "connector.onePassword.useAccount": "Dieses Konto verwenden",
  "connector.onePassword.vaultTitle": "Geteilter Tresor",
  "connector.onePassword.vaultDescription":
    "Verschiebe Anmeldedaten in diesen Tresor in 1Password, damit Agenten sie verwenden können. Entferne sie, um die Nutzung zu beenden.",
  "connector.onePassword.loginsLoading": "Anmeldedaten werden aus 1Password gelesen",
  "connector.onePassword.loginCount": {
    one: "{count} Anmeldung",
    other: "{count} Anmeldungen",
  },
  "connector.onePassword.disconnect": "Trennen",
  "connector.onePassword.disconnectTitle": "1Password trennen",
  "connector.onePassword.disconnectSummary":
    "volto entfernt das Token. Der Tresor und das Dienstkonto bleiben in 1Password erhalten. Entferne sie dort, wenn du sie nicht mehr benötigst.",
  "connector.onePassword.actionFailed": "volto konnte die 1Password-Verbindung nicht ändern.",
  "connector.onePassword.statusConnected": "Verbunden",
  "connector.onePassword.statusConnecting": "Wird verbunden",
  "connector.onePassword.descriptionHost":
    "Teile einen eigenen 1Password-Tresor über ein Dienstkonto mit volto auf {host}, damit Agenten auf {host} sich in seinem Browser bei Websites anmelden können.",
  "connector.onePassword.howItWorksHost":
    "{host} verbindet sich mit einem Dienstkonto-Token. Erstelle auf 1Password.com ein Dienstkonto, das nur den Tresor lesen kann, den du mit volto teilst, und füge sein Token hier ein. {host} prüft das Token und speichert es; dieser Computer speichert es nicht. Agenten sehen nie ein Passwort.",
  "connector.onePassword.disconnectSummaryHost":
    "{host} vergisst das Token. Der Tresor und das Dienstkonto bleiben in 1Password; entferne sie dort, wenn du sie nicht mehr brauchst.",
  "connector.bitwarden.title": "Bitwarden",
  "connector.bitwarden.description": "Anmeldedaten aus Bitwarden im Browser ausfüllen.",
  "connector.bitwarden.setup":
    "Installiere die Bitwarden CLI und melde dich mit bw login an. Erstelle einen Ordner namens Shared with volto und lege dort nur Anmeldedaten ab, die Agenten verwenden dürfen. Führe bw unlock --raw aus und füge den Sitzungsschlüssel unten ein. Füge nicht dein Master-Passwort ein.",
  "connector.bitwarden.scope":
    "Alle Agenten auf diesem Computer können passende Anmeldedaten in Shared with volto verwenden. volto verwendet exakte HTTPS-Ursprünge. Einträge, die eine Master-Passwort-Abfrage oder eine eigene URI-Abgleichsregel erfordern, werden nicht verwendet.",
  "connector.bitwarden.session":
    "Der Sitzungsschlüssel bleibt im Arbeitsspeicher. Verbinde dich erneut, wenn der Tresor 8 Stunden nicht verwendet wurde oder volto beendet wurde. Trennen beendet den Zugriff von volto. Andere Bitwarden-Clients werden dadurch nicht gesperrt.",
  "connector.bitwarden.sessionKey": "Bitwarden-Sitzungsschlüssel",
  "connector.bitwarden.connect": "Bitwarden verbinden",
  "connector.bitwarden.disconnect": "Bitwarden trennen",
  "connector.bitwarden.disconnectAction": "Trennen",
  "connector.bitwarden.disconnectSummary":
    "volto vergisst den Sitzungsschlüssel. Agenten können keine Logins mehr aus Bitwarden ausfüllen. Andere Bitwarden-Clients bleiben entsperrt.",
  "connector.bitwarden.disconnectSummaryHost":
    "{host} entfernt den API-Schlüssel und das Master-Passwort. Agenten auf {host} können keine Logins mehr aus Bitwarden ausfüllen.",
  "connector.bitwarden.connected": "Verbunden",
  "connector.bitwarden.actionFailed": "volto konnte die Bitwarden-Verbindung nicht ändern.",
  "connector.bitwarden.descriptionHost": "Browser-Anmeldungen auf {host} aus Bitwarden oder Vaultwarden ausfüllen.",
  "connector.bitwarden.setupHost":
    "{host} meldet sich mit seiner eigenen Bitwarden CLI an. Erstelle in deinem Tresor einen Ordner mit dem Namen Shared with volto und lege nur die Anmeldungen hinein, die Agenten verwenden dürfen. Öffne im Web-Tresor Einstellungen › Sicherheit › Schlüssel und zeige deinen API-Schlüssel an. Gib unten die Serveradresse, client_id, client_secret und dein Master-Passwort ein.",
  "connector.bitwarden.scopeHost":
    "Alle Agenten auf {host} können passende Anmeldungen in Shared with volto verwenden. volto verwendet exakte HTTPS-Ursprünge. Einträge mit Master-Passwort-Abfrage oder eigener URI-Erkennungsregel werden nicht verwendet.",
  "connector.bitwarden.sessionHost":
    "{host} speichert den API-Schlüssel und das Master-Passwort verschlüsselt, damit es nach einem Neustart wieder entsperren kann. Dieser Computer speichert sie nicht. Trennen entfernt sie von {host}. {host} muss die Serveradresse über HTTPS erreichen.",
  "connector.bitwarden.serverUrl": "Serveradresse",
  "connector.bitwarden.folderTitle": "Geteilter Ordner",
  "connector.bitwarden.folderDescription":
    "Verschiebe Anmeldedaten in diesen Ordner in Bitwarden, damit Agenten sie verwenden können. Entferne sie, um die Nutzung zu beenden.",
  "connector.bitwarden.folderName": "Shared with volto",
  "connector.bitwarden.folderKind": "Ordner in Bitwarden",
  "connector.bitwarden.clientId": "API-Schlüssel client_id",
  "connector.bitwarden.clientSecret": "API-Schlüssel client_secret",
  "connector.bitwarden.masterPassword": "Master-Passwort",
  "connector.bitwarden.connecting": "Verbinden",
} as const satisfies PartialTranslation<typeof source>;
