# Verify intent card template

Apply the `verify-intent` label and keep the `## Verify Intent` body heading. AD
recognizes a verification card only when both markers are present. The card is
created by the tester; the tester does not assign a lane. AD places it in the
same `lane-ready` queue and assigns it only to CX2, CX4, or CX5 review lanes.

```markdown
## Verify Intent
目标 PR: #<PR number or URL>
证据要求:
- [ ] <one observable check, command, or artifact>
- [ ] <another check>
风险等级: <low|medium|high> (optional)
```

目标 PR and at least one checkbox evidence item are required. Invalid cards
stay undispatched and AD logs the missing fields. At equal priority, AD ranks
work cards before verify cards, then by issue number. The queue share policy is
configurable for future P2-3 work; this card does not implement quotas.
