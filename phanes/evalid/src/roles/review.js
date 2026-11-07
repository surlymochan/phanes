const OID = /^[0-9a-f]{40,64}$/;
const DIGEST = /^sha256:[0-9a-f]{64}$/;

/** Validate a delivery review against the controller's frozen delivery inputs. */
export function validateDelivery({ review, approvedSpec, tdd, holdout, baseCommit, headCommit, commits, intake }) {
  const findings = [];
  const reject = (code, reason, verdict = 'FAIL') => findings.push({ code, reason, verdict });

  if (!review || typeof review !== 'object') return { verdict: 'FAIL', findings: [{ code: 'invalid-review', reason: 'Review payload is required.', verdict: 'FAIL' }] };
  if (!approvedSpec?.approved || !DIGEST.test(approvedSpec.digest ?? '')) reject('spec-not-approved', 'An approved Spec receipt is required.');
  if (!tdd || tdd.phase !== 'green' || tdd.status !== 'PASS' || tdd.spec_digest !== approvedSpec?.digest) reject('tdd-not-accepted', 'Accepted green evidence for the approved Spec is required.');
  if (!OID.test(baseCommit ?? '') || !OID.test(headCommit ?? '') || review.reviewed_base !== baseCommit || review.reviewed_head !== headCommit) reject('stale-head', 'Review must cover the frozen base and current head.');
  if (!Array.isArray(commits) || review.commits?.length !== commits.length || review.commits.some((oid, i) => oid !== commits[i])) reject('commit-chain-mismatch', 'Review must include the complete ordered commit chain.');
  if (!DIGEST.test(review.spec_digest ?? '') || review.spec_digest !== approvedSpec?.digest || !DIGEST.test(review.tdd_digest ?? '') || review.tdd_digest !== tdd?.digest) reject('evidence-mismatch', 'Review evidence must reference the accepted Spec and TDD artifacts.');
  if (review.verdict !== 'PASS' && review.verdict !== 'FAIL' && review.verdict !== 'NEEDS-BOSS') reject('invalid-verdict', 'Review verdict is invalid.');
  if (!Array.isArray(review.findings)) reject('invalid-findings', 'Review findings must be an array.');
  else {
    for (const finding of review.findings) {
      if (finding.severity === 'BLOCKER') reject('blocker', finding.reason || 'Review contains a BLOCKER.');
      else if (finding.severity !== 'FOLLOW-UP') reject('invalid-finding', 'Finding severity must be BLOCKER or FOLLOW-UP.');
    }
  }
  if (holdout && (review.holdout_receipt !== holdout.digest || holdout.subject_head !== headCommit || holdout.spec_digest !== approvedSpec?.digest)) reject('holdout-binding', 'Holdout must bind the review receipt, head and approved Spec.');
  if (holdout?.status === 'NOT_APPLICABLE' && intake?.holdout_required !== false) reject('holdout-not-frozen', 'Not applicable requires a frozen intake exemption.');
  if (!holdout || !DIGEST.test(holdout.digest ?? '') || holdout.status === 'unknown' || holdout.status == null) reject('holdout-unknown', 'A valid holdout receipt is required.');
  else if (holdout.unpublished_requirement === true) reject('unpublished-requirement', 'An unpublished requirement must be escalated to the Boss.', 'NEEDS-BOSS');
  else if (holdout.status !== 'PASS' && !(holdout.status === 'NOT_APPLICABLE' && holdout.required === false)) reject('holdout-failed', 'Required holdout did not pass.');
  else if (holdout.status === 'PASS' && holdout.isolation !== 'verified') reject('holdout-isolation', 'Passing holdout requires verified isolation.');

  if (review.verdict === 'NEEDS-BOSS') reject('review-needs-boss', 'Independent review requires Boss decision.', 'NEEDS-BOSS');
  else if (review.verdict === 'FAIL') reject('review-failed', 'Independent review did not pass.');
  const verdict = findings.some(f => f.verdict === 'NEEDS-BOSS') ? 'NEEDS-BOSS' : findings.length ? 'FAIL' : 'PASS';
  return { verdict, findings };
}

export const validateReview = validateDelivery;
