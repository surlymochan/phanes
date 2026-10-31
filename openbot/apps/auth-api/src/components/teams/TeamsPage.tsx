import type { JSX } from "@solidjs/web";
import { createSignal, For, lazy, onCleanup, onSettled, Show } from "solid-js";
import { landingAnalytics } from "../../lib/analytics";
import { LANDING_TEAM, type LandingTeammate } from "../../lib/landing-content";
import {
  TEAMS_AUDIENCE,
  TEAMS_BENEFITS,
  TEAMS_CLOSING,
  TEAMS_CUSTOM,
  TEAMS_HERO,
  TEAMS_ROUTE,
  TEAMS_SETUPS,
  TEAMS_SETUPS_SECTION,
  type TeamsSetupId,
} from "../../lib/teams-page";
import { createLandingReveal } from "../landing/createLandingReveal";
import { LandingFooter } from "../landing/LandingFooter";
import { LandingGlow } from "../landing/LandingGlow";
import { LandingIcon, type LandingIconName } from "../landing/LandingIcon";
import { SiteHeader } from "../landing/SiteHeader";
import { ButtonLink } from "../ui/button";

// Loaded after the page is in the browser, as in `FeaturesSection`: the avatar engine is large.
const AgentAvatar = lazy(() =>
  import("@openbot/ui/features/agents/AgentAvatar").then((module) => ({ default: module.AgentAvatar })),
);

const SETUP_ICONS: Record<TeamsSetupId, LandingIconName> = {
  local: "laptop",
  managed: "cloud",
  custom: "blocks",
};

/** One icon for each item of `TEAMS_CUSTOM.items`, in the same order. */
const CUSTOM_ICONS = ["cpu", "users", "puzzle", "blocks", "clock"] as const satisfies readonly LandingIconName[];

/** One icon for each item of `TEAMS_AUDIENCE.items`, in the same order. */
const AUDIENCE_ICONS = ["users", "globe", "clock", "puzzle", "contact"] as const satisfies readonly LandingIconName[];

/** The two marquee rows, as indexes of `TEAMS_AUDIENCE.items`. Each row repeats its items, so the loop has no gap. */
const MARQUEE_ROWS = [
  [0, 1, 2, 0, 1, 2],
  [3, 4, 3, 4, 3, 4],
] as const;

/** The words of the closing title that get the setup colours. */
const ACCENT_WORDS = new Set(["AI", "coworkers"]);

const pad = (value: number) => String(value).padStart(2, "0");

/** A random number from `min` to `max`. */
const between = (min: number, max: number) => min + Math.random() * (max - min);

// The page for companies: the team in the title, the setups in the colours of the Teams cards on the
// landing page, what a server adds, the steps of a custom project, who it is for, and one closing call.
// Each button opens the contact form.
export function TeamsPage() {
  onSettled(() => landingAnalytics.start(document, window.location.hostname, TEAMS_ROUTE));

  return (
    <div class="landing-page teams-page">
      <SiteHeader page="content" />

      <main class="teams-main">
        <TeamsHero />

        <RevealSection id="setups" labelledBy="teams-setups-title">
          <header class="teams-heading teams-heading-center">
            <h2 id="teams-setups-title">{TEAMS_SETUPS_SECTION.heading}</h2>
          </header>
          <ol class="teams-setups">
            <For each={TEAMS_SETUPS}>
              {(setup) => (
                <li class="teams-setup" data-setup={setup.id}>
                  <div class="landing-download-card-top">
                    <LandingIcon name={SETUP_ICONS[setup.id]} class="teams-setup-icon" />
                    <span class="landing-download-status">{setup.label}</span>
                  </div>
                  <div class="landing-download-card-copy">
                    <h3>{setup.title}</h3>
                    <p>{setup.description}</p>
                  </div>
                </li>
              )}
            </For>
          </ol>
          <p class="teams-help">
            {TEAMS_SETUPS_SECTION.question}
            <ContactButton label={TEAMS_SETUPS_SECTION.action} size="sm" variant="secondary" />
          </p>
        </RevealSection>

        <RevealSection id="benefits">
          <ul class="teams-bento">
            <For each={TEAMS_BENEFITS}>
              {(benefit, index) => (
                <li class="teams-tile" data-tile={index()}>
                  <div class="teams-tile-art" aria-hidden="true">
                    <LandingIcon name={benefit.icon} />
                  </div>
                  <div class="teams-tile-copy">
                    <h2>{benefit.title}</h2>
                    <p>{benefit.description}</p>
                  </div>
                </li>
              )}
            </For>
          </ul>
        </RevealSection>

        <CustomTimeline />

        <AudienceMarquee />

        <ClosingSection />
      </main>

      <LandingFooter />
      <LandingGlow />
    </div>
  );
}

