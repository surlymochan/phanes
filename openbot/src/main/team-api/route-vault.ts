import type { VaultState } from "@openbot/contracts/ipc";
import {
  VAULT_CAPABILITY,
  VAULT_ROUTES,
  VAULT_WIRE_ITEM_LIMIT,
  VAULT_WIRE_NAME_LIMIT,
} from "@openbot/contracts/team-protocol/vault-v1";
import { isFixedSourceText, sourceText } from "@openbot/i18n/source";
import { runCauseEffect } from "../../backend/effect-boundary";
import { decodeVaultSaveInput, decodeVaultSecretRequest } from "../vault-inputs";
import type { TeamApiVault } from "./dependencies";
import { HttpError } from "./http-error";
import type { RouteOutcome, TeamApiRequestContext } from "./request-context";
import { readJson, requireAdmin, stringField } from "./request-helpers";

const ROUTES: ReadonlySet<string> = new Set(Object.values(VAULT_ROUTES));

/** The wire list of `vault-v1`, cut to the codec's bounds, so one long agent name cannot fail the list. */
function wireState(state: VaultState): VaultState {
  return {
    status: state.status,
    items: state.items
      .slice(0, VAULT_WIRE_ITEM_LIMIT)
      .map((item) =>
        item.addedBy === undefined ? item : { ...item, addedBy: item.addedBy.slice(0, VAULT_WIRE_NAME_LIMIT) },
      ),
  };
}

/**
 * The Vault of this host, which an owner or administrator manages from their own computer or browser.
 * Frozen by `vault-v1`.
 *
 * A list never carries a password, a card number or a security code; `reveal` answers one for the
 * user who asked to see or copy it. `requireAdmin` runs on every route.
 */
export async function routeVault(
  context: TeamApiRequestContext,
  vault: TeamApiVault | undefined,
): Promise<RouteOutcome> {
  const { method, url, capabilities, member, request, json } = context;
  if (method !== "POST" || !ROUTES.has(url.pathname)) return "unmatched";
  if (!vault || !capabilities.has(VAULT_CAPABILITY))
    throw new HttpError(400, sourceText("error.team.vaultUnsupported"));
  requireAdmin(member);
  // `readJson` has already run the body through the `vault-v1` codec.
  const body = await readJson(request);
  try {
    switch (url.pathname) {
      case VAULT_ROUTES.save:
        return json(200, wireState(await runCauseEffect(vault.save(parse(() => decodeVaultSaveInput(body))))));
      case VAULT_ROUTES.delete:
        return json(200, wireState(await runCauseEffect(vault.delete(stringField(body, "id", false, 64)))));
      case VAULT_ROUTES.reveal:
        return json(200, { value: vault.reveal(parse(() => decodeVaultSecretRequest(body))) });
      case VAULT_ROUTES.reset:
        // Only the way out of a file that the host cannot read: a readable Vault is never deleted here.
        if (vault.state().status !== "unreadable")
          throw new HttpError(409, sourceText("error.team.vaultResetReadable"));
        return json(200, wireState(await runCauseEffect(vault.reset())));
      default:
        return json(200, wireState(vault.state()));
    }
  } catch (error) {
    // A value typed wrong, a missing item or a Vault that cannot be read is catalog text for the
    // admin. Any other failure stays private: the server answers it with its generic error.
    if (error instanceof Error && !(error instanceof HttpError) && isFixedSourceText(error.message))
      throw new HttpError(400, error.message);
    throw error;
  }
}

function parse<T>(read: () => T): T {
  try {
    return read();
  } catch (error) {
    throw new HttpError(400, error instanceof Error ? error.message : "Invalid Vault request.");
  }
}
