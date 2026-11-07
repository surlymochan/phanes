// The contact form on `/contact` and the `/v1/contact` route that sends it share these values.

/** The path of the contact page. */
export const CONTACT_ROUTE = "/contact";

/** The mailbox that receives each message. The form also names it for people who prefer email. */
export const CONTACT_EMAIL = "hello@openbot.run";

export const CONTACT_TOPICS = [
  { id: "setup", label: "Custom setup" },
  { id: "partnership", label: "Partnership" },
  { id: "press", label: "Press" },
  { id: "other", label: "Something else" },
] as const;

export const CONTACT_LIMITS = {
  name: 120,
  company: 120,
  message: 2_000,
} as const;

export function contactTopicLabel(id: string): string | null {
  return CONTACT_TOPICS.find((topic) => topic.id === id)?.label ?? null;
}
