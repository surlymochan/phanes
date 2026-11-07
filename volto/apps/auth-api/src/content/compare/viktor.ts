import { VOLTO_LINKS } from "../../lib/landing-links";
import { type Comparison, VOLTO_LANGUAGE_COUNT, VOLTO_TEXT } from "./comparison";

// Every statement about Viktor here is taken from viktor.com: its home page, pricing page,
// security page, changelog, documentation, privacy policy and general terms (both last updated
// 2026-07-31); each page used is in `sources`. Viktor is a service of Zeta AI, Inc., a
// Delaware corporation. Check them again, and move `checkedAt`, whenever this file changes.

export const VIKTOR_COMPARISON: Comparison = {
  rival: { name: "Viktor", mark: "viktor" },
  answer:
    "Choose Volto if you want a team of AI agents on your own computer, with your data in a database that you hold and the AI plans you already pay for: the app is free, and there are no credits to buy. Choose Viktor if you want one AI coworker in Slack or Microsoft Teams that runs in the cloud, connects to thousands of business apps, and comes with a SOC 2 report.",
  chooseVolto: [
    "Your chats, files and company data must stay on your own computer, not on a service's servers in the US.",
    "You already pay for ChatGPT, Claude, Gemini, Grok, Cursor, Cline or Muse Code, and you do not want to pay for each task again in credits.",
    "You want a team of agents, each with its own job and model, not one assistant for the whole company.",
    "You want a free app that you can download today, with source code that you can read.",
  ],
  rivalPlans:
    "You choose from the models that Viktor offers, from OpenAI, Anthropic, Google and xAI, or connect your own OpenRouter key. An admin sets the default model for the workspace. You pay in credits: up to $100 in free credits to start, then a Team plan from $50 a month for 20,000 credits.",
  chooseRival: [
    "Your team works in Slack or Microsoft Teams, and you want one AI coworker that everyone mentions there.",
    "You need a ready connection to many business apps, such as Salesforce, HubSpot and Google Ads.",
    "Your company needs a vendor with a SOC 2 Type 1 report, SAML single sign-on and a DPA.",
    "You want no computer of yours to stay on.",
  ],
  rows: [
    {
      icon: "cpu",
      topic: "Models and plans",
      volto: VOLTO_TEXT.models,
      rival:
        "Models from OpenAI, Anthropic, Google and xAI, or your own OpenRouter key. You cannot use a ChatGPT or Claude plan that you already pay for, and Viktor bills each task in credits.",
      better: "volto",
    },
    {
      icon: "tag",
      topic: "Price",
      volto:
        "Free for noncommercial use; commercial use needs a license. Your agents use the plans you already pay for, so there is no charge for each task.",
      rival:
        "Up to $100 in free credits, then from $50 a month for 20,000 credits. Viktor says that 40,000 credits are about 80 to 400 tasks, and scheduled tasks use credits too.",
      better: "volto",
    },
    {
      icon: "laptop",
      topic: "Where agents work",
      volto: "On the computer that runs Volto, or on a hosted Volto server in the EU.",
      rival: "On Viktor's own cloud computer for your workspace, in the US.",
      better: "volto",
    },
    {
      icon: "lock",
      topic: "Your data",
      volto:
        "Workspaces, chats and files stay in a database on your computer. The provider you choose gets the requests you send. Product analytics, with no chat content, are on by default and linked to your account when you sign in; you can turn them off.",
      rival:
        "Stored in the US. Viktor can read all messages in the Slack channels it joins, not only those that mention it, and can get up to about 90 days of earlier history. EU data residency is only on Enterprise contracts.",
      better: "volto",
    },
    {
      icon: "users",
      topic: "Teams",
      volto:
        "Each agent is a full coding agent with its own job, model and workspace. They give work to each other in shared channels, and coworkers can join one team host. Roles set which agents each member can view, chat with or manage.",
      rival: "One shared AI coworker for the whole workspace, with one pool of credits.",
      better: "volto",
    },
    {
      icon: "check",
      topic: "Who approves an action",
      volto:
        "You do. An agent shows a draft and waits for your approval before it sends a message for you, unless you or a routine that you set up tells it to send. It asks you to confirm the item, price and total before a payment.",
      rival:
        "An admin chooses which tools need approval. Under Viktor's terms, you are responsible for every message and action that it sends or takes for you.",
    },
    {
      icon: "puzzle",
      topic: "Integrations",
      volto: "Agents use MCP servers, plugins from the Volto catalog, and the command-line tools on your computer.",
      rival: "More than 9,800 apps, such as Salesforce, HubSpot, Linear, Notion, Stripe and Google Ads.",
      better: "rival",
    },
    {
      icon: "phone",
      topic: "How you talk to it",
      volto:
        "The desktop app, and apps for iPhone and Android over an encrypted connection between your devices. Agents can also answer in Slack, Discord and Telegram.",
      rival: "In Slack, Microsoft Teams or Discord, or in the Viktor web app.",
    },
    {
      icon: "refresh",
      topic: "Backup models",
      volto: VOLTO_TEXT.backupModels,
      rival:
        "An admin chooses the default, fast and deep models. Viktor states a backup only to a second cloud host for Claude Fable 5. When credits run out, Viktor pauses.",
      better: "volto",
    },
    {
      icon: "key",
      topic: "Logins and payments",
      volto: VOLTO_TEXT.logins,
      rival:
        "Viktor keeps the OAuth tokens and API keys of your apps in encrypted vaults and adds them on its servers, so the model does not see them. It states no saved passwords, password manager or payment cards for its browser.",
      better: "volto",
    },
    {
      icon: "mic",
      topic: "Voice",
      volto: VOLTO_TEXT.voice,
      rival:
        "You can talk to Viktor in a live voice call, and it speaks its replies. A setting stops it when you talk over it. ElevenLabs and Groq process the speech in the cloud.",
    },
    {
      icon: "globe",
      topic: "Who can use it",
      volto: `Anyone, in any country: Volto has no region lock. Each AI provider sets the countries for its own plan. The desktop app is in ${VOLTO_LANGUAGE_COUNT} languages, and hosted servers run in the EU.`,
      rival: "Businesses only: its terms say that the service is not for consumers.",
      better: "volto",
    },
    {
      icon: "code",
      topic: "Source code",
      volto: "On GitHub, under the PolyForm Noncommercial License 1.0.0.",
      rival: "A closed service that runs only on Viktor's servers.",
      better: "volto",
    },
  ],
  intro:
    "Viktor is an AI coworker that lives in Slack and Microsoft Teams. Anyone on the team mentions it, and it does the work on its own cloud computer: reports, ad campaigns, bug triage, internal apps and invoices, with more than 9,800 connected apps. Volto also gives you AI agents that do real work. The difference is where they run, who holds your data, and how you pay. Viktor runs in the US, can read the channels it joins, and bills each task in credits. Volto runs a team of agents on your own computer, keeps your data there, and uses the AI plans you already pay for.",
  sections: [
    {
      title: "Models and what you pay",
      volto: `${VOLTO_TEXT.plansList} Volto is free for noncommercial use, so a busy month does not cost more.`,
      rival:
        "Viktor offers models from OpenAI, Anthropic, Google and xAI, and an admin sets the default model for the workspace. You can also connect your own OpenRouter key; those models are billed to your OpenRouter account. You cannot use a ChatGPT or Claude plan that you already pay for. You pay in credits, shared by the whole workspace. You start with up to $100 in free credits. The Team plan starts at $50 a month for 20,000 credits and goes up to $50,000 a month for 20 million credits; Enterprise has custom prices. Viktor says that a quick Slack question uses about 50 to 100 credits, a deep research report 500 to 2,000, and a full project 2,000 to 5,000. Scheduled tasks use credits each time they run, and unused credits roll over for one month only.",
      better: "volto",
    },
    {
      title: "Where the work happens",
      volto: VOLTO_TEXT.workSection,
      rival:
        "Viktor has its own computer in the cloud, where it writes and runs code. Its security page says that it is hosted on AWS in us-east-1, and that each workspace runs in a sandbox. No computer of yours must stay on. Data is in the US by default; EU data residency is only on Enterprise contracts.",
      better: "volto",
    },
    {
      title: "What it reads, and who holds your data",
      volto:
        "Volto keeps workspaces, conversations, attachments and browser data in a SQLite database on the computer that runs it. Volto keeps no other copy. On a hosted Volto server, the database is on that server in the EU. An account holds your profile, team memberships, invitations, sign-in sessions, the settings that let devices find each other, and any agent templates that you publish. Product analytics, which never include chat content, are on by default and linked to your account when you sign in; you can turn them off in Settings.",
      rival:
        "Viktor's privacy policy says that it reads all messages in the Slack channels it joins, not only the messages that mention it, and that it can get up to about 90 days of earlier history when it joins. A user can give it wider search access, which can include private channels and direct messages. Data is stored with cloud providers in US regions, and the policy names more than 30 other services that process it. Viktor says that it does not train foundation models on your data, but it can use your data for small internal models, such as one that chooses the model for a task.",
      better: "volto",
    },
    {
      title: "How agents work as a team",
      volto:
        "In Volto, each agent is a full coding agent: Codex, Claude Code, Gemini, Grok CLI, Cursor CLI, Cline, OpenCode, Pi, Muse Code or an agent of your own, each with its own job, model and workspace. A lead agent can give parts of a task to other agents in a shared channel. You follow their work and step in when a decision needs you. When you move an agent to a different provider, it keeps its role, workspace and conversation. For a team of people, one computer runs the host and the others join it; the chats and files stay on the host. Roles set which agents each member can view, chat with or manage.",
      rival:
        "Viktor is one AI coworker for the whole workspace. Everyone on the team mentions the same Viktor, it keeps one shared memory of the team, and every request uses the same pool of credits.",
      better: "volto",
    },
    {
      title: "Approvals and responsibility",
      volto:
        "Volto agents show a draft before they send a message for you. They send it after you approve it, or when you or a routine that you set up tells them to send. Before a payment, an agent reads back the item, price and total and asks you to confirm. A new agent has Full access: it does not ask before it runs a command. Set an agent to Ask first, and that agent asks you first; you answer in the chat or from a notification on your phone.",
      rival:
        "Some built-in actions, such as joining a Slack channel, always need approval in Viktor. For each integration tool, an admin chooses if it runs on its own, needs approval, or never runs, and a user who can edit that tool can click Always approve (not in Microsoft Teams). Viktor's terms make you responsible for all messages and actions sent or taken through your account, and you must defend Viktor against third-party claims that come from any action it takes through a connected app under the permissions you set.",
    },
    {
      title: "Integrations and company controls",
      volto:
        "Volto agents use MCP servers, plugins from the Volto catalog, and the tools on the computer that runs them. They can also answer in Slack, Discord and Telegram. Volto has no SOC 2 report. On your own computer, your own security controls apply to your data.",
      rival:
        "Viktor connects to more than 9,800 apps and builds a custom integration for one it does not have. It has a SOC 2 Type 1 report; SOC 2 Type 2 and ISO 27001 are in progress. Enterprise adds SAML single sign-on, role-based access, audit logs and a DPA.",
      better: "rival",
    },
  ],
  faq: [
    {
      question: "Is Volto an alternative to Viktor?",
      answer:
        "Yes, if you want your agents and your data on your own computer. Both give you AI agents that do work for your team. Volto runs a team of agents on your computer, with the AI plans you already pay for. Viktor is one AI coworker in Slack or Microsoft Teams that runs in the US and bills each task in credits.",
    },
    {
      question: "How much does Viktor cost?",
      answer:
        "Viktor gives up to $100 in free credits. Then the Team plan starts at $50 a month for 20,000 credits; Viktor says that 40,000 credits are about 80 to 400 tasks. Enterprise has custom prices. Volto is free for noncommercial use, and it uses the AI plans you already pay for, with no credits.",
    },
    {
      question: "Can I use my ChatGPT or Claude plan with Viktor?",
      answer:
        "No. Viktor bills its models in credits, or you connect your own OpenRouter key. Volto runs Codex with your ChatGPT plan, Claude Code with your Claude plan, and Gemini, Grok, Cursor, Cline and Muse Code with your own accounts.",
    },
    {
      question: "Does Viktor read all my Slack messages?",
      answer:
        "Viktor's privacy policy says that it reads all messages in the channels it joins, not only those that mention it, and up to about 90 days of earlier history. A user can give it wider access, which can include private channels and direct messages. Volto keeps your workspaces, chats and files in a database on your own computer.",
    },
    {
      question: "Where does Viktor store my data?",
      answer:
        "In the US: Viktor is hosted on AWS in us-east-1, and EU data residency is only on Enterprise contracts. Volto keeps your data on your computer, or on a hosted Volto server in the EU that you choose.",
    },
    {
      question: "Do agents keep working when my laptop is closed?",
      answer:
        "Yes, with both. Viktor works on its own cloud computer, so no computer of yours must stay on. Volto agents work on the computer or server that runs Volto: keep that computer on, or use a hosted Volto server, and connect from your laptop or phone.",
    },
    {
      question: "Is Viktor open source?",
      answer:
        "No. Viktor is a closed service. Volto's source code is on GitHub under the PolyForm Noncommercial License 1.0.0.",
    },
  ],
  sources: [
    { label: "Viktor", url: "https://viktor.com/" },
    { label: "Viktor pricing", url: "https://viktor.com/pricing" },
    { label: "Viktor security", url: "https://viktor.com/security" },
    { label: "Viktor changelog", url: "https://viktor.com/changelog" },
    { label: "Viktor approvals", url: "https://viktor.com/docs/approvals" },
    { label: "Viktor integration permissions", url: "https://viktor.com/docs/integration-permissions" },
    { label: "Viktor chat commands", url: "https://viktor.com/docs/chat-commands" },
    { label: "Viktor enterprise", url: "https://viktor.com/enterprise" },
    { label: "Viktor privacy policy", url: "https://viktor.com/legal/privacy" },
    { label: "Viktor general terms", url: "https://viktor.com/legal/general-terms" },
    { label: "Viktor settings", url: "https://viktor.com/docs/settings-overview" },
    { label: "Viktor: running out of credits", url: "https://viktor.com/docs/running-out-of-credits" },
    { label: "Viktor: what Viktor can do", url: "https://viktor.com/docs/what-viktor-can-do" },
    { label: "Volto privacy notes", url: VOLTO_LINKS.privacy },
    { label: "Volto source code", url: VOLTO_LINKS.repository },
  ],
  checkedAt: "2026-10-23",
};
