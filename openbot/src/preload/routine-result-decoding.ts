// What main answers for routine results: the places to pick, a routine's destination, and its deliveries.

import {
  isNullableRoutineResultDestination,
  isRoutineResultConnectionOptions,
  isRoutineResultDeliveries,
} from "@openbot/contracts/ipc";
import { guardedDecoder } from "@openbot/contracts/ipc-decoding";

export const decodeRoutineResultOptions = guardedDecoder(
  isRoutineResultConnectionOptions,
  "routine result options response",
);
export const decodeRoutineResultDestination = guardedDecoder(
  isNullableRoutineResultDestination,
  "routine result destination response",
);
export const decodeRoutineResultDeliveries = guardedDecoder(
  isRoutineResultDeliveries,
  "routine result deliveries response",
);
