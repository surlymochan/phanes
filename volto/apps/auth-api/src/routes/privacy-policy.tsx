import { createFileRoute } from "@tanstack/solid-router";
import { PrivacyPolicyPage } from "../components/legal/PrivacyPolicyPage";
import { PRIVACY_POLICY_META, PRIVACY_POLICY_ROUTE } from "../lib/privacy-policy";
import { VOLTO_SOCIAL_IMAGE_ALT, VOLTO_SOCIAL_IMAGE_URL, VOLTO_X_HANDLE } from "../lib/site-metadata";

export const Route = createFileRoute("/privacy-policy")({
  head: ({ match }) => {
    const url = new URL(PRIVACY_POLICY_ROUTE, match.context.siteUrl).toString();
    const { title, description } = PRIVACY_POLICY_META;
    return {
      meta: [
        { title },
        { name: "description", content: description },
        { property: "og:type", content: "website" },
        { property: "og:site_name", content: "volto" },
        { property: "og:locale", content: "en_US" },
        { property: "og:url", content: url },
        { property: "og:title", content: title },
        { property: "og:description", content: description },
        { property: "og:image", content: VOLTO_SOCIAL_IMAGE_URL },
        { property: "og:image:alt", content: VOLTO_SOCIAL_IMAGE_ALT },
        { name: "twitter:card", content: "summary_large_image" },
        { name: "twitter:site", content: VOLTO_X_HANDLE },
        { name: "twitter:title", content: title },
        { name: "twitter:description", content: description },
        { name: "twitter:image", content: VOLTO_SOCIAL_IMAGE_URL },
        { name: "twitter:image:alt", content: VOLTO_SOCIAL_IMAGE_ALT },
      ],
      links: [{ rel: "canonical", href: url }],
    };
  },
  component: PrivacyPolicyPage,
});
