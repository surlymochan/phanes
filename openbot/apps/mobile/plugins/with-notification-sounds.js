const { copyFileSync, mkdirSync } = require("node:fs");
const { basename, join, parse, resolve } = require("node:path");
const { withDangerousMod, withXcodeProject } = require("expo/config-plugins");
const { setNotificationSounds } = require("expo-notifications/plugin/build/withNotificationsIOS");

// Adds the notification sounds to the app. The `expo-notifications` plugin would also copy its
// `sounds` into the Android resources, which reject the `-` in the names. The names stay on iOS,
// because the account service sends them to released iPhone builds (`NOTIFICATION_SOUND_FILES`), so
// Android gets copies with `_` in place of `-`. `OpenBotMessagingService.kt` and the notification
// channels (`android-notification-channels.ts`) use those names.

/** @type {import('expo/config-plugins').ConfigPlugin<{ sounds: string[] }>} */
module.exports = (config, { sounds }) => {
  config = withXcodeProject(config, (config) => {
    config.modResults = setNotificationSounds(config.modRequest.projectRoot, {
      sounds,
      project: config.modResults,
      projectName: config.modRequest.projectName,
    });
    return config;
  });
  return withDangerousMod(config, [
    "android",
    (config) => {
      const { projectRoot, platformProjectRoot } = config.modRequest;
      const raw = join(platformProjectRoot, "app", "src", "main", "res", "raw");
      mkdirSync(raw, { recursive: true });
      for (const sound of sounds) {
        const { name, ext } = parse(basename(sound));
        copyFileSync(resolve(projectRoot, sound), join(raw, `${name.replaceAll("-", "_")}${ext}`));
      }
      return config;
    },
  ]);
};
