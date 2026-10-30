import { type DynamicRecord, isDynamicRecord } from "@openbot/contracts/runtime-values";

/**
 * A reply that carries no usable JSON object. Callers translate it into their own user-facing
 * wording, because "the provider answered badly" means something different for a routing decision
 * than it does for a generated profile.
 */
export class StructuredOutputError extends Error {}

/**
 * Reads the one JSON object a text completion was asked for.
 *
 * No provider client in this repository can constrain a completion to a schema: every model reply
 * arrives as text through `generateTextWithoutTools`. So the object has to be found in prose. Three
 * shapes are accepted, in order of how often they arrive: the bare object, the object inside a
 * ```json fence, and the object after a sentence of preamble. A model that explains itself before
 * answering is not a failure the user should have to resolve.
 *
 * The return type is a record rather than `unknown`: only a JSON object is ever accepted, so a
 * bare array, string or number from the provider is a `StructuredOutputError` here and never
 * reaches the caller.
 */
export function extractJsonObject(response: string): DynamicRecord {
  const unfenced = response
    .trim()
    .replace(/^```(?:json)?\s*/u, "")
    .replace(/\s*```$/u, "")
    .trim();
  const candidate = unfenced.startsWith("{") ? unfenced : (balancedObject(unfenced) ?? unfenced);
  let parsed: unknown;
  try {
    parsed = JSON.parse(candidate);
  } catch {
    throw new StructuredOutputError("The provider returned no JSON object.");
  }
  if (!isDynamicRecord(parsed)) throw new StructuredOutputError("The provider returned no JSON object.");
  return parsed;
}

/**
 * Scans for the first balanced `{…}` span, skipping braces inside strings so that a reply like
 * `Assigning it. {"agentId":"a-1"}` is read and one like `{"text":"a } b"}` is not cut short.
 */
function balancedObject(value: string): string | null {
  const start = value.indexOf("{");
  if (start < 0) return null;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let index = start; index < value.length; index += 1) {
    const character = value[index];
    if (inString) {
      if (escaped) escaped = false;
      else if (character === "\\") escaped = true;
      else if (character === '"') inString = false;
      continue;
    }
    if (character === '"') inString = true;
    else if (character === "{") depth += 1;
    else if (character === "}") {
      depth -= 1;
      if (depth === 0) return value.slice(start, index + 1);
    }
  }
  return null;
}