function ContactButton(props: { label: string; size?: "sm" | "lg"; variant?: "primary" | "secondary" }) {
  return (
    <ButtonLink to="/contact" variant={props.variant ?? "primary"} size={props.size ?? "lg"} icon="contact">
      {props.label}
    </ButtonLink>
  );
}

/** A section that fades in when it scrolls into view, as the landing sections do. */
function RevealSection(props: { id: string; labelledBy?: string; children: JSX.Element }) {
  let sectionRef: HTMLElement | undefined;
  const revealed = createLandingReveal(() => sectionRef);
  return (
    <section
      ref={sectionRef}
      id={props.id}
      class="teams-section"
      aria-labelledby={props.labelledBy}
      data-revealed={revealed() ? "true" : "false"}
    >
      {props.children}
    </section>
  );
}

// The hero ----------------------------------------------------------------------------------------

/** "Bring a team of AI coworkers …": the avatars go after "of". */
const [titleLead, titleRest] = TEAMS_HERO.heading.split(" of ");

/** As the home hero puts the logo in "Meet OpenBot", this title holds the team. Their eyes follow the pointer. */
function TeamsHero() {
  return (
    <section class="landing-hero teams-hero" aria-labelledby="teams-title">
      <div class="landing-hero-grid" aria-hidden="true" />
      <div class="teams-cursors" aria-hidden="true">
        <For each={LANDING_TEAM}>{(teammate, index) => <Cursor teammate={teammate} index={index()} />}</For>
      </div>
      <div class="landing-hero-copy">
        <p class="teams-pill landing-hero-line" style={{ "--landing-hero-line": 0 }}>
          <span class="teams-pill-dots" aria-hidden="true">
            <For each={TEAMS_SETUPS}>{(setup) => <span data-setup={setup.id} />}</For>
          </span>
          <span class="teams-pill-label">OpenBot for Teams</span>
        </p>
        <h1 id="teams-title" class="teams-title landing-hero-line" style={{ "--landing-hero-line": 1 }}>
          <span>{titleLead}</span>{" "}
          <span class="teams-title-keep">
            of{" "}
            <span class="teams-title-team" aria-hidden="true">
              <For each={LANDING_TEAM}>{(teammate) => <TitleAvatar teammate={teammate} />}</For>
            </span>
          </span>{" "}
          <span>{titleRest}</span>
        </h1>
        <p class="landing-description landing-hero-line" style={{ "--landing-hero-line": 2 }}>
          {TEAMS_HERO.intro}
        </p>
        <div class="landing-actions landing-hero-line" style={{ "--landing-hero-line": 3 }}>
          <ContactButton label={TEAMS_CLOSING.action} />
        </div>
      </div>
    </section>
  );
}

/** An agent avatar. The Worker renders an empty circle of its size, and the browser fills it in. */
function TitleAvatar(props: { teammate: LandingTeammate }) {
  const [mounted, setMounted] = createSignal(false);
  onSettled(() => {
    setMounted(true);
  });
  return (
    <Show when={mounted()} fallback={<span class="teams-avatar" />}>
      <AgentAvatar
        seed={props.teammate.avatarSeed}
        hue={props.teammate.avatarHue}
        motion="hover"
        followPointer
        class="teams-avatar"
      />
    </Show>
  );
}

/** The area of each cursor, as parts of the hero's width and height. The areas overlap the title edges. */
const CURSOR_AREAS = [
  { left: 0.02, top: 0.1, width: 0.32, height: 0.46 },
  { left: 0.68, top: 0.18, width: 0.28, height: 0.52 },
  { left: 0.05, top: 0.56, width: 0.34, height: 0.36 },
] as const;

/**
 * A name cursor that moves as a hand moves a mouse. It steers to a random point in its area: its speed
 * lags behind the wanted speed (inertia), it slows down near the point, and a bend makes the path a curve.
 * Then it waits and chooses a new point. Each cursor has its own random values, so they never move together.
 */
