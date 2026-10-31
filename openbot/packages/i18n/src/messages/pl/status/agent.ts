import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/status/agent";

export const messages = {
  "status.agent.dispatchApproval":
    "Ta wysyłka kodowa zawiera ryzykowną operację. Sprawdź jej kartę i argumenty przed zatwierdzeniem.",
  "status.agent.dispatchStarted": "Rozpoczęto pracę programistyczną: {title}. Host: {host}. Uruchomienie: {runId}.",
  "status.agent.dispatchFinished":
    "Praca programistyczna zakończona: {title}. Host: {host}. Wynik: {status}. Sprawdź wynik, zanim zgłosisz sukces.",
  "status.agent.dispatchNoResult": "Wykonawca openswarm zakończył się bez pliku wyniku.",
  "status.agent.claudeWriteOutside":
    "Zapis {path} poza obszarem roboczym agenta, folderem współdzielonym i folderami tymczasowymi.",
  "status.agent.contextCleared": "Kontekst wyczyszczony. Tutaj zaczyna się nowy czat.",
  "status.agent.modelSwitchedForLimit": "Osiągnięto limit użycia. Przełączono na {model}.",
  "status.agent.modelReturnedAfterLimit": "Limit użycia już nie obowiązuje. Przełączono z powrotem na {model}.",
  "status.agent.pausedHourBudget":
    "Osiągnięto limit tokenów: {used} tokenów w ostatniej godzinie (limit {limit}). Nowa praca czeka, dopóki nie zostaniesz wznowiony.",
  "status.agent.pausedTurnBudget":
    "Zatrzymano po kroku: ta tura zużyła {used} tokenów (limit {limit}). Praca jest zachowana. Kontynuuj, aby ruszyć dalej.",
  "status.agent.pausedPlanLimit":
    "Limit planu zatrzymał tę turę. Praca jest zachowana i ruszy dalej, gdy limit się skończy.",
  "status.agent.modelSwitchedForError": "Błąd dostawcy. Przełączono na {model}.",
  "status.agent.modelReturnedAfterError": "Po błędzie dostawcy przełączono z powrotem na {model}.",
  "status.agent.marketplaceSuggested": "Zaproponowano aplikację z Marketplace: {app}.",
  "status.agent.channelMemberAdded": "Agent {agent} dodał {member} do kanału. Prośba od: {person}.",
  "status.agent.channelMemberRemoved": "Agent {agent} usunął {member} z kanału. Prośba od: {person}.",
  "status.agent.channelInstructionsChanged": "Agent {agent} zmienił instrukcje kanału. Prośba od: {person}.",
  "status.agent.channelRoutineCreated": "Agent {agent} utworzył rutynę {routine}. Prośba od: {person}.",
  "status.agent.channelRoutineUpdated": "Agent {agent} zmienił rutynę {routine}. Prośba od: {person}.",
  "status.agent.channelRoutineDeleted": "Agent {agent} usunął rutynę {routine}. Prośba od: {person}.",
  "status.agent.screenCaptureScreen": "Przechwyć główny ekran i wyślij obraz do modelu agenta.",
  "status.agent.screenCaptureChannel":
    "Obraz pojawi się na kanale {channel} i każdy członek tego serwera, który czyta kanał, może go zobaczyć.",
  "status.agent.screenCaptureDisplay": "Przechwyć wyświetlacz {name} i wyślij obraz do modelu agenta.",
  "status.agent.screenCaptureWindow": "Przechwyć okno {name} i wyślij obraz do modelu agenta.",
  "status.agent.screenCaptureRegion":
    "Przechwyć obszar ekranu w punkcie {x}, {y} o rozmiarze {width} × {height} punktów i wyślij obraz do modelu agenta.",
  "status.agent.screenCaptureProgress": "Prośba o przechwycenie ekranu…",
  "status.agent.screenCaptureReviewing": "Sprawdzanie przechwyconego ekranu…",
  "status.agent.starting": "Uruchamianie lokalnych CLI agentów…",
  "status.agent.memoriesSearching": "Przeszukiwanie wspomnień…",
  "status.agent.memoriesSearched": {
    one: "Przeszukano wspomnienia, znalezione: {count}",
    few: "Przeszukano wspomnienia, znalezione: {count}",
    many: "Przeszukano wspomnienia, znalezione: {count}",
    other: "Przeszukano wspomnienia, znalezione: {count}",
  },
  "status.agent.memoriesUpdating": "Aktualizowanie wspomnień…",
  "status.agent.skillUsing": "Używanie umiejętności {name}…",
  "status.agent.skillReading": "Czytanie umiejętności…",
  "status.agent.memoryChanges": "Wspomnienia: zapisane {saved}, zaktualizowane {updated}, zapomniane {forgotten}.",
  "status.agent.memoryChangesWithConflicts":
    "Wspomnienia: zapisane {saved}, zaktualizowane {updated}, zapomniane {forgotten}, niezmienione {notChanged}, bo inny wątek zmienił je wcześniej.",
} as const satisfies PartialTranslation<typeof source>;
