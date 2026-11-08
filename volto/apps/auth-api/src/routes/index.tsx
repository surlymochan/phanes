import { createFileRoute } from "@tanstack/solid-router";
import { LandingPage } from "../components/landing/LandingPage";
import { voltoHomeHead } from "../lib/site-metadata";
import { VOLTO_SOFTWARE_APPLICATION } from "../lib/software-application";

export const Route = createFileRoute("/")({
  head: () => voltoHomeHead(VOLTO_SOFTWARE_APPLICATION),
  component: LandingPage,
});
