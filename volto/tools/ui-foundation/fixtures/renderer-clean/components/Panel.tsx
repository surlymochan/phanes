// A feature component that breaks nothing: a shared primitive instead of a native control,
// palette tokens instead of literals, a role the composite ratchet does not count, and the
// one class styles.css declares, so the dead-rule scan has nothing to report here either.
import { Dialog } from "./ui/complex";

export function Panel() {
  return (
    <section class="panel" role="group" style={{ color: "var(--volto-text)", transition: "var(--volto-transition)" }}>
      <Dialog />
    </section>
  );
}
