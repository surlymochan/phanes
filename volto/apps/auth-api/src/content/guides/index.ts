// Slug to guide body, on the same terms as the news bodies: eager, so the prose
// is in the server's HTML rather than arriving after hydration.

import type { ArticleBody } from "../body";
import { AgenticAIDefinition } from "./agentic-ai-definition";
import { AIAgentsExamples } from "./ai-agents-examples";
import { AIAgentsVsAgenticAI } from "./ai-agents-vs-agentic-ai";
import { Volto101 } from "./volto-101";
import { VoltoHostedServers } from "./volto-hosted-servers";
import { VoltoMarketplace } from "./volto-marketplace";
import { WhatAreAIAgents } from "./what-are-ai-agents";
import { WtfIsVolto } from "./wtf-is-volto";

export const GUIDE_BODIES: Readonly<Record<string, ArticleBody>> = {
  "ai-agents-examples": AIAgentsExamples,
  "ai-agents-vs-agentic-ai": AIAgentsVsAgenticAI,
  "agentic-ai-definition": AgenticAIDefinition,
  "what-are-ai-agents": WhatAreAIAgents,
  "openbot-101": Volto101,
  "openbot-hosted-servers": VoltoHostedServers,
  "openbot-marketplace": VoltoMarketplace,
  "wtf-is-openbot": WtfIsVolto,
};
