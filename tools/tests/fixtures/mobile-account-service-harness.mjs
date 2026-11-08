// Probe of the mobile account service choice (Refs #10).
//
// The probe runs one email sign-in of the real mobile app against a stubbed
// network and reports the account service the app asked for. It answers the
// question a self-hosted account service asks: does a build that is not a
// development build still use the account service named in its environment?
//
// The environment of the probe:
//   VOLTO_ROOT             the `volto/` tree of the repository
//   VOLTO_MOBILE_TEST_DEV  "1" for a development build, otherwise a release build
//   VOLTO_MOBILE_TEST_ENV  the environment variable the account service is
//                          named in; the other one stays unset
//   VOLTO_MOBILE_TEST_URL  the account service address, empty to name none
//
// The result is one JSON line on stdout: {"requests": [{"url": ..., "method": ...}]}.

import { register } from "node:module";
import { pathToFileURL } from "node:url";

const root = process.env.VOLTO_ROOT;
if (!root) throw new Error("VOLTO_ROOT must point at the volto tree.");

register(new URL("./mobile-account-service-loader.mjs", import.meta.url));

// A release build has no `__DEV__`. A self-compiled or TestFlight build is one.
globalThis.__DEV__ = process.env.VOLTO_MOBILE_TEST_DEV === "1";
globalThis.__voltoTestRequests = [];

const names = ["EXPO_PUBLIC_VOLTO_API_URL", "EXPO_PUBLIC_OPENBOT_API_URL"];
for (const name of names) delete process.env[name];
const configured = process.env.VOLTO_MOBILE_TEST_URL;
if (configured) process.env[process.env.VOLTO_MOBILE_TEST_ENV ?? names[0]] = configured;

const module = await import(
  pathToFileURL(`${root}/apps/mobile/src/features/auth/api/mobile-auth.ts`).href
);

await module.startMobileEmailSignIn("owner@example.com", "request-1");
console.log(JSON.stringify({ requests: globalThis.__voltoTestRequests }));
