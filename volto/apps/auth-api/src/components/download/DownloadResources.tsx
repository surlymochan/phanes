import { Link } from "@tanstack/solid-router";
import { EXTERNAL_LINK_REL, VOLTO_LINKS } from "../../lib/landing-links";

/** Where a reader goes after the installer: what changed, every file, and what to do when it fails. */
export function DownloadResources() {
  return (
    <p class="download-resources">
      <Link to="/changelog">Release notes</Link>
      <a href={VOLTO_LINKS.releases} target="_blank" rel={EXTERNAL_LINK_REL}>
        All releases on GitHub
      </a>
      <a href={VOLTO_LINKS.troubleshooting} target="_blank" rel={EXTERNAL_LINK_REL}>
        Troubleshooting
      </a>
    </p>
  );
}
