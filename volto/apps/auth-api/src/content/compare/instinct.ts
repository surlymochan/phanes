import { VOLTO_LINKS } from "../../lib/landing-links";
import { type Comparison, VOLTO_TEXT } from "./comparison";

// Every statement about Instinct here is taken from instinct.com, its terms of service and
// its privacy policy, or from the TechCrunch report on its private access; each page used
// is in `sources`. Instinct names no model, no price and no list of countries, so the text
// says so. Check them again, and move `checkedAt`, whenever this file changes. Instinct is
// a service of Spear Street Technology, in San Francisco.

export const INSTINCT_COMPARISON: Comparison = {
  rival: { name: "Instinct", mark: "instinct" },
  answer:
    "Choose Volto if you want a team of AI agents on your own computer, with your data in a database that you hold, the AI plans you already pay for, and agents that show you a draft before they send a message for you: the app is free. Choose Instinct if you have an invite, and you want one assistant that you text or call, with no app to set up.",
  chooseVolto: [
    "Your mail, files and chats must stay on your own computer, not on a service's servers in the US.",
    "You want to approve a message or a payment before an agent makes it.",
    "You want ChatGPT, Claude, Gemini, Grok, Cursor, Cline, Muse Code and Pi agents in one team, or your own model.",
    "You want a free app that you can download today, with source code that you can read.",
  ],
  rivalPlans:
    "Instinct names no model. It is in private access, by invite, and it publishes no price; its terms say that it can charge fees and change its prices at any time.",
  chooseRival: [
    "You have an invite, and you want an assistant with no app: you text it or call it.",
    "You want it to use a phone and a computer for you, as a person does.",
    "You want no computer of yours to stay on.",
  ],
  rows: [
    {
      icon: "cpu",
      topic: "Models and plans",
      volto: VOLTO_TEXT.models,
      rival:
        "Instinct names no model. Its privacy policy says that it shares data with third-party AI model providers.",
      better: "volto",
    },
    {
      icon: "laptop",
      topic: "Where agents work",
      volto: "On the computer that runs Volto.",
      rival:
        "On Instinct's service, which its terms say it operates in the United States. Instinct says that it can use a phone and a computer as people do.",
      better: "volto",
    },
    {
      icon: "cloud",
      topic: "When your computer is off",
      volto: VOLTO_TEXT.offline,
      rival: "Instinct works on its own service, so no computer of yours must stay on.",
    },
    {
      icon: "phone",
      topic: "How you talk to it",
      volto:
        "Apps for iPhone and Android connect from anywhere, over an encrypted connection between your devices, and no cloud stores your chats or files. Agents can also answer in Slack, Discord and Telegram.",
      rival: "By text message, WhatsApp or a phone call. Instinct has no app of its own to talk to it.",
    },
    {
      icon: "refresh",
      topic: "Backup models",
      volto: VOLTO_TEXT.backupModels,
      rival: "Instinct names no model and states no backup model. You cannot choose a model.",
      better: "volto",
    },
    {
      icon: "key",
      topic: "Logins and payments",
      volto: VOLTO_TEXT.logins,
      rival:
        "You can give Instinct your passwords for other accounts and your payment details, so that it signs in and books for you. Its privacy policy names no encryption and says that it cannot guarantee perfect security.",
      better: "volto",
    },
    {
      icon: "lock",
      topic: "Your data",
      volto:
        "Workspaces, chats and files stay in a database on your computer. The provider you choose gets the requests you send. Product analytics, with no chat content, are on by default and linked to your account when you sign in; you can turn them off.",
      rival:
        "Instinct can use your data to train its AI models, unless you opt out. A disconnected app does not delete the data that Instinct collected from it, and when you delete your account, Instinct does not have to delete your materials.",
      better: "volto",
    },
    {
      icon: "check",
      topic: "Who approves an action",
      volto:
        "You do. An agent shows a draft and waits for your approval before it sends a message for you, unless you or a routine that you set up tells it to send. It asks you to confirm the item, price and total before a payment.",
      rival:
        "Under its terms, you appoint Instinct as your agent to make agreements and transactions for you, and they bind you as if you had made them. One tester said that it sent an email for her without checking first.",
      better: "volto",
    },
    {
      icon: "users",
      topic: "Teams",
      volto: VOLTO_TEXT.teams,
      rival: "One personal assistant for each person.",
      better: "volto",
    },
    {
      icon: "tag",
      topic: "Price and account",
      volto:
        "Free for noncommercial use; commercial use needs a license. Your agents use the plans you already pay for.",
      rival:
        "No published price. The terms say that you pay the fees that apply, and that Instinct can change its prices at any time. You sign in with a phone number.",
      better: "volto",
    },
    {
      icon: "globe",
      topic: "Where you can use it",
      volto: VOLTO_TEXT.regions,
      rival:
        "By invite only, in private access. Instinct operates its service in the United States and names no countries. Only for people 18 and over.",
      better: "volto",
    },
    {
      icon: "devices",
      topic: "Apps",
      volto: VOLTO_TEXT.apps,
      rival: "No app to install: you text or call Instinct, and you manage your account on the web.",
    },
    {
      icon: "code",
      topic: "Source code",
      volto: "On GitHub, under the PolyForm Noncommercial License 1.0.0.",
      rival: "A closed service that runs only on Instinct's servers.",
      better: "volto",
    },
  ],
  intro:
    "Instinct is a personal assistant that you text or call. It connects to your email, messaging apps and calendar, and to the screen, audio and location of your devices, and it acts for you: it books tables, cleans up an inbox and shops. It is in private access, by invite. Volto also gives you agents that do work for you. The difference is where they run, who holds your data, and who approves what they do. Instinct runs on its own service in the US, can train its models on your data, and its terms make the transactions it enters bind you. Volto runs a team of agents on your own computer, keeps your data there, and asks you before it sends a message or pays.",
  sections: [
    {
      title: "Models and the plans you pay for",
      volto: VOLTO_TEXT.plansSection,
      rival:
        "Instinct does not say which models it uses, and you cannot choose one. Its privacy policy names third-party AI model providers among the parties that get your data. Instinct publishes no price: it is in private access, and its terms say that you pay the fees that apply and that it can change its prices at any time.",
      better: "volto",
    },
    {
      title: "Where the work happens",
      volto: VOLTO_TEXT.workSection,
      rival:
        "Instinct works on its own service, which its terms say it operates in the United States, so no computer of yours must stay on. It connects to your email, messaging apps and calendar, and to the screen, audio and location of your devices. Instinct says that it can use a phone and a computer in the same way that people do.",
      better: "volto",
    },
    {
      title: "Who approves what the agent does",
      volto:
        "Volto agents show a draft before they send a message for you. They send it after you approve it, or when you or a routine that you set up tells them to send. Before a payment, an agent reads back the item, price and total and asks you to confirm. A new agent has Full access: it does not ask before it runs a command. Set an agent to Ask first, and that agent asks you first; you answer in the chat or from a notification on your phone.",
      rival:
        "Instinct's terms authorize it to take the actions that it decides answer your request, and appoint it as your agent to enter into agreements, commitments or transactions for you. Those bind you as if you had entered into them yourself. Testers told TechCrunch that Instinct sent an email for one of them without checking first, and found a sign-up code in another's inbox to complete a booking.",
      better: "volto",
    },
    {
      title: "Phones and remote access",
      volto: VOLTO_TEXT.phonesSection,
      rival:
        "You talk to Instinct by text message or WhatsApp, or you call it; there is no Instinct app to install. You sign in on the web with your phone number and a code, and you manage connected apps and your data there.",
    },
    {
      title: "How agents work as a team",
      volto: VOLTO_TEXT.teamSection,
      rival:
        "Instinct is one personal assistant for each person. It describes no teams of agents and no shared workspace.",
      better: "volto",
    },
    {
      title: "Data and privacy",
      volto:
        "Volto keeps workspaces, conversations, attachments and browser data in a SQLite database on the computer that runs it. Volto keeps no other copy. On a hosted Volto server, the database is on that server in the EU. An account holds your profile, team memberships, invitations, sign-in sessions, the settings that let devices find each other, and any agent templates that you publish. Product analytics, which never include chat content, are on by default and linked to your account when you sign in; you can turn them off in Settings.",
      rival:
        "Instinct's privacy policy says that it can use your information to train the AI models behind its products. You can opt out, but models already trained on your data stay, and data flagged for safety review can still be used. It does not use data from Google Workspace to train models. Disconnecting an app does not delete the data that Instinct collected from it; you delete that data on the web. When you delete your account, Instinct may delete your materials, but it does not have to. A tester told TechCrunch that Instinct still summarized her inbox after she disconnected it, and that it said it kept the emails in plain text.",
      better: "volto",
    },
  ],
  faq: [
    {
      question: "Is Volto an alternative to Instinct?",
      answer:
        "Yes, if you want your agents and your data on your own computer. Both give you AI agents that do work for you. Volto runs a team of agents on your computer, with the AI plans you choose, and asks you before it sends a message or pays. Instinct is one assistant that you text or call, on its own service in the US.",
    },
    {
      question: "Is Instinct private?",
      answer:
        "Instinct connects to your email, messages and calendar, and to the screen, audio and location of your devices. Its privacy policy says that it can train its AI models on your information unless you opt out, that it shares data with third-party AI model providers, and that disconnecting an app does not delete the data it collected. Volto keeps your workspaces, chats and files in a database on your own computer.",
    },
    {
      question: "Can Instinct act for me without asking?",
      answer:
        "Its terms authorize it to take actions for you and to enter into agreements and transactions that bind you as if you had made them. A tester said that it sent an email for her without checking first. Volto agents show a draft and wait for your approval before they send a message, unless you tell them to send, and confirm before a payment.",
    },
    {
      question: "How much does Instinct cost?",
      answer:
        "Instinct publishes no price. It is in private access, by invite, and its terms say that you pay the fees that apply and that it can change its prices at any time. Volto is free for noncommercial use, and it uses the AI plans you already pay for.",
    },
    {
      question: "Does Instinct run on my computer?",
      answer:
        "No. Instinct runs on its own service, which it operates in the United States, and you reach it by text message, WhatsApp or a phone call. Volto agents work on your computer, or on a hosted Volto server in the EU that you choose.",
    },
    {
      question: "Do agents keep working when my laptop is closed?",
      answer:
        "Yes, with both. Instinct works on its own service, so no computer of yours must stay on. Volto agents work on the computer or server that runs Volto: keep that computer on, or use a hosted Volto server, and connect from your laptop or phone.",
    },
    {
      question: "Is Instinct open source?",
      answer:
        "No. Instinct is a closed service. Volto's source code is on GitHub under the PolyForm Noncommercial License 1.0.0.",
    },
  ],
  sources: [
    { label: "Instinct", url: "https://instinct.com/" },
    { label: "Instinct terms of service", url: "https://instinct.com/terms" },
    { label: "Instinct privacy policy", url: "https://instinct.com/privacy-policy" },
    { label: "Instinct sign-in", url: "https://app.instinct.com/" },
    {
      label: "TechCrunch: Instinct's AI assistant raises privacy and security concerns",
      url: "https://techcrunch.com/2026/08/24/instincts-powerful-ai-assistant-is-raising-privacy-and-security-concerns/",
    },
    { label: "Volto privacy notes", url: VOLTO_LINKS.privacy },
    { label: "Volto source code", url: VOLTO_LINKS.repository },
  ],
  checkedAt: "2026-10-15",
};
