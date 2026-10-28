Keep the original cause when the automation server reports a refusal

runCauseEffect threw the failure wrapper when an error had no tag, so the
automation server lost the reason of a refused update or a new chat on a busy
agent. Throw the original cause again for an untagged error.