function Cursor(props: { teammate: LandingTeammate; index: number }) {
  let element: HTMLSpanElement | undefined;
  let frame = 0;
  let last = 0;
  const position = { x: 0, y: 0 };
  const velocity = { x: 0, y: 0 };
  const move = { x: 0, y: 0, speed: 0, grip: 0, bend: 0, waitUntil: 0 };

  /** Chooses the next point. The cursor starts to it at `startAt`. */
  const pickTarget = (startAt: number) => {
    const area = CURSOR_AREAS[props.index] ?? CURSOR_AREAS[0];
    const box = element?.parentElement?.getBoundingClientRect();
    if (!box) return;
    move.x = between(area.left, area.left + area.width) * box.width;
    move.y = between(area.top, area.top + area.height) * box.height;
    // Top speed in px/s, how fast the speed follows the wanted speed, and how much the path curves.
    move.speed = between(260, 720);
    move.grip = between(2.4, 4.2);
    move.bend = between(-0.45, 0.45);
    move.waitUntil = startAt;
  };

  const draw = () => {
    if (!element) return;
    // The arrow leans a little into a sideways move.
    const lean = Math.max(-10, Math.min(10, velocity.x * 0.02));
    element.style.transform = `translate3d(${position.x.toFixed(1)}px, ${position.y.toFixed(1)}px, 0)`;
    element.style.setProperty("--teams-lean", `${lean.toFixed(2)}deg`);
  };

  const step = (now: number) => {
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    const dx = move.x - position.x;
    const dy = move.y - position.y;
    const distance = Math.hypot(dx, dy);
    const speed = Math.hypot(velocity.x, velocity.y);
    if (now >= move.waitUntil) {
      if (distance < 3 && speed < 12) {
        // Arrived: rest for a moment, then go on.
        pickTarget(now + between(400, 2600));
      } else {
        // The wanted speed falls near the point, so the cursor slows down instead of stopping hard.
        const wanted = Math.min(move.speed, distance * 2.6);
        const nx = dx / (distance || 1);
        const ny = dy / (distance || 1);
        const curve = move.bend * Math.min(1, distance / 160);
        const desiredX = (nx - ny * curve) * wanted;
        const desiredY = (ny + nx * curve) * wanted;
        velocity.x += (desiredX - velocity.x) * Math.min(1, move.grip * dt);
        velocity.y += (desiredY - velocity.y) * Math.min(1, move.grip * dt);
      }
    } else {
      velocity.x *= 1 - Math.min(1, 6 * dt);
      velocity.y *= 1 - Math.min(1, 6 * dt);
    }
    position.x += velocity.x * dt;
    position.y += velocity.y * dt;
    draw();
    frame = requestAnimationFrame(step);
  };

  let stop = () => {};

  /** Puts the cursor on its first point and shows it. A hidden hero (phone width) has no size. */
  const place = () => {
    if (!element || element.dataset.placed) return;
    const box = element.parentElement?.getBoundingClientRect();
    if (!box?.width || !box.height) return;
    pickTarget(0);
    position.x = move.x;
    position.y = move.y;
    draw();
    element.dataset.placed = "true";
  };

  onSettled(() => {
    place();
    const area = element?.parentElement;
    if (!area || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const pause = () => {
      cancelAnimationFrame(frame);
      frame = 0;
    };
    // The cursor moves only while the hero shows. A hero that is hidden or scrolled away uses no frames.
    const observer = new IntersectionObserver(([entry]) => {
      if (!entry?.isIntersecting) return pause();
      place();
      if (frame) return;
      last = performance.now();
      pickTarget(last + between(0, 1200));
      frame = requestAnimationFrame(step);
    });
    observer.observe(area);
    stop = () => {
      observer.disconnect();
      pause();
    };
  });
  // The Worker also runs this cleanup. It has no `IntersectionObserver`, and `stop` changes only in the browser.
  onCleanup(() => stop());

  return (
    <span ref={element} class="teams-cursor" data-cursor={props.index}>
      <svg viewBox="0 0 24 24" class="teams-cursor-arrow" aria-hidden="true">
        <path d="M4 3l7 18 2.5-7.5L21 11z" />
      </svg>
      <span class="teams-cursor-name">{props.teammate.name}</span>
    </span>
  );
}

// The custom-deployment section ---------------------------------------------------------------

/** The five items are the steps of one project. One time, a spark runs along the line and lights each step. */
function CustomTimeline() {
  let timeline: HTMLDivElement | undefined;
  const playing = createLandingReveal(() => timeline, { rootMargin: "0px 0px -25% 0px" });

  return (
    <RevealSection id="custom" labelledBy="teams-custom-title">
      <header class="teams-heading teams-heading-center">
        <h2 id="teams-custom-title">{TEAMS_CUSTOM.heading}</h2>
        <p>{TEAMS_CUSTOM.intro}</p>
      </header>
      <div ref={timeline} class="teams-timeline" data-play={playing() ? "true" : "false"}>
        <span class="teams-timeline-line" aria-hidden="true">
          <span class="teams-timeline-fill" />
          <span class="teams-timeline-spark" />
        </span>
        <ol class="teams-timeline-track">
          <For each={TEAMS_CUSTOM.items}>
            {(item, index) => (
              <li class="teams-step" data-tile={index() % 4} style={{ "--teams-index": index() }}>
                <span class="teams-step-node">
                  <LandingIcon name={CUSTOM_ICONS[index()] ?? "check"} />
                </span>
                <span class="teams-step-number">{pad(index() + 1)}</span>
                <span class="teams-step-title">{item}</span>
              </li>
            )}
          </For>
        </ol>
      </div>
    </RevealSection>
  );
}

// The audience section --------------------------------------------------------------------------

/** "Companies introducing AI agents …" gives "Companies" and "introducing AI agents …". */
function splitLead(item: string) {
  const space = item.indexOf(" ");
  return { lead: item.slice(0, space), rest: item.slice(space + 1) };
}

function AudienceChip(props: { index: number }) {
  const parts = () => splitLead(TEAMS_AUDIENCE.items[props.index] ?? "");
  return (
    <span class="teams-chip" data-tile={props.index % 4}>
      <span class="teams-chip-icon">
        <LandingIcon name={AUDIENCE_ICONS[props.index] ?? "check"} />
      </span>
      <span>
        <strong>{parts().lead}</strong> {parts().rest}
      </span>
    </span>
  );
}

/** Two rows of chips that move in opposite directions. The pointer on a row stops it. */
function AudienceMarquee() {
  return (
    <RevealSection id="audience" labelledBy="teams-audience-title">
      <header class="teams-heading teams-heading-center">
        <h2 id="teams-audience-title">{TEAMS_AUDIENCE.heading}</h2>
      </header>
      <div class="teams-marquee">
        <ul class="landing-visually-hidden">
          <For each={TEAMS_AUDIENCE.items}>{(item) => <li>{item}</li>}</For>
        </ul>
        <For each={MARQUEE_ROWS}>
          {(row, rowIndex) => (
            <div class="teams-marquee-row" data-row={rowIndex()} aria-hidden="true">
              <div class="teams-marquee-track">
                <For each={[0, 1]}>
                  {() => (
                    <div class="teams-marquee-group">
                      <For each={row}>{(index) => <AudienceChip index={index} />}</For>
                    </div>
                  )}
                </For>
              </div>
            </div>
          )}
        </For>
      </div>
    </RevealSection>
  );
}

// The closing section ---------------------------------------------------------------------------

/** Each word of the title becomes bright as you scroll. A coloured grid follows the pointer. */
function ClosingSection() {
  let section: HTMLElement | undefined;
  let heading: HTMLHeadingElement | undefined;
  let spot: HTMLDivElement | undefined;
  const words = TEAMS_CLOSING.heading.split(" ");
  let stop = () => {};

  onSettled(() => {
    if (!section || !heading) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      section.style.setProperty("--teams-progress", "1");
      section.dataset.done = "true";
      return;
    }
    // The scroll sets a target: 0 when the title top is at 92% of the window, 1 at 38%. The shown value
    // follows the target with inertia, so a fast scroll or a wheel step does not make the words jump.
    let target = 0;
    let shown = 0;
    let frame = 0;
    let last = 0;
    const draw = () => {
      if (!section) return;
      section.style.setProperty("--teams-progress", shown.toFixed(4));
      // Two limits, so the glow does not turn on and off at the edge.
      if (shown > 0.99) section.dataset.done = "true";
      else if (shown < 0.9) section.dataset.done = "false";
    };
    const step = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      shown += (target - shown) * (1 - Math.exp(-dt * 7));
      if (Math.abs(target - shown) < 0.0005) shown = target;
      draw();
      frame = shown === target ? 0 : requestAnimationFrame(step);
    };
    const update = () => {
      if (!heading) return;
      const top = heading.getBoundingClientRect().top;
      target = Math.max(0, Math.min(1, (window.innerHeight * 0.92 - top) / (window.innerHeight * 0.54)));
      if (frame || target === shown) return;
      last = performance.now();
      frame = requestAnimationFrame(step);
    };
    document.addEventListener("scroll", update, { capture: true, passive: true });
    window.addEventListener("resize", update);
    update();
    shown = target;
    draw();
    stop = () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("scroll", update, { capture: true });
      window.removeEventListener("resize", update);
    };
  });
  onCleanup(() => stop());

  return (
    <section
      ref={section}
      id="contact"
      class="teams-section teams-closing"
      aria-labelledby="teams-closing-title"
      onPointerMove={(event) => {
        const box = spot?.getBoundingClientRect();
        if (!box) return;
        spot?.style.setProperty("--teams-x", `${event.clientX - box.left}px`);
        spot?.style.setProperty("--teams-y", `${event.clientY - box.top}px`);
      }}
    >
      <div class="landing-hero-grid" aria-hidden="true" />
      <div ref={spot} class="landing-hero-grid teams-closing-spot" aria-hidden="true" />
      <h2 ref={heading} id="teams-closing-title" style={{ "--teams-count": words.length }}>
        <For each={words}>
          {(word, index) => (
            <>
              <span
                class="teams-closing-word"
                data-accent={ACCENT_WORDS.has(word) ? "true" : undefined}
                style={{ "--teams-word": index() }}
              >
                {word}
              </span>{" "}
            </>
          )}
        </For>
      </h2>
      <span class="teams-closing-action">
        <ContactButton label={TEAMS_CLOSING.action} />
      </span>
    </section>
  );
}
