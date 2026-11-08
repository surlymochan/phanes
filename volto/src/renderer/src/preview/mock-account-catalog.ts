import { STORY_AGENT_TEMPLATE_DETAIL } from "./agent-template-fixtures";
import {
  STORY_MARKETPLACE_AGENT_DETAILS,
  STORY_MARKETPLACE_AGENTS,
  STORY_MARKETPLACE_SKILL_DETAILS,
  STORY_MARKETPLACE_SKILLS,
} from "./fixtures";
import { matchesQuery } from "./mock-support";

/** One page of `items`, with the search, filter and cursor of the account service's list routes. */
function page<T extends { category?: string | undefined; featured?: boolean | undefined }>(
  items: readonly T[],
  params: URLSearchParams,
  text: (item: T) => string,
) {
  const matches = items.filter(
    (item) =>
      matchesQuery(text(item), params.get("query") ?? undefined) &&
      (!params.has("category") || (item.category ?? "other") === params.get("category")) &&
      (params.get("featured") !== "true" || item.featured),
  );
  const start = Number(params.get("cursor") ?? 0);
  const end = start + Number(params.get("limit") ?? 50);
  return { items: matches.slice(start, end), nextCursor: end < matches.length ? String(end) : null };
}

/** The answer of a catalog route, or a 404 for a listing that the fixtures do not have. */
function catalogResponse(path: string, params: URLSearchParams): Response {
  if (path === "/v1/skills/") {
    const { items, nextCursor } = page(
      STORY_MARKETPLACE_SKILLS,
      params,
      (skill) => `${skill.name} ${skill.description} ${skill.creatorName}`,
    );
    return Response.json({ skills: items, nextCursor });
  }
  if (path === "/v1/marketplace/agents/") {
    const { items, nextCursor } = page(
      STORY_MARKETPLACE_AGENTS,
      params,
      (agent) => `${agent.name} ${agent.title} ${agent.description} ${agent.creatorName}`,
    );
    return Response.json({ agents: items, nextCursor });
  }
  const [, collection, id] = /^\/v1\/(skills|marketplace\/agents|agent-templates)\/([^/]+)$/.exec(path) ?? [];
  const key = id ? decodeURIComponent(id) : "";
  const body =
    collection === "skills"
      ? STORY_MARKETPLACE_SKILL_DETAILS[key]
      : collection === "marketplace/agents"
        ? STORY_MARKETPLACE_AGENT_DETAILS[key]
        : key === STORY_AGENT_TEMPLATE_DETAIL.id
          ? STORY_AGENT_TEMPLATE_DETAIL
          : undefined;
  return body ? Response.json(body) : Response.json({ error: "not_found" }, { status: 404 });
}

/**
 * The account service's public marketplace routes, for the web client in Storybook: the skill and
 * agent catalogs, their pages, and shared agent templates. Other requests go to `fallback`.
 */
export function mockAccountCatalogFetch(fallback: typeof fetch = fetch): typeof fetch {
  return async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input), window.location.origin);
    if (!url.pathname.startsWith("/v1/")) return fallback(input, init);
    return catalogResponse(url.pathname, url.searchParams);
  };
}
