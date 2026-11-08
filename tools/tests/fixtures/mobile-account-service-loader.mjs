// Module customization hooks for the mobile account service probe (Refs #10).
//
// The mobile app talks to an account service. A self-hosted account service is
// named in the build environment, so the sign-in request must go to that
// address. The probe loads the real `mobile-auth.ts` of the `volto/` tree and
// replaces only the boundaries that need a device or a package manager:
//
//   * `@volto/contracts/*` are the real sources of the tree. They hold the
//     account service URL rules, so the probe must not re-implement them.
//   * `expo-*`, `zod` and the app-local helpers have no runnable form outside
//     an installed app, so they are stubbed at the module boundary.
//   * `logged-fetch` records the request URL of the sign-in call, which is the
//     observable result the probe reports.
//
// The relative imports of the tree are written without a file extension, so the
// resolver adds the `.ts` suffix Node needs.

const ROOT = new URL(`file://${process.env.VOLTO_ROOT ?? ""}/`);

/** Real sources of the tree, addressed the way the mobile app imports them. */
const SOURCE_MAP = {
  "@volto/contracts/avatar-images": "packages/contracts/src/avatar-images.ts",
  "@volto/contracts/input-limits": "packages/contracts/src/input-limits.ts",
  "@volto/contracts/invite-links": "packages/contracts/src/invite-links.ts",
  "@volto/contracts/mobile-connect": "packages/contracts/src/mobile-connect.ts",
  "@volto/contracts/runtime-values": "packages/contracts/src/runtime-values.ts",
  "@volto/contracts/validation": "packages/contracts/src/validation.ts",
};

/** Boundaries the probe replaces. Each stub keeps the shape the app expects. */
const STUBS = {
  zod: `
const schema = () => {
  const self = { parse: (value) => value, safeParse: (value) => ({ success: true, data: value }) };
  for (const name of ["uuid", "finite", "optional", "min", "max", "email", "regex", "int", "nonempty", "trim", "default", "catch", "nullable", "nullish", "refine", "transform", "describe", "strict", "passthrough", "array", "or", "and"]) {
    self[name] = () => self;
  }
  return self;
};
export const z = {
  object: schema, array: schema, string: schema, number: schema, boolean: schema, unknown: schema, enum: schema,
};
`,
  "expo-crypto": `export const randomUUID = () => "00000000-0000-4000-8000-000000000000";`,
  "expo-device": `export const deviceName = "Test Phone"; export const modelName = "Test Phone";`,
  "expo-secure-store": `
export const getItemAsync = async () => null;
export const setItemAsync = async () => undefined;
export const deleteItemAsync = async () => undefined;
export const WHEN_UNLOCKED_THIS_DEVICE_ONLY = "whenUnlockedThisDeviceOnly";
`,
  "@/shared/lib/platform": `export const isIOS = true; export const isAndroid = false;`,
  "@/shared/lib/text": `export const currentText = () => ({ t: (key) => key });`,
  "../../../shared/lib/locked-keychain": `
export const getItemAsync = async () => null;
export const setItemAsync = async () => undefined;
export const deleteItemAsync = async () => undefined;
`,
  "../../support/model/logged-fetch": `
export async function fetch(url, init) {
  globalThis.__voltoTestRequests.push({ url: String(url), method: init?.method ?? "GET" });
  return {
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: async () => ({ challengeId: "challenge-1", resendAt: 0 }),
  };
}
`,
};

export async function resolve(specifier, context, nextResolve) {
  const stub = STUBS[specifier];
  if (stub) return { url: `data:text/javascript,${encodeURIComponent(stub)}`, shortCircuit: true };
  const source = SOURCE_MAP[specifier];
  if (source) return { url: new URL(source, ROOT).href, shortCircuit: true };
  if (specifier.startsWith("./") || specifier.startsWith("../")) {
    const parent = context.parentURL ?? ROOT.href;
    const base = new URL(specifier, parent);
    if (!/\.[cm]?[jt]sx?$/.test(base.pathname)) {
      const { existsSync } = await import("node:fs");
      for (const candidate of [".ts", "/index.ts"]) {
        const withExtension = new URL(`${base.href}${candidate}`);
        if (existsSync(withExtension)) return { url: withExtension.href, shortCircuit: true };
      }
    }
  }
  return nextResolve(specifier, context);
}
