// Expo SDK 58 changed the default fingerprint preset to `balanced`, which hashes a native package in
// node_modules by its name and version only. `strict` hashes its native files, as SDK 57 did, so a
// patch in the root `patches/` folder that changes native code also changes `runtimeVersion`.
/** @type {import('@expo/fingerprint').Config} */
module.exports = { preset: "strict" };
