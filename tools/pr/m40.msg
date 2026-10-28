Prove server claims with only a local account service

Add an E2E that starts a throwaway local account service with its own
migrated database, signs a server claim through the admin route, redeems
it, and stops the whole process group, so a listening grandchild cannot
survive. The account service accepts an optional state directory for it.
