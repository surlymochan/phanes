import { type AppLanguage, DEFAULT_APP_LANGUAGE, isAppLanguage } from "@openbot/contracts/app-language";
import { loadLocale, resolveLocale } from "@openbot/i18n";
import { createSignal, onCleanup } from "solid-js";

const STORAGE_KEY = "openbot.web.language";

type LanguageStorage = Pick<Storage, "getItem" | "setItem">;

/** Reading `window.localStorage` throws when the browser blocks storage, so it is read inside the guard. */
function read(storage: LanguageStorage | undefined): AppLanguage {
  try {
    const value = (storage ?? window.localStorage).getItem(STORAGE_KEY);
    return isAppLanguage(value) ? value : DEFAULT_APP_LANGUAGE;
  } catch {
    return DEFAULT_APP_LANGUAGE;
  }
}

/**
 * Loads the catalogs of the saved language, so the first screen of the web client is in it. The
 * public web build loads every language other than English on first use.
 */
export function loadSavedLanguage(): Promise<void> {
  return loadLocale(resolveLocale(read(undefined), navigator.language));
}

/**
 * The web client's interface language. The web client has no main process, so this browser keeps it,
 * also before sign-in. Other tabs follow through `storage`.
 */
export function createWebLanguagePreference(storage?: LanguageStorage) {
  const [language, setStoredLanguage] = createSignal<AppLanguage>(read(storage));
  function listen(event: StorageEvent): void {
    if (event.key === STORAGE_KEY) setStoredLanguage(read(storage));
  }
  window.addEventListener("storage", listen);
  onCleanup(() => window.removeEventListener("storage", listen));

  return {
    language,
    setLanguage(value: AppLanguage): void {
      setStoredLanguage(value);
      try {
        (storage ?? window.localStorage).setItem(STORAGE_KEY, value);
      } catch {
        // The choice holds for this tab when browser storage is unavailable.
      }
    },
  };
}
