import type { RoutineResultConnectionOption, RoutineResultDestination } from "@openbot/contracts/ipc";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue, Text } from "@openbot/ui";
import { For } from "solid-js";
import { useText } from "../../text";

/** A place to send to. The connection's name and platform come from the host when it saves. */
export interface RoutineResultChoice {
  connectionId: string;
  placeId: string;
  placeName: string;
}

interface RoutineResultDestinationFieldProps {
  /** The enabled connections and their places. */
  options: RoutineResultConnectionOption[];
  /** The saved place. It stays in the list when its connection is off now. */
  saved: RoutineResultDestination | null;
  /** The place in the draft, which is the saved one until the user picks another. */
  value: RoutineResultChoice | null;
  onChange: (choice: RoutineResultChoice | null) => void;
}

/** Kobalte shows an empty key as no selection. A place key always holds a `/`, so it is never this one. */
const NONE = "none";

interface PlaceEntry {
  key: string;
  choice: RoutineResultChoice;
  connectionName: string;
  /** False for a saved place that its connection does not offer now. */
  available: boolean;
}

/**
 * Kobalte puts the key in a `[data-key="…"]` selector without escaping it, so the key must not hold a
 * quote or a backslash. `encodeURIComponent` encodes both, and the `/` between the parts.
 */
function placeKey(choice: { connectionId: string; placeId: string }): string {
  return `${encodeURIComponent(choice.connectionId)}/${encodeURIComponent(choice.placeId)}`;
}

/** Where an agent routine sends each answer, as well as to its chat. */
export function RoutineResultDestinationField(props: RoutineResultDestinationFieldProps) {
  const { t } = useText();
  const entries = (): PlaceEntry[] => {
    const offered = props.options.flatMap((connection) =>
      connection.places.map((place) => ({
        key: placeKey({ connectionId: connection.connectionId, placeId: place.id }),
        choice: { connectionId: connection.connectionId, placeId: place.id, placeName: place.name },
        connectionName: connection.connectionName,
        available: true,
      })),
    );
    const saved = props.saved;
    if (!saved || offered.some((entry) => entry.key === placeKey(saved))) return offered;
    return [
      ...offered,
      {
        key: placeKey(saved),
        choice: { connectionId: saved.connectionId, placeId: saved.placeId, placeName: saved.placeName },
        connectionName: saved.connectionName,
        available: false,
      },
    ];
  };
  const entry = (key: string) => entries().find((candidate) => candidate.key === key);
  const label = (key: string) => {
    const found = entry(key);
    if (!found) return t("routine.settings.resultDestination.none");
    const place = t("routine.settings.resultDestination.place", {
      connection: found.connectionName,
      place: found.choice.placeName,
    });
    return found.available ? place : t("routine.settings.resultDestination.unavailable", { place });
  };
  const failed = () => props.options.filter((connection) => connection.placesFailed);
  const selected = () => (props.value ? placeKey(props.value) : NONE);

  return (
    <div class="settings-field">
      <span id="agent-routine-result-destination-label">{t("routine.settings.resultDestination")}</span>
      <Select<string>
        options={[NONE, ...entries().map((candidate) => candidate.key)]}
        value={selected()}
        onChange={(key) => {
          // Kobalte selects the same key again when the options load; only a new place is a change.
          if ((key ?? NONE) === selected()) return;
          props.onChange(key ? (entry(key)?.choice ?? null) : null);
        }}
        itemComponent={(item) => <SelectItem item={item.item}>{label(item.item.rawValue)}</SelectItem>}
      >
        <SelectTrigger aria-labelledby="agent-routine-result-destination-label">
          <SelectValue<string>>{(state) => label(state.selectedOption())}</SelectValue>
        </SelectTrigger>
        <SelectContent />
      </Select>
      <For each={failed()}>
        {(connection) => (
          <Text variant="caption" tone="muted">
            {t("routine.settings.resultDestination.placesFailed", { connection: connection.connectionName })}
          </Text>
        )}
      </For>
    </div>
  );
}
