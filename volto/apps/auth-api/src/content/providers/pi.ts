import type { ProviderPage } from "./provider-page";

// Every statement about Pi here is taken from pi.dev, Pi's GitHub repository and Earendil's
// release post. Each page used is in `sources`. Check them again, and move `checkedAt`,
// whenever this file changes.

const REPO = "https://github.com/earendil-works/pi";

export const PI_PROVIDER: ProviderPage = {
  provider: "pi",
  name: "Pi",
  answer:
    "volto runs Pi as one agent of a team, on macOS, Windows or Linux, with the providers that you sign in to in Pi. You get shared channels with agents on other providers, and an app on your phone.",
  connect:
    "Choose Pi, and volto downloads and pins its own copy of Pi. To sign in, run `/login` in Pi on the computer that runs volto, connect a subscription or an API key, and close Pi. volto shows Pi as connected soon after.",
  adds: [
    {
      icon: "cpu",
      title: "The models you sign in to",
      text: "A Pi agent uses the models that Pi lists for the providers that you connected with /login.",
    },
    {
      icon: "contact",
      title: "Steer it while it works",
      text: "Send a message while a Pi agent works, and the message changes the course of the current turn.",
    },
  ],
  vendor: "Earendil",
  officialApps: [
    {
      name: "Pi in the terminal",
      platforms: "macOS, Linux, Windows. Native Windows uses Git Bash",
      note: "The terminal agent.",
    },
    {
      name: "Pi SDK",
      platforms: "TypeScript",
      note: "Pi in your own program.",
    },
  ],
  comparisons: ["best-ai-agent-apps"],
  faq: [
    {
      question: "Is there a GUI for Pi?",
      answer:
        "Pi has no official desktop or web app: it works in the terminal. volto is a desktop app that runs Pi as one agent of a team, with agents on other providers and an app on your phone.",
    },
    {
      question: "How do I sign in to Pi in volto?",
      answer:
        "Run /login in Pi on the computer that runs volto, and connect a subscription or an API key. Then close Pi, and volto shows Pi as connected.",
    },
    {
      question: "Is Pi free?",
      answer:
        "Pi is open source under the MIT license, and volto downloads it for you. You pay the providers that you sign in to.",
    },
    {
      question: "Does Pi run Inception's Mercury models?",
      answer:
        "Yes. In volto, Inception runs in the copy of Pi that volto downloads, with an Inception API key that volto keeps encrypted. Pi's own settings do not change.",
    },
  ],
  sources: [
    { label: "Pi", url: "https://pi.dev" },
    { label: "Pi documentation", url: "https://pi.dev/docs/latest" },
    { label: "Pi on GitHub", url: REPO },
    { label: "Pi on Windows", url: `${REPO}/blob/main/packages/coding-agent/docs/windows.md` },
    { label: "Earendil: Pi 1.0", url: "https://earendil.com/posts/pi-1-0/" },
  ],
  checkedAt: "2026-10-24",
};
