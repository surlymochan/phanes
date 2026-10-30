import { isString } from "@openbot/contracts/runtime-values";
import { createFileRoute } from "@tanstack/solid-router";
import { Effect } from "effect";
import { runApiResponse } from "../../../server/effect-runtime";
import { readJsonObject } from "../../../server/json-body";
import {
  apiError,
  authErrorResponse,
  json,
  requestAuthService,
  requireServerClaimsAdmin,
} from "../../../server/request-auth";

/** An operator makes a one-time claim that signs a new server in for the user of the email. */
export const Route = createFileRoute("/v2/admin/server-claims")({
  server: {
    handlers: {
      POST: ({ request }) =>
        runApiResponse(
          Effect.gen(function* () {
            if (!(yield* requireServerClaimsAdmin(request)))
              return apiError(401, "unauthorized", "Admin access is required.");
            const body = yield* readJsonObject(request);
            if (!isString(body.email)) return apiError(400, "invalid_email", "Enter a valid email address.");
            return json(yield* requestAuthService().createServerClaim(body.email));
          }),
          authErrorResponse,
        ),
    },
  },
});
