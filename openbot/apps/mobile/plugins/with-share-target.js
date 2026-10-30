const fs = require("node:fs");
const path = require("node:path");
const { withDangerousMod, withPlugins } = require("expo/config-plugins");
const { mergeContents } = require("@expo/config-plugins/build/utils/generateCode");

// Applies expo-sharing, which adds the iOS share extension and the Android share intents, with the
// options from app.json. Then it changes two files that expo-sharing copies into the extension:
// - Info.plist sets the display name to the target name, `expo-sharing-extension`. The iOS share
//   sheet shows that name under the OpenBot icon, so the extension gets the app's name.
// - ShareIntoViewController.swift reads an item as a web link before it looks for an image. An image
//   from a web page also carries its address, so Safari's "Share" on an image sent only the address.

const TARGET_NAME = "expo-sharing-extension";

// The image goes first, as Messages sends it. The address stays the fallback when the image does not load.
const IMAGE_FIRST = `    if provider.hasItemConformingToTypeIdentifier(UTType.image.identifier),
      let image = await handleFile(provider, type: .image, utType: .image) {
      return image
    }`;

/** @param {string} contents */
function preferImages(contents) {
  return mergeContents({
    src: contents,
    newSrc: IMAGE_FIRST,
    tag: "openbot-share-image-first",
    anchor: /private func parseProvider\(_ provider: NSItemProvider\) async -> SharePayload\? \{/u,
    offset: 1,
    comment: "//",
  }).contents;
}

/** @param {string} contents @param {string} displayName */
function setDisplayName(contents, displayName) {
  const pattern = /(<key>CFBundleDisplayName<\/key>\s*<string>)[^<]*(<\/string>)/u;
  if (!pattern.test(contents)) throw new Error(`${TARGET_NAME}/Info.plist has no CFBundleDisplayName.`);
  return contents.replace(pattern, (_match, start, end) => `${start}${displayName}${end}`);
}

/** @param {string} value */
function xmlText(value) {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

/** @type {import('expo/config-plugins').ConfigPlugin<{ ios?: { enabled?: boolean } } | undefined>} */
const withShareTarget = (config, props) => {
  if (!props?.ios?.enabled) return withPlugins(config, [["expo-sharing", props]]);
  // A mod runs before the mods registered earlier. This one is registered before expo-sharing, so it
  // runs after expo-sharing has copied the extension files.
  const withName = withDangerousMod(config, [
    "ios",
    (mod) => {
      const directory = path.join(mod.modRequest.platformProjectRoot, TARGET_NAME);
      const plist = path.join(directory, "Info.plist");
      fs.writeFileSync(plist, setDisplayName(fs.readFileSync(plist, "utf8"), xmlText(mod.name)));
      const controller = path.join(directory, "ShareIntoViewController.swift");
      fs.writeFileSync(controller, preferImages(fs.readFileSync(controller, "utf8")));
      return mod;
    },
  ]);
  return withPlugins(withName, [["expo-sharing", props]]);
};

module.exports = withShareTarget;
